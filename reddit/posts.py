"""Post endpoints: details, the comment tree (+ `more` expansion and a
flat export), duplicates / crossposts, batch lookup, submissions of an
external URL, media download links, and single-comment details.

`post` is {"id", "share"} from refs.resolve_post; `comment` is {"id",
"post_id"} from refs.resolve_comment.

Comment trees: /comments/<id> returns the post and a nested listing whose
leaves are `more` nodes (Reddit loads at most `limit` comments per call,
500 max). Every `more` becomes a {count, cursor} the caller feeds to
/posts/comments/more; parsers.more_stub documents the cursor forms.
"""
from . import parsers as P
from . import shared as S
from .fetch import RedditBadRequest, RedditNotFound, get_json

COMMENT_LIMIT_MAX = 500
EXPORT_DEFAULT = 500
EXPORT_MAX = 2000
EXPORT_BATCH_CALLS = 40         # /api/morechildren calls an export may spend


def _pid(post):
    return S.post_id(post)


def _thread(post_id, sort, limit, depth, comment=None, context=None):
    params = {"sort": S.COMMENT_SORTS.get(sort, sort), "limit": limit, "depth": depth}
    if comment:
        params["comment"] = comment
        params["context"] = context if context is not None else 0
    payload = get_json(f"/comments/{post_id}", params, label=f"post {post_id}")
    if not isinstance(payload, list) or len(payload) < 2:
        raise RedditNotFound(f"post {post_id} not found")
    post_children, _ = P.listing_children(payload[0])
    parsed_post = P.post(post_children[0]) if post_children else None
    if parsed_post is None:
        raise RedditNotFound(f"post {post_id} not found")
    return parsed_post, P.listing_children(payload[1])[0]


def details(post):
    """Everything about one post (title, body, author, flair, stats,
    flags, media, poll, crosspost parent) — no comments."""
    post_id = _pid(post)
    parsed, _ = _thread(post_id, "best", 1, 0)
    return {"post": parsed}


def comments(post, sort="best", cursor=None, limit=100, depth=None, comment=None, context=0):
    """The post with its nested comment tree. `comment` focuses the tree on
    one comment (with `context` ancestors above it). Every branch Reddit
    left folded is a {count, cursor} `more_replies` for /posts/comments/more."""
    if cursor:
        page = more(cursor)
        return {"post": None, "sort": sort, "comment_count": _count(page["comments"]), **page}
    post_id = _pid(post)
    state = {"sort": sort, "limit": limit, "depth": depth}
    parsed_post, children = _thread(post_id, sort, limit, depth, comment=comment, context=context)
    tree, tail = P.comment_tree(children, post_id, state)
    return {"post": parsed_post, "sort": sort, "comment_count": _count(tree), "comments": tree, "more_comments": tail}


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
    post_id = state.get("post")
    sort = state.get("sort") or "best"
    if not post_id:
        raise RedditBadRequest("invalid cursor: pass the cursor value from the previous response unchanged")
    if state.get("focus"):
        _, children = _thread(post_id, sort, COMMENT_LIMIT_MAX, 10, comment=state["focus"], context=0)
        tree, tail = P.comment_tree(children, post_id, {"sort": sort, "limit": COMMENT_LIMIT_MAX, "depth": 10})
        focus = tree[0] if tree else None
        return {"comments": (focus or {}).get("replies") or [], "more_comments": (focus or {}).get("more_replies") or tail}
    if "skip" in state:
        limit = state.get("limit") or COMMENT_LIMIT_MAX
        depth = state.get("depth")
        _, children = _thread(post_id, sort, limit, depth)
        tail_ids = []
        for child in children:
            if child.get("kind") == "more" and (child.get("data") or {}).get("parent_id") == f"t3_{post_id}":
                tail_ids = [c for c in (child["data"].get("children") or []) if c]
        skip = int(state.get("skip") or 0)
        ids = tail_ids[skip:skip + P.MORE_BATCH]
        if not ids:
            return {"comments": [], "more_comments": None}
        roots, tail = _more_children(post_id, ids, sort)
        remaining = len(tail_ids) - (skip + len(ids))
        if remaining > 0:
            next_state = dict(state)
            next_state["skip"] = skip + len(ids)
            tail = {"count": remaining, "cursor": P.encode_cursor(next_state)}
        return {"comments": roots, "more_comments": tail}
    ids = state.get("ids") or []
    if not ids:
        raise RedditBadRequest("invalid cursor: pass the cursor value from the previous response unchanged")
    roots, tail = _more_children(post_id, ids, sort)
    return {"comments": roots, "more_comments": tail}


def _more_children(post_id, ids, sort):
    payload = get_json("/api/morechildren", {
        "link_id": f"t3_{post_id}", "children": ",".join(ids[:P.MORE_BATCH]),
        "api_type": "json", "sort": S.COMMENT_SORTS.get(sort, sort),
    }, label=f"post {post_id} more comments")
    things = (((payload or {}).get("json") or {}).get("data") or {}).get("things") or []
    return P.rebuild_tree(things, post_id, {"sort": sort})


def export(post, sort="best", max_comments=EXPORT_DEFAULT):
    """The whole thread as ONE flat, thread-ordered list (each comment
    followed by its replies, `depth` says how deep) with plain-text bodies —
    every folded branch is expanded until `max_comments`. Meant for
    summarisation / sentiment pipelines. `meta.calls` counts the upstream
    requests spent."""
    post_id = _pid(post)
    max_comments = min(max_comments or EXPORT_DEFAULT, EXPORT_MAX)
    parsed_post, children = _thread(post_id, sort, COMMENT_LIMIT_MAX, 10)
    calls = 1
    state = {"sort": sort, "limit": COMMENT_LIMIT_MAX, "depth": 10}
    tree, tail = P.comment_tree(children, post_id, state)
    # expand folded branches breadth-first until the budget is spent
    pending = []
    if tail:
        pending.append((None, tail))
    total = _count(tree)
    for node in _walk(tree):
        if node.get("more_replies"):
            pending.append((node, node["more_replies"]))
    truncated = False
    while pending and calls < EXPORT_BATCH_CALLS and total < max_comments:
        holder, stub = pending.pop(0)
        try:
            page = more(stub["cursor"])
        except RedditBadRequest:
            continue
        calls += 1 if "skip" not in P.decode_cursor(stub["cursor"]) else 2
        new_nodes = page["comments"]
        if holder is None:
            tree.extend(new_nodes)
        else:
            holder["replies"].extend(new_nodes)
            holder["more_replies"] = None
        total += _count(new_nodes)
        for node in _walk(new_nodes):
            if node.get("more_replies"):
                pending.append((node, node["more_replies"]))
        if page.get("more_comments"):
            pending.append((holder, page["more_comments"]))
    if pending or total > max_comments:
        truncated = True
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
        yield from _walk(node.get("replies") or [])


def _flat(node):
    out = {k: v for k, v in node.items() if k not in ("replies", "more_replies", "text_html")}
    out["reply_count"] = len(node.get("replies") or [])
    return out


def duplicates(post, cursor=None, limit=25):
    """Other submissions of the same link (crossposts and reposts)."""
    post_id = _pid(post)
    payload = get_json(f"/duplicates/{post_id}", S.page_params(cursor, limit), label=f"post {post_id} duplicates")
    if not isinstance(payload, list) or not payload:
        raise RedditNotFound(f"post {post_id} not found")
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
    """Every Reddit submission of an external URL (/api/info?url=)."""
    return S.listing("/api/info", S.page_params(cursor, limit, url=url), P.post, "posts", url=url)


def media(post):
    """Just the post's downloadable media: images (originals + gif / mp4
    variants), gallery items in order, video renditions with the separate
    audio track and manifests, embeds, and the preview poster. Reddit-hosted
    videos are resolved against their DASH manifest, so every rendition and
    the audio link are the exact files v.redd.it serves for that video."""
    post_id = _pid(post)
    parsed, _ = _thread(post_id, "best", 1, 0)
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


def comment_details(comment, context=0):
    """One comment with its author, body and stats (and `context`
    ancestors when > 0), found through /api/info or its thread."""
    cid = comment["id"]
    if not context:
        things = S.info_by_ids([f"t1_{cid}"])
        for thing in things:
            parsed = P.comment(thing, include_replies=False)
            if parsed:
                return {"comment": parsed}
        raise RedditNotFound(f"comment {cid} not found")
    post_id = comment.get("post_id")
    if not post_id:
        things = S.info_by_ids([f"t1_{cid}"])
        for thing in things:
            post_id = ((thing.get("data") or {}).get("link_id") or "").split("_", 1)[-1] or None
        if not post_id:
            raise RedditNotFound(f"comment {cid} not found")
    parsed_post, children = _thread(post_id, "best", COMMENT_LIMIT_MAX, 10, comment=cid, context=min(context, 10))
    tree, _ = P.comment_tree(children, post_id, {"sort": "best", "limit": COMMENT_LIMIT_MAX, "depth": 10})
    if not tree:
        raise RedditNotFound(f"comment {cid} not found")
    return {"post": parsed_post, "comment": tree[0]}
