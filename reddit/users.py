"""User endpoints: details (+trophies), posts / comments / overview,
moderated subreddits, trophies, batch, active subreddits.

`user` is {"name", "id"} from refs.resolve_user. A suspended account is
still served by /user/<name>/about (with is_suspended); a deleted or
unknown one is a 404.
"""
from collections import Counter

from . import parsers as P
from . import shared as S
from .fetch import RedditNotFound, get_json, run_parallel


def _name(user):
    return S.username(user)


def _about(name):
    parsed = P.user(get_json(f"/user/{name}/about", label=f"u/{name}"))
    if parsed is None:
        raise RedditNotFound(f"u/{name} not found")
    return parsed


def details(user, include_trophies=True):
    """Karma split, cake day, avatar, profile (followers, bio), flags —
    with the trophy case unless include_trophies=false."""
    name = _name(user)
    parsed = _about(name)
    out = {"user": parsed}
    if include_trophies and not parsed["flags"].get("is_suspended"):
        try:
            out["trophies"] = _trophies(parsed["username"])
        except RedditNotFound:
            out["trophies"] = []
    return out


def _trophies(name):
    payload = get_json(f"/api/v1/user/{name}/trophies", label=f"u/{name} trophies")
    items = ((payload or {}).get("data") or {}).get("trophies") or []
    return [t for t in (P.trophy(i) for i in items) if t]


def trophies(user):
    name = _name(user)
    items = _trophies(name)
    return {"user": name, "trophy_count": len(items), "trophies": items}


def _history(name, kind, sort, time, cursor, limit, key, parser):
    ranked = sort in ("top", "controversial")
    window = (time or "all") if ranked else None
    params = S.page_params(cursor, limit, sort=sort, t=window)
    out = S.listing(f"/user/{name}/{kind}", params, parser, key)
    note = None
    if ranked and window == "all" and not cursor and not out[key]:
        # Upstream quirk (probed 2026-09-23): top / controversial over
        # `all` answers an EMPTY listing for long-lived prolific accounts
        # (u/spez) while every shorter window works. Serve the year window
        # and say so rather than returning nothing.
        params["t"] = "year"
        retry = S.listing(f"/user/{name}/{kind}", params, parser, key)
        if retry[key]:
            out, window = retry, "year"
            note = "Reddit returns no results for time=all on this account; served time=year instead"
    result = {"user": name, "sort": sort, "time": window, **out}
    if note:
        result["note"] = note
    return result


def posts(user, sort="new", time=None, cursor=None, limit=25):
    """The user's submissions."""
    return _history(_name(user), "submitted", sort, time, cursor, limit, "posts", P.post)


def comments(user, sort="new", time=None, cursor=None, limit=25):
    """The user's comments, each with the post it was made on."""
    return _history(_name(user), "comments", sort, time, cursor, limit, "comments", P.comment)


def overview(user, sort="new", time=None, cursor=None, limit=25):
    """Posts and comments interleaved; each item carries `kind`."""
    return _history(_name(user), "overview", sort, time, cursor, limit, "items", P.thing)


def moderated(user):
    """Subreddits the user moderates, with subscriber counts."""
    name = _name(user)
    payload = get_json(f"/user/{name}/moderated_subreddits", label=f"u/{name} moderated")
    items = (payload or {}).get("data") or [] if isinstance(payload, dict) else []
    subs = [s for s in (P.moderated_subreddit(i) for i in items) if s]
    return {"user": name, "subreddit_count": len(subs), "subreddits": subs}


def batch(users):
    """Up to 100 users in one call. Fullname (t2_) refs resolve through
    /api/user_data_by_account_ids in ONE request; names need one /about
    each (fanned out, so keep name lists short)."""
    ids = [u["id"] for u in users if u.get("id")]
    names = [u["name"] for u in users if u.get("name")]
    found = []
    missing = []
    if ids:
        payload = get_json("/api/user_data_by_account_ids", {"ids": ",".join(ids)}, label="user lookup")
        for uid in ids:
            entry = (payload or {}).get(uid) if isinstance(payload, dict) else None
            parsed = P.user_from_account_ids(uid, entry) if entry else None
            if parsed:
                found.append(parsed)
            else:
                missing.append(uid)

    def fetch_one(name):
        def call():
            try:
                return _about(name)
            except RedditNotFound:
                return None
        return call

    for name, parsed in zip(names, run_parallel([fetch_one(n) for n in names])):
        if parsed:
            found.append(parsed)
        else:
            missing.append(name)
    return {"count": len(found), "users": found, "missing": missing}


def active_subreddits(user, sample_size=100):
    """Where the user posts and comments most, counted over their newest
    `sample_size` overview items."""
    name = _name(user)
    out = S.listing(f"/user/{name}/overview", {"limit": min(sample_size, 100), "sort": "new"}, P.thing, "items")
    posts_by = Counter()
    comments_by = Counter()
    for item in out["items"]:
        sub = (item.get("subreddit") or {}).get("name")
        if not sub:
            continue
        (posts_by if item["kind"] == "post" else comments_by)[sub] += 1
    totals = Counter()
    totals.update(posts_by)
    totals.update(comments_by)
    ranked = [{"subreddit": sub, "link": f"https://www.reddit.com/r/{sub}/", "item_count": count,
               "post_count": posts_by.get(sub, 0), "comment_count": comments_by.get(sub, 0)}
              for sub, count in totals.most_common()]
    return {"user": name, "sample_size": len(out["items"]), "subreddit_count": len(ranked), "subreddits": ranked}
