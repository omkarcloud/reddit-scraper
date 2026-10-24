"""Comment trees from www.reddit.com's shreddit HTML, read through a local
Chrome (browser.py) as in-page fetches.

Why: oauth /comments/<id> answers the anonymous app bearer with Reddit's
network-security wall since 2026-10-20 20:00 UTC — every token, every exit
(residential and datacenter), every form of the path — while /by_id, /search
and the listings still answer. www.reddit.com renders the tree for a real
browser, and a parked browser's in-page fetches are served even when its
own navigation got the "Prove your humanity" page (measured 2026-10-23).

Surfaces (all proven 2026-10-23):

  GET  /svc/shreddit/comments/r/<sub>/t3_<id>?sort=<SORT>
        the tree partial: the first 25 top-level comments, replies inline
        for small threads, folded branches and the top-level tail as
        `more-comments` loaders (~250-650 KB)
  POST /svc/shreddit/more-comments/<sub>/t3_<id>?<loader query>
        body: cursor=<the loader's hidden input>&csrf_token=<cookie>
        -> the loader's comments: top-level pages of 10 then 100, branches
        of ~10 with their own deeper loaders (~200 KB / 2 MB)
  GET  /svc/shreddit/comment/t1_<id>?depth=0
        one comment, no post or subreddit needed (20 KB)
  GET  /svc/shreddit/comments/r/<sub>/t3_<post>/t1_<cid>?sort=<SORT>&context=N
        the tree focused on one comment: its chain of N ancestors with the
        comment (is-highlighted) and its replies first, then the thread's
        other top-level comments (dropped here, as the JSON API did)
Reddit does not check the subreddit name in these paths (any name works);
an unknown post gives an empty partial. The full permalink page is not
used: its comments sit in <template> elements lxml does not descend into.

Markup: each comment is a <shreddit-comment> with the attributes thingId,
author, depth, score, created, permalink, postId (replies nest INSIDE their
parent element); its body is div#<thingId>-post-rtjson-content. A folded
branch is a <faceplate-partial src=".../more-comments/..." method="post">
inside the parent comment with <input name="cursor">; the top-level tail is
the same element outside every comment (top-level=1). Parsed with lxml.html
(bs4's parsers mis-nest or skip the custom elements).

Cursors (/posts/comments/more): {"post", "sr", "sort", "src", "cur",
"parent"?, "n"?} for a loader (replayed as the POST above), {"post", "sr",
"sort", "focus", "parent"?} for a "continue this thread" deep link (the
focused page, replies of `focus`). Older JSON-API cursors are rejected.
"""
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, quote, urlsplit

import lxml.html

from . import parsers as P
from . import refs
from .browser import page_fetch
from .fetch import RedditBadRequest, RedditNotFound

WWW = "https://www.reddit.com"
PARTIAL_ACCEPT = "text/vnd.reddit.partial+html, text/html;q=0.9"
SORTS = {"best": "CONFIDENCE", "top": "TOP", "new": "NEW", "controversial": "CONTROVERSIAL", "old": "OLD", "qa": "QA"}
TOP_PAGE_MAX = 5                # top-level pages followed for one /posts/comments call (25 + 10 + 100 + 100 ...)
_BLOCK_TAGS = {"p", "div", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


# ---- fetchers (each returns a parsed page) --------------------------------------------------------

def tree(post_id, subreddit, sort="best", limit=100):
    """The post's tree: the tree partial, then top-level pages until `limit`
    top-level comments are in (or the tail is gone) -> {comments, more_comments}."""
    status, html = page_fetch(f"/svc/shreddit/comments/r/{subreddit or 'reddit'}/t3_{post_id}?sort={SORTS.get(sort, 'CONFIDENCE')}",
                             f"post {post_id} comments")
    page = parse_tree(html, post_id, subreddit, sort)
    if status == 404 or (not page["comments"] and not page["more_comments"] and not html.strip()):
        raise RedditNotFound(f"post {post_id} not found", reason="post_not_found", post_id=post_id)
    pages = 0
    while page["more_comments"] and len(page["comments"]) < limit and pages < TOP_PAGE_MAX:
        more = expand(P.decode_cursor(page["more_comments"]["cursor"]))
        page["comments"] += more["comments"]
        page["more_comments"] = more["more_comments"]
        pages += 1
    return page


def expand(state):
    """One cursor -> {comments, more_comments}: a loader is POSTed back, a
    deep link reads the focused page and returns that comment's replies."""
    post_id, sub, sort = state.get("post"), state.get("sr"), state.get("sort") or "best"
    if not post_id or not (state.get("src") or state.get("focus")):
        raise RedditBadRequest("cursor expired: comment cursors issued before 2026-10-23 cannot be replayed; "
                               "re-read /posts/comments and use its new cursors")
    if state.get("src"):
        body = f"cursor={quote(state['cur'], safe='')}&csrf_token="
        _, html = page_fetch(state["src"], f"post {post_id} more comments", method="POST", body=body)
        page = parse_tree(html, post_id, sub, sort, parent=state.get("parent"))
        return {"comments": page["comments"], "more_comments": page["more_comments"]}
    page = focused(post_id, sub, state["focus"], 0, sort)
    node = _find(page["comments"], state["focus"])
    if node is None:
        return {"comments": [], "more_comments": None}
    return {"comments": node.get("replies") or [], "more_comments": node.get("more_replies")}


def focused(post_id, subreddit, comment_id, context, sort="best"):
    """The tree focused on `comment_id`: the one root chain that holds it
    (`context` ancestors, the comment, its replies) -> {comments, more_comments}
    with comments == [] when the comment is not in that post."""
    _, html = page_fetch(f"/svc/shreddit/comments/r/{subreddit or 'reddit'}/t3_{post_id}/t1_{comment_id}"
                        f"?sort={SORTS.get(sort, 'CONFIDENCE')}&context={int(context or 0)}", f"comment {comment_id}")
    page = parse_tree(html, post_id, subreddit, sort)
    chain = [root for root in page["comments"] if _find([root], comment_id)]
    return {"comments": chain[:1], "more_comments": None}


def one(comment_id):
    """One comment by id (no post needed) -> parsed comment or None."""
    status, html = page_fetch(f"/svc/shreddit/comment/t1_{comment_id}?depth=0", f"comment {comment_id}")
    if status == 404:
        return None
    page = parse_tree(html, None, None, "best")
    return page["comments"][0] if page["comments"] else None


def comment_stream(subreddit, cursor=None, limit=25):
    """A subreddit's live comment stream (newest first) -> {comments,
    next_cursor, has_more}. oauth /r/<sub>/comments answers the wall since
    2026-10-24 (every token, every exit) while www.reddit.com still serves
    the stream page in the legacy (old-Reddit) layout to a real browser;
    `after=<t1 fullname>` pages it like the listing did. A 403 / 404
    page (private, gold-only, banned, unknown) is a RedditNotFound."""
    query = f"?limit={int(limit or 25)}" + (f"&after={quote(cursor, safe='')}" if cursor else "")
    status, html = page_fetch(f"/r/{subreddit}/comments/{query}", f"r/{subreddit} comments", partial=False)
    if status in (403, 404):
        raise RedditNotFound(f"r/{subreddit} comments not found", reason="subreddit_not_found", subreddit=subreddit)
    return parse_stream(html)


def parse_stream(html):
    """The legacy comment-stream page -> {comments, next_cursor, has_more}.
    Each comment is a div.thing.comment with data-* attributes (id, author,
    author id, subreddit, permalink), the parent post line (p.parent: title,
    its link, author), the tagline (score, time, OP / distinguished /
    edited markers) and the body (div.md)."""
    root = _root(html)
    comments = []
    for el in root.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " thing ")][@data-type="comment"]'):
        parsed = _stream_comment(el)
        if parsed:
            comments.append(parsed)
    nxt = None
    for href in root.xpath('//span[@class="next-button"]/a/@href') or root.xpath('//a[contains(@href, "after=t1_")]/@href'):
        m = re.search(r"[?&]after=(t1_[a-z0-9]+)", href)
        if m:
            nxt = m.group(1)
            break
    return {"comments": comments, "next_cursor": nxt, "has_more": bool(nxt)}


def _stream_comment(el):
    cid = refs.strip_kind(el.get("data-fullname"))
    if not cid:
        return None
    permalink = el.get("data-permalink")
    m = re.search(r"/comments/([a-z0-9]+)/", permalink or "")
    pid = m.group(1) if m else None
    sub = el.get("data-subreddit")
    author_name = el.get("data-author")
    author = P.author({"author": author_name, "author_fullname": el.get("data-author-fullname")})
    tagline = (el.xpath('.//div[contains(@class, "entry")]/p[contains(@class, "tagline")]') or [None])[0]
    tag_author = (tagline.xpath('./a[contains(@class, "author")]') if tagline is not None else []) or [None]
    author_cls = (tag_author[0].get("class") or "") if tag_author[0] is not None else ""
    score = (tagline.xpath('./span[contains(@class, "score") and contains(@class, "unvoted")]/@title') if tagline is not None else [])
    times = tagline.xpath("./time") if tagline is not None else []
    created = next((t.get("datetime") for t in times if "edited-timestamp" not in (t.get("class") or "")), None)
    edited = next((t.get("datetime") for t in times if "edited-timestamp" in (t.get("class") or "")), None)
    body = el.xpath('.//form[contains(@class, "usertext")]//div[@class="md"]')
    text, text_html = _body(body[0]) if body else (None, None)
    score_v = P.to_int(score[0]) if score else None
    distinguished = next((d for d in ("admin", "moderator") if d in author_cls.split()), None)
    deleted = author_name in P._MISSING_AUTHORS or text in ("[deleted]", "[removed]")
    return {
        "id": cid,
        "link": refs.comment_link(permalink, pid, cid),
        "post_id": pid,
        "parent_comment_id": None,
        "depth": None,
        "text": text,
        "text_html": text_html,
        "created_at": _iso(created),
        "edited_at": _iso(edited),
        "subreddit": {"name": sub, "id": refs.strip_kind(el.get("data-subreddit-fullname")), "link": refs.subreddit_link(sub)} if sub else None,
        "author": author,
        "stats": {"score": score_v, "is_score_hidden": score_v is None, "controversiality": None},
        "flags": {
            "is_submitter": "submitter" in author_cls.split(),
            "is_stickied": "stickied" in (el.get("class") or "").split(),
            "is_locked": None,
            "is_archived": None,
            "is_collapsed": "collapsed" in (el.get("class") or "").split(),
            "is_edited": edited is not None,
            "is_deleted": deleted,
        },
        "collapsed_reason": None,
        "distinguished": distinguished,
        "media": _media(body[0]) if body else [],
        "post": _stream_post(el, pid, permalink),
        "replies": [],
        "more_replies": None,
    }


def _stream_post(el, post_id, permalink):
    """The p.parent line of a stream comment -> parsers.post_ref shape."""
    line = (el.xpath('./p[contains(@class, "parent")]') or [None])[0]
    if line is None or not post_id:
        return None
    title = (line.xpath('./a[contains(@class, "title")]') or [None])[0]
    href = title.get("href") if title is not None else None
    author = (line.xpath('./a[contains(@class, "author")]/text()') or [None])[0]
    post_link = refs.post_link(permalink.rsplit("/", 2)[0] + "/") if permalink else refs.post_link(None, post_id)
    full = el.xpath('.//a[@data-event-action="full_comments"]/text()')
    count = re.search(r"\((\d+)\)", full[0]) if full else None
    return {
        "id": post_id,
        "title": title.text_content().strip() if title is not None else None,
        "link": post_link,
        "external_link": href if href and href.startswith("http") and "reddit.com/" not in href else None,
        "author": {"username": author, "link": refs.user_link(author)} if author and author not in P._MISSING_AUTHORS else None,
        "comment_count": int(count.group(1)) if count else None,
        "is_nsfw": bool(line.xpath('.//*[contains(@class, "nsfw-stamp")]')),
    }


def _find(nodes, comment_id):
    for node in nodes or []:
        if node.get("id") == comment_id:
            return node
        hit = _find(node.get("replies"), comment_id)
        if hit:
            return hit
    return None


# ---- parsing ------------------------------------------------------------------------------------

def _root(html):
    text = html or ""
    if "<html" in text[:4000].lower():
        return lxml.html.document_fromstring(text)
    return lxml.html.fragment_fromstring(text, create_parent="div")


def parse_tree(html, post_id, subreddit, sort, parent=None):
    """shreddit HTML (a page or a partial) -> {"comments": nested [...],
    "more_comments": stub | None}. `parent` is the id of the comment a
    branch partial's root comments reply to (known only from the cursor)."""
    root = _root(html)
    nodes = {}
    roots = []
    for el in root.iter("shreddit-comment"):
        parsed = _comment(el, post_id, subreddit)
        if parsed is None:
            continue
        nodes[el] = parsed
        anc = el.xpath("ancestor::shreddit-comment[1]")
        if anc and anc[0] in nodes:
            parsed["parent_comment_id"] = nodes[anc[0]]["id"]
            nodes[anc[0]]["replies"].append(parsed)
        else:
            if parent and parsed.get("parent_comment_id") is None and (parsed.get("depth") or 0) > 0:
                parsed["parent_comment_id"] = parent
            roots.append(parsed)
    tail = None
    pid = post_id or next((n["post_id"] for n in nodes.values() if n.get("post_id")), None)
    for loader in root.xpath('//faceplate-partial[contains(@src, "/more-comments/")]'):
        src = unescape(loader.get("src") or "")
        cur = (loader.xpath('./input[@name="cursor"]/@value') or [None])[0]
        if not src or cur is None:
            continue
        number = (loader.xpath(".//faceplate-number/@number") or [None])[0]
        count = P.to_int(number)
        if count is None:
            count = P.to_int((parse_qs(urlsplit(src).query).get("comments-remaining") or [None])[0])
        anc = loader.xpath("ancestor::shreddit-comment[1]")
        payload = {"post": pid, "sr": subreddit, "sort": sort, "src": src, "cur": cur}
        if anc and anc[0] in nodes:
            payload["parent"] = nodes[anc[0]]["id"]
            if nodes[anc[0]]["more_replies"] is None:
                nodes[anc[0]]["more_replies"] = {"count": count, "cursor": P.encode_cursor(payload)}
        elif tail is None:
            if parent:
                payload["parent"] = parent
            tail = {"count": count, "cursor": P.encode_cursor(payload)}
    # "continue this thread" deep links outside any loader (the depth limit)
    for a in root.xpath('//a[@slot="more-comments-permalink"][not(ancestor::faceplate-partial)]'):
        number = (a.xpath(".//faceplate-number/@number") or [None])[0]
        anc = a.xpath("ancestor::shreddit-comment[1]")
        m = re.search(r"/comment/([a-z0-9]+)/", a.get("href") or "")
        if number is None or not m or not anc or anc[0] not in nodes or nodes[anc[0]]["more_replies"] is not None:
            continue
        payload = {"post": pid, "sr": subreddit, "sort": sort, "focus": m.group(1), "parent": nodes[anc[0]]["id"]}
        nodes[anc[0]]["more_replies"] = {"count": P.to_int(number), "cursor": P.encode_cursor(payload)}
    return {"comments": roots, "more_comments": tail}


def _comment(el, post_id, subreddit):
    tid = el.get("thingid") or ""
    cid = refs.strip_kind(tid) if tid.startswith("t1_") else None
    if not cid:
        return None
    pid = refs.strip_kind(el.get("postid")) or post_id
    permalink = el.get("permalink")
    sub = subreddit
    if permalink and permalink.startswith("/r/"):
        sub = permalink.split("/")[2]
    body = el.xpath(f'.//div[@id="{tid}-post-rtjson-content"]')
    text, text_html = _body(body[0]) if body else (None, None)
    author_name = el.get("author")
    author = P.author({"author": author_name})
    score = P.to_int(el.get("score"))
    deleted = (author_name in P._MISSING_AUTHORS) or (text in ("[deleted]", "[removed]"))
    return {
        "id": cid,
        "link": refs.comment_link(permalink, pid, cid),
        "post_id": pid,
        "parent_comment_id": None,
        "depth": P.to_int(el.get("depth")),
        "text": text,
        "text_html": text_html,
        "created_at": _iso(el.get("created")),
        "edited_at": None,
        "subreddit": {"name": sub, "id": None, "link": refs.subreddit_link(sub)} if sub else None,
        "author": author,
        "stats": {"score": score, "is_score_hidden": score is None, "controversiality": None},
        "flags": {
            "is_submitter": None,
            "is_stickied": "stickied" in el.attrib or None,
            "is_locked": None,
            "is_archived": None,
            "is_collapsed": "collapsed" in el.attrib,
            "is_edited": None,
            "is_deleted": deleted,
        },
        "collapsed_reason": None,
        "distinguished": el.get("distinguished"),
        "media": _media(body[0]) if body else [],
        "award_count": P.to_int(el.get("award-count")),
        "replies": [],
        "more_replies": None,
    }


def _iso(value):
    """'2026-09-23T05:20:30.283000+0000' -> '2026-09-23T05:20:30Z'."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(div):
    """The rtjson content div -> (plain text with paragraph breaks, inner HTML)."""
    html = (div.text or "") + "".join(lxml.html.tostring(child, encoding="unicode") for child in div)
    clone = lxml.html.fragment_fromstring(lxml.html.tostring(div, encoding="unicode"))
    for el in clone.iter():
        if el.tag == "br":
            el.text = "\n"
        elif el.tag in _BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
    text = clone.text_content()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (text or None), (html.strip() or None)


def _media(div):
    """Images and gifs embedded in a comment body -> [{type, link}]."""
    out = []
    for img in div.iter("img"):
        src = img.get("src") or ""
        if not src.startswith("http") or "emote" in (img.get("class") or "") or "/emoji/" in src:
            continue
        out.append({"type": "gif" if src.endswith(".gif") or "giphy" in src else "image", "link": src,
                    "alt": img.get("alt") or None})
    return out
