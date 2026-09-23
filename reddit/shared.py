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
from .fetch import RedditBadRequest, RedditNotFound, get_json
from .schemas import GEO_FILTERS, POST_SORTS, SEARCH_SORTS, TIME_FILTERS, USER_SORTS  # noqa: F401

LOOKUP_TTL = 1800
_lookup_lock = threading.Lock()
_lookups = {}           # ("t5"|"t2", id) -> (expires, name)

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


def subreddit_name(ref):
    """{"name", "id"} -> the display name (a t5_ id is looked up once via
    /api/info)."""
    if ref.get("name"):
        return ref["name"]
    sid = ref["id"]
    cached = _recall("t5", sid)
    if cached:
        return cached
    payload = get_json("/api/info", {"id": sid}, label=f"subreddit {sid}")
    children, _ = P.listing_children(payload)
    for child in children:
        data = child.get("data") or {}
        if child.get("kind") == "t5" and data.get("display_name"):
            _remember("t5", sid, data["display_name"])
            return data["display_name"]
    raise RedditNotFound(f"subreddit {sid} not found")


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


def info_by_ids(fullnames):
    """/api/info?id=… for up to 100 fullnames -> [things] in the API's
    order (missing ids are simply absent)."""
    if not fullnames:
        return []
    payload = get_json("/api/info", {"id": ",".join(fullnames)}, label="lookup")
    children, _ = P.listing_children(payload)
    return children


def info_by_ids_ordered(fullnames):
    """info_by_ids, re-ordered to match `fullnames`."""
    children = info_by_ids(fullnames)
    by_name = {}
    for child in children:
        data = child.get("data") or {}
        name = data.get("name")
        if name:
            by_name[name] = child
    return [by_name[f] for f in fullnames if f in by_name]


def info_by_subreddit_names(names):
    """/api/info?sr_name=… -> [t5 things]."""
    if not names:
        return []
    payload = get_json("/api/info", {"sr_name": ",".join(names)}, label="subreddit lookup")
    children, _ = P.listing_children(payload)
    return children
