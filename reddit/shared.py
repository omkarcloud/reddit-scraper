"""Helpers shared by the /reddit/* endpoint modules: resolving the id-only
forms of the ref params, listing calls, and the `time` / `sort` mappings.

Every list endpoint pages the Reddit way: `cursor` in (the `after`
fullname of the previous page), `next_cursor` / `has_more` out. `limit` is
Reddit's per-page cap of 100.
"""
import threading
import time

from . import parsers as P
from . import refs
from .fetch import RedditBadRequest, RedditForbidden, RedditNotFound, get_json, run_parallel
from .schemas import GEO_FILTERS, POST_SORTS, SEARCH_SORTS, TIME_FILTERS, USER_SORTS  # noqa: F401

LOOKUP_TTL = 1800
_lookup_lock = threading.Lock()
_lookups = {}           # ("t5"|"t2", id) -> (expires, name)

# /r/<name>/about and /user/<name>/about answers, memoised briefly: details,
# similar, batch, the explore leaderboard (<= 48 abouts) and the
# unavailable-post diagnosis all ask for the same object within one call or
# within minutes of each other, and the t5 / t2 payload only moves slowly.
# Successes only; capped so a listing sweep cannot pin memory.
ABOUT_TTL = 300
ABOUT_MEMO_MAX = 500
_about_lock = threading.Lock()
_abouts = {}            # ("r"|"user", name.lower()) -> (expires, payload)

# public comment sort -> Reddit's token (best is "confidence" upstream)
COMMENT_SORTS = {"best": "confidence", "top": "top", "new": "new", "controversial": "controversial",
                 "old": "old", "qa": "qa"}


def _remember(kind, key, name):
    with _lookup_lock:
        if len(_lookups) > 5000:
            _lookups.clear()
        _lookups[(kind, key)] = (time.time() + LOOKUP_TTL, name)


def _recall(kind, key):
    with _lookup_lock:
        hit = _lookups.get((kind, key))
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def about_json(kind, name, label=None):
    """GET /r/<name>/about (kind "r") or /user/<name>/about (kind "user")
    -> the parsed payload, from the ABOUT_TTL memo when the same object was
    fetched recently. Errors propagate and are never memoised."""
    key = (kind, str(name).lower())
    now = time.time()
    with _about_lock:
        hit = _abouts.get(key)
        if hit and hit[0] > now:
            return hit[1]
    payload = get_json(f"/{kind}/{name}/about", label=label or f"{'r' if kind == 'r' else 'u'}/{name}")
    with _about_lock:
        if len(_abouts) >= ABOUT_MEMO_MAX:
            _abouts.clear()
        _abouts[key] = (now + ABOUT_TTL, payload)
    return payload


T5_IDS_UNRESOLVABLE = ("Reddit no longer resolves subreddit ids (t5_...) for anonymous clients "
                       "(/api/info went behind its network-security wall on 2026-10-16); "
                       "pass the subreddit name or its reddit.com/r/... link instead")


def subreddit_name(ref):
    """{"name", "id"} -> the display name. A t5_ id has no anonymous lookup
    left (every /api/info form is walled, /subreddits/search?q=<t5 id> finds
    unrelated communities, www /r/<t5 id>/ renders the empty shell) so it is
    a 400 unless this process resolved it earlier."""
    if ref.get("name"):
        return ref["name"]
    sid = ref["id"]
    cached = _recall("t5", sid)
    if cached:
        return cached
    raise RedditBadRequest(T5_IDS_UNRESOLVABLE, reason="subreddit_id_unsupported")


def username(ref):
    """{"name", "id"} -> the username (a t2_ id is looked up once via
    /api/user_data_by_account_ids)."""
    if ref.get("name"):
        return ref["name"]
    uid = ref["id"]
    cached = _recall("t2", uid)
    if cached:
        return cached
    payload = get_json("/api/user_data_by_account_ids", {"ids": uid}, label=f"user {uid}")
    entry = (payload or {}).get(uid) if isinstance(payload, dict) else None
    name = (entry or {}).get("name")
    if not name:
        raise RedditNotFound(f"user {uid} not found")
    _remember("t2", uid, name)
    return name


def post_id(ref):
    """{"id", "share"} -> the post id. A share link (/r/<sr>/s/<code>) is
    resolved by asking oauth for the redirect target."""
    if ref.get("id"):
        return ref["id"]
    share = ref["share"]
    cached = _recall("share", share)
    if cached:
        return cached
    from .fetch import resolve_redirect
    target = resolve_redirect(share)
    if not target:
        raise RedditNotFound(f"share link {share} did not resolve to a post")
    try:
        resolved = refs.resolve_post(target)
    except ValueError:
        raise RedditNotFound(f"share link {share} did not resolve to a post")
    if not resolved.get("id"):
        raise RedditNotFound(f"share link {share} did not resolve to a post")
    _remember("share", share, resolved["id"])
    return resolved["id"]


def post_subreddit(ref):
    """The subreddit the caller's post link names (/r/<sr>/comments/…,
    /user/<name>/comments/… -> u_<name>, share links /r/<sr>/s/…), else
    None (a bare id)."""
    if ref.get("subreddit"):
        return ref["subreddit"]
    share = ref.get("share") or ""
    parts = [p for p in share.split("/") if p]
    if len(parts) >= 2 and parts[0] == "r":
        return parts[1]
    return None


# ---- why a post is unavailable ----------------------------------------------------------
# A post whose subreddit is banned / private / quarantined / gold-only, or
# whose author's profile is suspended or gone, answers /comments/<id> (and
# /duplicates/<id>, /by_id) with a bare 403 — no reason. Reddit names the
# reason only on the container, so it is asked there: /r/<sr>/about, or
# /user/<name>/about for a profile post (u_<name>, whose own about page
# stays 200 even when the account is suspended). A bare post id cannot be
# traced to its subreddit any more (/api/info is walled, www /comments/<id>
# 404s for these posts), hence "post_unavailable" with a hint.

POST_REASON_TEXT = {
    "subreddit_banned": "r/{subreddit} is banned",
    "subreddit_not_found": "r/{subreddit} does not exist",
    "subreddit_private": "r/{subreddit} is private (members only)",
    "subreddit_gold_only": "r/{subreddit} is gold only",
    "subreddit_quarantined": "r/{subreddit} is quarantined (hidden from anonymous readers)",
    "subreddit_gated": "r/{subreddit} is gated (hidden from anonymous readers)",
    "user_suspended": "its author's account u/{user} is suspended",
    "user_not_found": "its author's account u/{user} was not found (deleted or never existed)",
}


def unavailable_post(post_id, subreddit=None):
    """-> the RedditNotFound explaining why Reddit refuses post `post_id`
    (one extra call when the subreddit is known, none otherwise)."""
    base = f"post {post_id} is not publicly available"

    def err(reason, **where):
        text = POST_REASON_TEXT[reason].format(**where)
        return RedditNotFound(f"{base}: {text}", reason=reason, post_id=post_id, **where)

    if not subreddit:
        return RedditNotFound(
            f"{base}: it was removed by Reddit, or its subreddit or author account is banned, private, "
            f"quarantined or suspended. Pass the post's full link (reddit.com/r/<subreddit>/comments/{post_id}/...) "
            f"to get the exact reason", reason="post_unavailable", post_id=post_id)
    if subreddit.lower().startswith("u_"):
        user = subreddit[2:]
        try:
            about = about_json("user", user)
        except RedditForbidden:
            about = None
        except RedditNotFound:
            return err("user_not_found", user=user)
        data = (about or {}).get("data") or {} if isinstance(about, dict) else {}
        if data.get("is_suspended"):
            return err("user_suspended", user=data.get("name") or user)
        if about is None:
            # the profile itself is login-only: nothing more can be said anonymously
            return RedditNotFound(f"{base}: its author's profile u/{user} is not readable anonymously",
                                  reason="post_unavailable", post_id=post_id, user=user)
        return RedditNotFound(f"{base}: its author u/{user} is active, so Reddit removed the post or hid the profile",
                              reason="post_unavailable", post_id=post_id, user=user)
    try:
        about = about_json("r", subreddit)
    except RedditForbidden:
        about = None
    except RedditNotFound as e:
        reason = e.reason if e.reason in POST_REASON_TEXT else "subreddit_not_found"
        return err(reason, subreddit=subreddit)
    data = (about or {}).get("data") or {} if isinstance(about, dict) else {}
    name = data.get("display_name") or subreddit
    if data.get("subreddit_type") == "private":
        return err("subreddit_private", subreddit=name)
    if data.get("quarantine"):
        return err("subreddit_quarantined", subreddit=name)
    return RedditNotFound(f"{base}: r/{name} is public, so Reddit removed the post (or the link names the wrong subreddit)",
                          reason="post_unavailable", post_id=post_id, subreddit=name)


def listing(path, params, item_parser, key, **extra):
    """GET a Listing endpoint -> {key: [...], next_cursor, has_more, ...}."""
    payload = get_json(path, params, label=path)
    if not isinstance(payload, dict) or payload.get("kind") != "Listing":
        raise RedditBadRequest(f"{path} did not return a listing")
    return P.parse_listing(payload, item_parser, key, **extra)


def page_params(cursor=None, limit=None, **more):
    params = {"limit": limit or 25}
    if cursor:
        params["after"] = cursor
    params.update({k: v for k, v in more.items() if v is not None})
    return params


# ---- hydration by id ------------------------------------------------------------------------
# /api/info (id=, sr_name=, url=, .json, subreddit-scoped, www host) and
# /api/morechildren answer the anonymous app bearer with the 403 "blocked by
# network security" HTML since 2026-10-16 (every app client id, every UA).
# What still answers: /by_id/<t3,...> for posts, a comment's own thread
# /comments/<post>/_/<comment> for comments, /r/<name>/about for subreddits,
# /api/user_data_by_account_ids for users. Nothing resolves a bare comment id
# or a t5_ id any more.

def post_by_id(fullname):
    """/by_id/<one t3 fullname> -> the t3 thing, or None when Reddit does
    not serve it (unknown id = 404, removed / banned-subreddit post = an
    empty listing). Upstream failures propagate: reporting a live post as
    missing would be worse than a retryable 502."""
    try:
        payload = get_json("/by_id/" + fullname, {"limit": 1}, label="post lookup")
    except RedditNotFound:
        return None
    children, _ = P.listing_children(payload)
    return children[0] if children else None


def posts_by_ids(fullnames):
    """[t3 fullnames] -> [t3 things] (missing ids simply absent), one
    /by_id call per post, fanned out. Since 2026-10-19 Reddit answers a
    MULTI-id /by_id (`t3_a,t3_b`, any separator, with or without limit=)
    with its 403 network-security HTML for the app token, while a single id
    still answers 200 — so every post is looked up on its own."""
    if not fullnames:
        return []
    things = run_parallel([lambda f=f: post_by_id(f) for f in fullnames])
    return [t for t in things if t]


def comments_by_ids(comment_ids):
    """[comment id36] -> [parsed comments] in that order (missing absent):
    one shreddit single-comment partial each in the local Chrome
    (a comment's own JSON thread is walled since 2026-10-20, see shreddit.py),
    each with its parent-post summary (`post`, see with_post_refs)."""
    from . import shreddit as H
    if not comment_ids:
        return []
    found = run_parallel([lambda c=c: _comment_or_none(H, c) for c in comment_ids])
    return with_post_refs([c for c in found if c])


def with_post_refs(comments):
    """Add the parent-post summary (parsers.post_ref: id, title, link, author,
    comment count, NSFW) to parsed comments as `post`, one /by_id call per
    distinct post — the shreddit comment markup carries only the post id.
    A post Reddit no longer serves leaves `post` None."""
    ids = []
    for c in comments:
        if c.get("post_id") and c["post_id"] not in ids:
            ids.append(c["post_id"])
    posts = {}
    for thing in posts_by_ids([f"t3_{i}" for i in ids]):
        data = thing.get("data") or {}
        posts[data.get("id")] = P.post_ref({
            "link_id": f"t3_{data.get('id')}", "link_title": data.get("title"), "link_author": data.get("author"),
            "link_url": data.get("url"), "link_permalink": refs.post_link(data.get("permalink")),
            "num_comments": data.get("num_comments"), "over_18": data.get("over_18"),
        })
    for c in comments:
        c["post"] = posts.get(c.get("post_id"))
    return comments


def _comment_or_none(H, comment_id):
    try:
        return H.one(comment_id)
    except RedditNotFound:
        return None


def info_by_ids(fullnames, post_ids=None):
    """Hydrate t3_ fullnames -> [things] (missing absent) via /by_id, one
    call each. Comments are parsed, not things, since 2026-10-23: see
    comments_by_ids; t5_ / t2_ cannot be resolved this way any more (see
    subreddit_name)."""
    posts = [f for f in (fullnames or []) if f.startswith("t3_")]
    return posts_by_ids(posts) if posts else []


def info_by_ids_ordered(fullnames, post_ids=None):
    """info_by_ids, re-ordered to match `fullnames`."""
    children = info_by_ids(fullnames, post_ids)
    by_name = {}
    for child in children:
        data = child.get("data") or {}
        name = data.get("name")
        if name:
            by_name[name] = child
    return [by_name[f] for f in fullnames if f in by_name]


def subreddit_about(name, best_effort=False):
    """/r/<name>/about -> the t5 thing, or None when the community is
    missing / banned / private / gold-only. With best_effort, an upstream
    failure (rate limit, wall) is None too."""
    from .fetch import RedditUpstreamError
    try:
        payload = about_json("r", name, label=f"subreddit {name}")
    except (RedditNotFound, RedditBadRequest):
        return None
    except RedditUpstreamError:
        if best_effort:
            return None
        raise
    if isinstance(payload, dict) and payload.get("kind") == "t5":
        return payload
    return None


def info_by_subreddit_names(names, best_effort=False):
    """[names] -> [t5 things] in that order (unknown names absent): one
    /r/<name>/about each, fanned out (/api/info?sr_name= is walled)."""
    if not names:
        return []
    things = run_parallel([lambda n=n: subreddit_about(n, best_effort) for n in names])
    return [t for t in things if t]
