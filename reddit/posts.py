"""Post endpoints: details, the comment tree (+ `more` expansion and a
flat export), duplicates / crossposts, batch lookup, submissions of an
external URL, media download links, and single-comment details.

`post` is {"id", "share"[, "subreddit"]} from refs.resolve_post; `comment` is {"id",
"post_id"} from refs.resolve_comment.

Since 2026-10-20 20:00 UTC Reddit answers the JSON API's /comments/<id> with
its network-security wall for the anonymous app bearer on every exit, so:
  * the post itself (details, media) is read from /by_id/t3_<id>, which still
    answers and carries the same post object (only `num_duplicates`, never
    exposed, is absent);
  * comment trees (comments, more, export, comment_details) come from
    www.reddit.com's shreddit HTML through a local Chrome
    (shreddit.py, browser.py): the tree partial, the POSTed "more comments" loaders and
    the single-comment partial. Reddit folds replies itself there (a big
    thread's first page is 25 top-level comments with their branches as
    `more_replies` stubs), so `depth` is accepted but has no effect; `limit`
    is the number of top-level comments read before the tail becomes a
    `more_comments` cursor.
"""
from . import parsers as P
from . import shared as S
from . import shreddit as H
from .fetch import RedditBadRequest, RedditForbidden, RedditNotFound, get_json

EXPORT_DEFAULT = 500
EXPORT_MAX = 2000
EXPORT_BATCH_CALLS = 80         # browser fetches an export may spend on folded branches


def _pid(post):
    return S.post_id(post)


def _post_json(path, params, post_id, subreddit, label):
    """get_json for a post-scoped path, with Reddit's bare 403 / 404 turned
    into a reasoned RedditNotFound (see shared.unavailable_post)."""
    try:
        return get_json(path, params, label=label)
    except RedditForbidden:
        raise S.unavailable_post(post_id, subreddit)
    except RedditNotFound as e:
        if e.reason:
            raise
        raise RedditNotFound(f"post {post_id} not found", reason="post_not_found", post_id=post_id)


def _post_only(post_id, subreddit):
    """The parsed post from /by_id/t3_<id> (one id: the multi-id form is
    walled). An unknown id is a 404 there; a post Reddit no longer serves
    (removed, banned / private subreddit) is an EMPTY listing, explained
    through shared.unavailable_post like the bare 403 /comments gave."""
    payload = _post_json(f"/by_id/t3_{post_id}", {"limit": 1}, post_id, subreddit, f"post {post_id}")
    children, _ = P.listing_children(payload)
    parsed = P.post(children[0]) if children else None
    if parsed is None:
        raise S.unavailable_post(post_id, subreddit)
    return parsed


def _sub_of(parsed_post, post):
    return ((parsed_post.get("subreddit") or {}).get("name")) or S.post_subreddit(post)


def details(post):
    """Everything about one post (title, body, author, flair, stats,
    flags, media, poll, crosspost parent) — no comments."""
    post_id = _pid(post)
    return {"post": _post_only(post_id, S.post_subreddit(post))}


def comments(post, sort="best", cursor=None, limit=100, depth=None, comment=None, context=0):
    """The post with its nested comment tree. `comment` focuses the tree on
    one comment (with `context` ancestors above it). Every branch Reddit
    left folded is a {count, cursor} `more_replies` for /posts/comments/more."""
    if cursor:
        page = more(cursor)
        return {"post": None, "sort": sort, "comment_count": _count(page["comments"]), **page}
    post_id = _pid(post)
    parsed_post = _post_only(post_id, S.post_subreddit(post))
    subreddit = _sub_of(parsed_post, post)
    if comment:
        page = H.focused(post_id, subreddit, comment, context or 0, sort)
        if H._find(page["comments"], comment) is None:
            raise RedditNotFound(f"comment {comment} not found", reason="comment_not_found", comment_id=comment, post_id=post_id)
    else:
        page = H.tree(post_id, subreddit, sort, limit)
    return {"post": parsed_post, "sort": sort, "comment_count": _count(page["comments"]), **page}


def _count(tree):
    total = 0
    for node in tree or []:
        total += 1 + _count(node.get("replies") or [])
    return total


def more(cursor):
    """Expand one `more_replies` / `more_comments` cursor -> the comments
    it stood for (nested), plus any further `more_comments` cursor."""
    try:
        state = P.decode_cursor(cursor)
    except ValueError as e:
        raise RedditBadRequest(str(e))
    return H.expand(state)


def export(post, sort="best", max_comments=EXPORT_DEFAULT):
    """The whole thread as ONE flat, thread-ordered list (each comment
    followed by its replies, `depth` says how deep) with plain-text bodies —
    every folded branch is expanded until `max_comments`. Meant for
    summarisation / sentiment pipelines. `meta.calls` counts the browser
    fetches spent."""
    post_id = _pid(post)
    max_comments = min(max_comments or EXPORT_DEFAULT, EXPORT_MAX)
    parsed_post = _post_only(post_id, S.post_subreddit(post))
    page = H.tree(post_id, _sub_of(parsed_post, post), sort, limit=0)     # the tail is expanded below, counted
    calls = 1
    tree, tail = page["comments"], page["more_comments"]
    # expand folded branches breadth-first until the budget is spent
    pending = []
    if tail:
        pending.append((None, tail))
    total = _count(tree)
    for node in _walk(tree):
        if node.get("more_replies"):
            pending.append((node, node["more_replies"]))
    while pending and calls < EXPORT_BATCH_CALLS and total < max_comments:
        holder, stub = pending.pop(0)
        try:
            expanded = H.expand(P.decode_cursor(stub["cursor"]))
        except (RedditBadRequest, ValueError):
            continue
        calls += 1
        new_nodes = expanded["comments"]
        if holder is None:
            tree.extend(new_nodes)
        else:
            holder["replies"].extend(new_nodes)
            holder["more_replies"] = None
        total += _count(new_nodes)
        for node in _walk(new_nodes):
            if node.get("more_replies"):
                pending.append((node, node["more_replies"]))
        if expanded.get("more_comments"):
            pending.append((holder, expanded["more_comments"]))
    truncated = bool(pending) or total > max_comments
    flat = []
    for node in _walk(tree):
        if len(flat) >= max_comments:
            break
        flat.append(_flat(node))
    return {
        "post": parsed_post,
        "sort": sort,
        "comment_count": len(flat),
        "comments": flat,
        "meta": {"calls": calls, "is_truncated": truncated, "max_comments": max_comments},
    }


def _walk(nodes):
    for node in nodes or []:
        yield node
        yield from _walk(node.get("replies"))


def _flat(node):
    out = {k: v for k, v in node.items() if k not in ("replies", "more_replies", "text_html")}
    out["reply_count"] = len(node.get("replies") or [])
    return out


def duplicates(post, cursor=None, limit=25):
    """Other submissions of the same link (crossposts and reposts)."""
    post_id = _pid(post)
    payload = _post_json(f"/duplicates/{post_id}", S.page_params(cursor, limit), post_id, S.post_subreddit(post),
                         f"post {post_id} duplicates")
    if not isinstance(payload, list) or not payload:
        raise RedditNotFound(f"post {post_id} not found", reason="post_not_found", post_id=post_id)
    post_children, _ = P.listing_children(payload[0])
    original = P.post(post_children[0]) if post_children else None
    listing = P.parse_listing(payload[1], P.post, "duplicates") if len(payload) > 1 else {"duplicates": [], "next_cursor": None, "has_more": False}
    return {"post": original, "duplicate_count": len(listing["duplicates"]), **listing}


def batch(posts):
    """Up to 100 posts (ids, fullnames or links) in one call."""
    ids = []
    for ref in posts:
        pid = S.post_id(ref)
        if pid and pid not in ids:
            ids.append(pid)
    things = S.info_by_ids_ordered([f"t3_{i}" for i in ids])
    found = [p for p in (P.post(t) for t in things) if p]
    seen = {p["id"] for p in found}
    return {"count": len(found), "posts": found, "missing": [i for i in ids if i not in seen]}


def by_url(url, cursor=None, limit=25):
    """Reddit submissions of an external URL: the search index's `url:`
    operator (/api/info?url= is walled since 2026-10-16), so very fresh
    submissions can lag a little."""
    params = S.page_params(cursor, limit, q=f"url:{url}", type="link", include_over_18="on")
    return S.listing("/search", params, P.post, "posts", url=url)


def media(post):
    """Just the post's downloadable media: images (originals + gif / mp4
    variants), gallery items in order, video renditions with the separate
    audio track and manifests, embeds, and the preview poster. Reddit-hosted
    videos are resolved against their DASH manifest, so every rendition and
    the audio link are the exact files v.redd.it serves for that video."""
    post_id = _pid(post)
    parsed = _post_only(post_id, S.post_subreddit(post))
    source = parsed.get("crosspost_parent") or parsed
    media_block = source.get("media")
    renditions, audio_tracks = _renditions(media_block)
    if audio_tracks and media_block and media_block.get("video"):
        media_block["video"]["audio_link"] = audio_tracks[0]["link"]
        media_block["video"]["has_audio"] = True
    return {
        "post": {"id": parsed["id"], "title": parsed["title"], "link": parsed["link"], "type": parsed["type"],
                 "external_link": parsed.get("external_link"), "is_nsfw": parsed["flags"].get("is_nsfw")},
        "media": media_block,
        "thumbnail": source.get("thumbnail"),
        "video_renditions": renditions,
        "audio_tracks": audio_tracks,
    }


def _renditions(media_block):
    """(video renditions, audio tracks) of a v.redd.it video from its DASH
    manifest; ([], []) for anything else or when the manifest is gone."""
    from .fetch import get_video_manifest
    video = (media_block or {}).get("video") if media_block else None
    link = (video or {}).get("link")
    if not link or "v.redd.it" not in link:
        return [], []
    base = link.rsplit("/", 1)[0]
    manifest = P.parse_dash_manifest(get_video_manifest(base), base)
    if not manifest:
        return [], []
    return manifest["videos"], manifest["audio"]


COMMENT_NEEDS_POST = ("a comment with `context` is read from its thread, which needs the post id: pass the "
                      "comment's permalink (reddit.com/r/<sub>/comments/<post>/.../<comment>/) or the comment id "
                      "together with `post`")


def comment_details(comment, context=0, post=None):
    """One comment with its author, body and stats — from its own shreddit
    partial (a bare id is enough); with `context` > 0 the comment comes with
    that many ancestors from the thread page, which needs the post id (from
    the permalink or the `post` parameter)."""
    cid = comment["id"]
    post_id = comment.get("post_id") or (_pid(post) if post else None)
    if not context:
        parsed = H.one(cid)
        if not parsed:
            raise RedditNotFound(f"comment {cid} not found", reason="comment_not_found", comment_id=cid, post_id=post_id)
        return {"comment": S.with_post_refs([parsed])[0]}
    if not post_id:
        raise RedditBadRequest(COMMENT_NEEDS_POST, reason="post_required")
    subreddit = comment.get("subreddit") or (S.post_subreddit(post) if post else None)
    parsed_post = _post_only(post_id, subreddit)
    page = H.focused(post_id, _sub_of(parsed_post, post or {}), cid, min(int(context), 10), "best")
    tree = page["comments"]
    if H._find(tree, cid) is None:
        raise RedditNotFound(f"comment {cid} not found", reason="comment_not_found", comment_id=cid, post_id=post_id)
    return {"post": parsed_post, "comment": tree[0]}
