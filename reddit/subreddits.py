"""Subreddit endpoints: details (+rules), posts, comment stream, search
within, rules, wiki, similar, sticky, batch, flairs (observed), plus the
directories (popular / new) and the explore leaderboard.

`subreddit` is {"name", "id"} from refs.resolve_subreddit. Anonymous gaps
(probed 2026-09-22): `/about/moderators` is 403, the flair template APIs
answer USER_REQUIRED, and `active_user_count` is null — the moderator list
is therefore not offered, flairs are derived from recent posts, and the
online count stays null in `details`.
"""
from collections import Counter

from . import parsers as P
from . import shared as S
from . import shreddit as H
from . import www
from .fetch import RedditNotFound, get_json, get_www, run_parallel


def _name(subreddit):
    return S.subreddit_name(subreddit)


def details(subreddit, include_rules=True):
    """The subreddit's public metadata, with its rules."""
    name = _name(subreddit)
    try:
        about = S.about_json("r", name)
    except RedditNotFound as e:
        if e.reason:
            e.details.setdefault("subreddit", name)
            raise
        raise RedditNotFound(f"r/{name} not found", reason="subreddit_not_found", subreddit=name)
    parsed = P.subreddit(about)
    if parsed is None:
        raise RedditNotFound(f"r/{name} not found", reason="subreddit_not_found", subreddit=name)
    out = {"subreddit": parsed}
    if include_rules:
        try:
            out["rules"] = P.rules(get_json(f"/r/{name}/about/rules", label=f"r/{name} rules"))["rules"]
        except RedditNotFound:
            out["rules"] = []
    return out


def posts(subreddit, sort="hot", time=None, cursor=None, limit=25):
    """The subreddit's post listing (hot / new / top / rising / controversial)."""
    name = _name(subreddit)
    params = S.page_params(cursor, limit, t=time if sort in ("top", "controversial") else None)
    out = S.listing(f"/r/{name}/{sort}", params, P.post, "posts")
    return {"subreddit": name, "sort": sort, "time": time if sort in ("top", "controversial") else None, **out}


def comments(subreddit, cursor=None, limit=25):
    """The subreddit's live comment stream (newest first), each comment
    with the post it was made on — from the www stream page through the
    local Chrome (oauth /r/<sub>/comments is walled since 2026-10-24). A
    refused page is explained by the subreddit's /about (gold-only,
    private, banned, unknown)."""
    name = _name(subreddit)
    try:
        out = H.comment_stream(name, cursor, limit)
    except RedditNotFound:
        S.about_json("r", name)            # raises the reasoned RedditNotFound when Reddit names one
        raise
    return {"subreddit": name, **out}


def search(subreddit, query, sort="relevance", time="all", cursor=None, limit=25, include_nsfw=False):
    """Posts matching `query` inside one subreddit (supports Reddit's
    search syntax: flair:Name, author:user, self:yes, url:..., title:...)."""
    name = _name(subreddit)
    params = S.page_params(cursor, limit, q=query, restrict_sr=1, sort=sort, t=time,
                           include_over_18="on" if include_nsfw else None)
    out = S.listing(f"/r/{name}/search", params, P.post, "posts")
    return {"subreddit": name, "query": query, "sort": sort, "time": time, **out}


def rules(subreddit):
    name = _name(subreddit)
    parsed = P.rules(get_json(f"/r/{name}/about/rules", label=f"r/{name} rules"))
    return {"subreddit": name, **parsed}


def wiki(subreddit, page=None):
    """One wiki page (default `index`), or with page=list the page names."""
    name = _name(subreddit)
    if page == "list":
        payload = get_json(f"/r/{name}/wiki/pages", label=f"r/{name} wiki pages")
        pages = payload.get("data") if isinstance(payload, dict) else None
        return {"subreddit": name, "pages": [p for p in (pages or []) if isinstance(p, str)]}
    page = page or "index"
    payload = get_json(f"/r/{name}/wiki/{page}", label=f"r/{name} wiki/{page}")
    parsed = P.wiki_page(payload, page)
    if parsed is None:
        raise RedditNotFound(f"r/{name} wiki page {page!r} not found")
    return {"subreddit": name, "page": parsed}


def similar(subreddit):
    """Subreddits Reddit recommends next to this one (/api/similar_subreddits)."""
    name = _name(subreddit)
    about = S.about_json("r", name)
    data = (about or {}).get("data") or {}
    fullname = data.get("name")
    if not fullname:
        raise RedditNotFound(f"r/{name} not found")
    # The answer depends on the bearer's device bucket: 2–5 in 8 freshly
    # minted app tokens get an empty listing for every subreddit (measured
    # twice on 2026-10-16), so an empty answer re-mints the process token and
    # asks again (6 tries ≈ 5% residual empties); a token that answers is kept.
    from .fetch import bearer
    attempts = 6
    for attempt in range(attempts):
        payload = get_json("/api/similar_subreddits", {"sr_fullnames": fullname}, label=f"r/{name} similar")
        out = P.parse_listing(payload, P.subreddit, "subreddits")
        if out["subreddits"] or attempt == attempts - 1:
            break
        bearer(force=True)
    return {"subreddit": name, "subreddit_count": len(out["subreddits"]), "subreddits": out["subreddits"]}


def sticky(subreddit, position=1):
    """A pinned post of the subreddit (position 1 or 2) with its top comments.
    /about/sticky redirects to the post's /comments/<id>, walled since
    2026-10-20, so the post is the position-th stickied post at the top of
    the hot listing, and its first comment page (25 top-level comments with
    their inline replies) comes from the shreddit tree partial in the local Chrome."""
    name = _name(subreddit)
    payload = get_json(f"/r/{name}/hot", {"limit": 5}, label=f"r/{name} sticky {position}")
    pinned = [c for c in P.listing_children(payload)[0] if (c.get("data") or {}).get("stickied")]
    post = P.post(pinned[position - 1]) if len(pinned) >= position else None
    if post is None:
        raise RedditNotFound(f"r/{name} has no sticky post at position {position}")
    page = H.tree(post["id"], name, "best", limit=0)
    return {"subreddit": name, "position": position, "post": post, "comments": page["comments"],
            "more_comments": page["more_comments"]}


def batch(subreddits):
    """Up to 100 subreddits by name in one call; unknown names are listed
    in `missing`."""
    names = [s["name"] for s in subreddits if s.get("name")]
    ids = [s["id"] for s in subreddits if s.get("id")]
    calls = []
    if names:
        calls.append(lambda: S.info_by_subreddit_names(names))
    if ids:
        calls.append(lambda: S.info_by_ids(ids))
    found = []
    for children in run_parallel(calls):
        for child in children:
            parsed = P.subreddit(child)
            if parsed:
                found.append(parsed)
    seen_names = {s["name"].lower() for s in found}
    seen_ids = {s["id"] for s in found if s.get("id")}
    missing = [n for n in names if n.lower() not in seen_names] + [i for i in ids if i.split("_", 1)[-1] not in seen_ids]
    out = {"count": len(found), "subreddits": found, "missing": missing}
    if ids:
        out["note"] = S.T5_IDS_UNRESOLVABLE
    return out


def flairs(subreddit, sample_size=100):
    """Post flairs in use, counted over the newest `sample_size` posts (the
    flair template API needs a logged-in user)."""
    name = _name(subreddit)
    out = S.listing(f"/r/{name}/new", {"limit": min(sample_size, 100)}, P.post, "posts")
    counter = Counter()
    examples = {}
    for post in out["posts"]:
        flair = post.get("flair") or {}
        key = flair.get("template_id") or flair.get("text")
        if not key:
            continue
        counter[key] += 1
        examples.setdefault(key, flair)
    flairs_out = []
    for key, count in counter.most_common():
        flair = dict(examples[key])
        flair["post_count"] = count
        flairs_out.append(flair)
    return {"subreddit": name, "sample_size": len(out["posts"]), "flair_count": len(flairs_out), "flairs": flairs_out}


# ---- directories ------------------------------------------------------------------------------

def popular(cursor=None, limit=25):
    """Reddit's popular-communities directory (/subreddits/popular)."""
    return S.listing("/subreddits/popular", S.page_params(cursor, limit), P.subreddit, "subreddits")


def new(cursor=None, limit=25):
    """Newly created communities (/subreddits/new)."""
    return S.listing("/subreddits/new", S.page_params(cursor, limit), P.subreddit, "subreddits")


def leaderboard(topic=None):
    """The www /explore/ communities leaderboard (weekly visitors), optionally
    for one topic (`topic` = a slug from `topics`, e.g. technology)."""
    path = "/explore/"
    if topic:
        page = get_www("/explore/", label="explore")
        topics = www.explore_topics(page)
        match = next((t for t in topics if t["slug"] == topic.lower()), None)
        if match is None:
            raise RedditNotFound(f"explore topic {topic!r} not found; one of: " + ", ".join(t["slug"] for t in topics))
        path = f"/explore/{match['id']}/{match['slug']}/"
    page = get_www(path, label="explore")
    cards = www.explore_cards(page)
    topics = www.explore_topics(page)
    by_name = {}
    names = [c["name"] for c in cards]
    for child in S.info_by_subreddit_names(names, best_effort=True):
        parsed = P.subreddit(child)
        if parsed:
            by_name[parsed["name"].lower()] = parsed
    out = []
    for rank, card in enumerate(cards, 1):
        full = by_name.get(card["name"].lower())
        entry = {"rank": rank, "weekly_visitor_count": card["weekly_visitor_count"]}
        if full:
            entry.update(full)
        else:
            entry.update({"id": card["id"], "name": card["name"], "link": f"https://www.reddit.com/r/{card['name']}/",
                          "description": card["description"]})
        out.append(entry)
    return {"topic": topic, "topics": topics, "count": len(out), "subreddits": out}
