"""The few www.reddit.com (shreddit) surfaces the JSON API has no anonymous
equivalent for, rendered with the app bearer (fetch.get_www):

  comment search   /svc/shreddit/search/?q=&type=comments[&sort=&t=&cursor=]
  media search     /svc/shreddit/search/?q=&type=media[&sort=&t=&cursor=]
  explore          /explore/ and /explore/<id>/<topic>/ — the communities
                   leaderboard (48 cards per page, weekly visitor counts)

The pages embed no JSON; every unit carries its ids in a
`data-faceplate-tracking-context` attribute (HTML-escaped JSON), so the
extractors read ids from those attributes and the callers hydrate them
through the JSON API (/api/info?id=… for comments and posts,
/api/info?sr_name=… for subreddits — one call per page). The next page's
cursor is the `cursor=` query of the trailing `<faceplate-partial
src="/svc/shreddit/search/?…">` loader.
"""
import html
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

_TRACKING_RE = re.compile(r'data-faceplate-tracking-context="([^"]*)"')
_NEXT_RE = re.compile(r'<faceplate-partial[^>]*src="([^"]*/svc/shreddit/search/\?[^"]*cursor=[^"]*)"')
_CARD_RE = re.compile(r"<community-recommendation\b(.*?)</community-recommendation>", re.S)
_NUMBER_RE = re.compile(r'<faceplate-number[^>]*number="(\d+)"')
_TOPIC_RE = re.compile(r'href="/explore/([A-Za-z0-9]+)/([a-z0-9_]+)/"')
_TAG_RE = re.compile(r"<[^>]+>")


def _contexts(page):
    """Every tracking-context attribute -> parsed dict (in page order)."""
    out = []
    for raw in _TRACKING_RE.findall(page or ""):
        try:
            payload = json.loads(html.unescape(raw))
        except ValueError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _unique(values):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def next_cursor(page):
    match = _NEXT_RE.search(page or "")
    if not match:
        return None
    query = parse_qs(urlsplit(html.unescape(match.group(1))).query)
    cursor = (query.get("cursor") or [None])[0]
    return unquote(cursor) if cursor else None


def search_comment_ids(page):
    """Comment fullnames (t1_…) of a comment-search page, in result order,
    with each one's post id."""
    ids = []
    for ctx in _contexts(page):
        comment = ctx.get("comment") or {}
        cid = comment.get("id")
        if isinstance(cid, str) and cid.startswith("t1_"):
            ids.append(cid)
    return _unique(ids)


def search_post_ids(page):
    """Post fullnames (t3_…) of a post / media search page, in order."""
    ids = []
    for ctx in _contexts(page):
        post = ctx.get("post") or {}
        pid = post.get("id")
        if isinstance(pid, str) and pid.startswith("t3_"):
            ids.append(pid)
    return _unique(ids)


def explore_cards(page):
    """/explore/ cards -> [{name, id, weekly_visitor_count, description}]."""
    out = []
    for body in _CARD_RE.findall(page or ""):
        ctx = None
        for candidate in _contexts(body):
            if candidate.get("subreddit"):
                ctx = candidate["subreddit"]
                break
        if not ctx or not ctx.get("name"):
            continue
        number = _NUMBER_RE.search(body)
        # the description is the last <p> of the card body
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)
        description = None
        for text in reversed(paragraphs):
            plain = html.unescape(_TAG_RE.sub(" ", text))
            plain = " ".join(plain.split())
            if plain and "visitors" not in plain and "members" not in plain:
                description = plain
                break
        sid = ctx.get("id")
        out.append({
            "name": ctx["name"],
            "id": sid.split("_", 1)[1] if isinstance(sid, str) and "_" in sid else sid,
            "weekly_visitor_count": int(number.group(1)) if number else None,
            "description": description,
        })
    return out


def explore_topics(page):
    """The topic tabs of /explore/ -> [{id, slug, name}]."""
    out = []
    seen = set()
    for topic_id, slug in _TOPIC_RE.findall(page or ""):
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"id": topic_id, "slug": slug, "name": slug.replace("_", " ").title().replace("Qandas", "Q&As").replace(" And ", " and ")})
    return out
