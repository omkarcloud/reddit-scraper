"""Search and feed endpoints: post / subreddit / user search and
autocomplete on the JSON API; comment and media search through the www
search partial (ids from the HTML, hydrated with /api/info); the popular
feed per country, r/all and the anonymous front page.

Search sorts: relevance | hot | top | new | comments; `time` applies to
top-sorted (and any post) searches. Reddit's search syntax passes through
(subreddit:python, author:spez, flair:Showcase, self:yes, url:..., nsfw:no).
"""
from . import parsers as P
from . import shared as S
from . import www
from .fetch import RedditBadRequest, get_json, get_www

WWW_SEARCH_SORTS = {"relevance": "relevance", "new": "new", "top": "top"}


def _search(kind, query, sort, time, cursor, limit, include_nsfw, subreddit=None, key="posts", parser=P.post):
    params = S.page_params(cursor, limit, q=query, type=kind, sort=sort, t=time,
                           include_over_18="on" if include_nsfw else None)
    path = "/search"
    if subreddit:
        params["restrict_sr"] = 1
        path = f"/r/{subreddit}/search"
    out = S.listing(path, params, parser, key)
    return {"query": query, "sort": sort, "time": time, **out}


def search_posts(query, sort="relevance", time="all", subreddit=None, cursor=None, limit=25, include_nsfw=False):
    """Posts matching `query` across Reddit, or inside `subreddit`."""
    name = S.subreddit_name(subreddit) if subreddit else None
    out = _search("link", query, sort, time, cursor, limit, include_nsfw, subreddit=name)
    out["subreddit"] = name
    return out


def search_subreddits(query, cursor=None, limit=25, include_nsfw=False):
    """Communities matching `query` (full subreddit objects)."""
    return _search("sr", query, "relevance", None, cursor, limit, include_nsfw, key="subreddits", parser=P.subreddit)


def search_users(query, cursor=None, limit=25, include_nsfw=False):
    """Accounts matching `query` (karma, cake day, profile)."""
    return _search("user", query, "relevance", None, cursor, limit, include_nsfw, key="users", parser=P.user)


def autocomplete(query, limit=10, include_nsfw=False, include_profiles=True):
    """Reddit's typeahead: subreddits (and profiles) for a prefix."""
    payload = get_json("/api/subreddit_autocomplete_v2", {
        "query": query, "limit": limit, "include_over_18": 1 if include_nsfw else 0,
        "include_profiles": 1 if include_profiles else 0, "typeahead_active": "true",
    }, label="autocomplete")
    children, _ = P.listing_children(payload)
    items = []
    for child in children:
        if child.get("kind") == "t5":
            parsed = P.subreddit_compact(child)
            if parsed:
                items.append({"kind": "subreddit", **parsed})
        elif child.get("kind") == "t2":
            parsed = P.user(child)
            if parsed:
                items.append({"kind": "user", **parsed})
    return {"query": query, "count": len(items), "results": items}


def _www_search(kind, query, sort, time, cursor):
    params = {"q": query, "type": kind}
    if sort and sort != "relevance":
        params["sort"] = WWW_SEARCH_SORTS.get(sort, sort)
    if time and time != "all":
        params["t"] = time
    if cursor:
        params["cursor"] = cursor
    return get_www("/svc/shreddit/search/", params, label=f"{kind} search")


def search_comments(query, sort="relevance", time="all", cursor=None):
    """Comments matching `query` (the www Comments tab: ~9 per page)."""
    page = _www_search("comments", query, sort, time, cursor)
    ids = www.search_comment_ids(page)
    things = S.info_by_ids_ordered(ids) if ids else []
    comments = [c for c in (P.comment(t, include_replies=False) for t in things) if c]
    next_cursor = www.next_cursor(page) if ids else None
    return {"query": query, "sort": sort, "time": time, "comment_count": len(comments), "comments": comments,
            "next_cursor": next_cursor, "has_more": bool(next_cursor)}


def search_media(query, sort="relevance", time="all", cursor=None):
    """Image / video / gallery posts matching `query` (the www Media tab)."""
    page = _www_search("media", query, sort, time, cursor)
    ids = www.search_post_ids(page)
    things = S.info_by_ids_ordered(ids) if ids else []
    posts = [p for p in (P.post(t) for t in things) if p]
    next_cursor = www.next_cursor(page) if ids else None
    return {"query": query, "sort": sort, "time": time, "post_count": len(posts), "posts": posts,
            "next_cursor": next_cursor, "has_more": bool(next_cursor)}


# ---- feeds ---------------------------------------------------------------------------------

def popular(country="GLOBAL", sort="hot", time=None, cursor=None, limit=25):
    """r/popular for a country (GLOBAL, US, GB, CA, AU, DE, FR, IN, JP, MX,
    ES, IT, SE, PL, TR, AR — Reddit's own list; anything else silently
    falls back to US upstream, so the schema rejects it)."""
    params = S.page_params(cursor, limit, geo_filter=country, t=time if sort in ("top", "controversial") else None)
    payload = get_json(f"/r/popular/{sort}", params, label="popular")
    out = P.parse_listing(payload, P.post, "posts")
    echoed = ((payload or {}).get("data") or {}).get("geo_filter")
    return {"country": echoed or country, "sort": sort, "time": params.get("t"), **out}


def all_posts(sort="hot", time=None, cursor=None, limit=25):
    """r/all — every public subreddit in one feed."""
    params = S.page_params(cursor, limit, t=time if sort in ("top", "controversial") else None)
    out = S.listing(f"/r/all/{sort}", params, P.post, "posts")
    return {"sort": sort, "time": params.get("t"), **out}


def best(cursor=None, limit=25):
    """The logged-out front page (/best)."""
    return S.listing("/best", S.page_params(cursor, limit), P.post, "posts")
