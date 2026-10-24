"""Offline tests for the shreddit comment-tree path (reddit/shreddit.py +
reddit/posts.py): parsing the www partials captured on 2026-10-23, the
loader cursors, the local-Chrome transport's retries, and the endpoints
wired over a fake browser. No network.

    python -m pytest reddit/test_shreddit.py -q
"""
import json
import os
import sys
import time
from urllib.parse import parse_qs, unquote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from reddit import browser as B  # noqa: E402
from reddit import parsers as P, posts, shared as S, shreddit as H  # noqa: E402
from reddit.fetch import RedditBadRequest, RedditBlocked, RedditNotFound  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


SMALL = _fx("shreddit_thread_small.html")        # r/Python 1wn1d94: 11 comments, depth 2, no folds
TOP = _fx("shreddit_more_top.html")              # AskReddit 1wnwlhv top-level page: 10 comments, 10 folded branches, a tail
BRANCH = _fx("shreddit_more_branch.html")        # one folded branch of 1wnwlhv: 10 comments at depth 1-2
ONE = _fx("shreddit_comment_one.html")           # /svc/shreddit/comment/t1_pbefx01
STREAM = _fx("legacy_sr_comments.html")          # www /r/technology/comments/?limit=5 (legacy layout), 5 comments + next


def _walk(nodes):
    for n in nodes or []:
        yield n
        yield from _walk(n.get("replies"))


# ---- parsing --------------------------------------------------------------------------------

def test_small_thread_nests_replies_inside_their_parents():
    page = H.parse_tree(SMALL, "1wn1d94", "Python", "new")
    roots = page["comments"]
    assert [c["id"] for c in roots] == ["pbefx01", "pbcrehs", "pbcl3x9", "pbbthh2"]
    assert len(list(_walk(roots))) == 11 and page["more_comments"] is None
    thread = roots[1]
    assert [r["id"] for r in thread["replies"]] == ["pbe12oz", "pbdol0a", "pbd0ut2"]
    deep = thread["replies"][1]["replies"]
    assert [(r["id"], r["depth"], r["parent_comment_id"]) for r in deep] == [("pbe9d3h", 2, "pbdol0a"), ("pbdtuak", 2, "pbdol0a")]
    assert all(c["more_replies"] is None for c in _walk(roots))      # the hidden permalink anchors are not stubs


def test_comment_fields_match_the_json_api_shape():
    c = H.parse_tree(SMALL, "1wn1d94", "Python", "new")["comments"][0]
    assert c["id"] == "pbefx01" and c["post_id"] == "1wn1d94" and c["depth"] == 0
    assert c["link"] == "https://www.reddit.com/r/Python/comments/1wn1d94/comment/pbefx01/"
    assert c["created_at"] == "2026-09-22T17:07:35Z"
    assert c["author"]["username"] == "ram-popular" and c["author"]["link"].endswith("/user/ram-popular/")
    assert c["subreddit"] == {"name": "Python", "id": None, "link": "https://www.reddit.com/r/Python/"}
    assert c["stats"] == {"score": 4, "is_score_hidden": False, "controversiality": None}
    assert c["text"].startswith("Big deal for Python devs") and "FastAPI/Django support" in c["text"]
    assert c["text_html"].lstrip().startswith("<p>")
    assert c["flags"]["is_deleted"] is False and c["flags"]["is_collapsed"] is False
    assert c["media"] == [] and c["replies"] == [] and c["more_replies"] is None
    for key in ("edited_at", "collapsed_reason", "distinguished", "parent_comment_id"):
        assert c[key] is None
    multi = next(x for x in _walk(H.parse_tree(SMALL, "1wn1d94", "Python", "new")["comments"]) if x["id"] == "pbd0ut2")
    assert "\n\n" in multi["text"]                     # paragraphs survive as blank lines


def test_top_level_page_has_folded_branches_and_a_tail_cursor():
    page = H.parse_tree(TOP, "1wnwlhv", "AskReddit", "best")
    roots = page["comments"]
    assert len(roots) == 9 and len(list(_walk(roots))) == 10
    olive = roots[1]
    assert olive["id"] == "pbic8om" and olive["stats"]["score"] == 799 and olive["more_replies"]["count"] == 12
    state = P.decode_cursor(olive["more_replies"]["cursor"])
    assert state["post"] == "1wnwlhv" and state["sr"] == "AskReddit" and state["sort"] == "best" and state["parent"] == "pbic8om"
    assert state["src"].startswith("/svc/shreddit/more-comments/AskReddit/t3_1wnwlhv?") and "commentParentPositions=26" in state["src"]
    assert state["cur"] and "&amp;" not in state["src"]
    tail = P.decode_cursor(page["more_comments"]["cursor"])
    assert "top-level=1" in tail["src"] and tail["cur"] and "parent" not in tail
    deleted = next(c for c in roots if c["author"] is None)
    assert deleted["flags"]["is_deleted"] is True and deleted["text"] is None and deleted["stats"]["score"] == 212


def test_branch_partial_roots_point_at_the_cursor_parent():
    page = H.parse_tree(BRANCH, "1wnwlhv", "AskReddit", "best", parent="pbie3xx")
    roots = page["comments"]
    assert len(roots) == 7 and len(list(_walk(roots))) == 10
    assert all(r["depth"] == 1 and r["parent_comment_id"] == "pbie3xx" for r in roots)
    assert roots[0]["replies"][0]["parent_comment_id"] == "pbishq7" and roots[0]["replies"][0]["depth"] == 2
    assert roots[0]["more_replies"]["count"] == 5
    assert page["more_comments"]["count"] == 47                   # the branch's own "47 more replies" continuation
    assert P.decode_cursor(page["more_comments"]["cursor"])["parent"] == "pbie3xx"


def test_single_comment_partial():
    page = H.parse_tree(ONE, None, None, "best")
    assert len(page["comments"]) == 1
    c = page["comments"][0]
    assert c["id"] == "pbefx01" and c["post_id"] == "1wn1d94" and c["subreddit"]["name"] == "Python"


def test_unknown_or_empty_partial_parses_to_nothing():
    assert H.parse_tree("", "zzz", None, "best") == {"comments": [], "more_comments": None}
    assert H.parse_tree("<div></div>", "zzz", None, "best") == {"comments": [], "more_comments": None}


# ---- the fake browser ------------------------------------------------------------------------

WALL = "<html>" + "a" * 50_000 + "You've been blocked by network security.</html>"


class _Box:
    """The local Chrome stand-in (browser._CHROME): answers in-page fetches by
    URL pattern and records them; the first `refuse` answers are the wall."""

    def __init__(self):
        self.calls = []
        self.refuse = 0
        self.closed = 0

    def call(self, fn):
        return fn()

    def close(self):
        self.closed += 1

    def fetch(self, url, method, headers, body):
        self.calls.append({"url": url, "method": method, "body": body, "headers": headers})
        if self.refuse:
            self.refuse -= 1
            return {"status": 403, "body": WALL}
        if "/svc/shreddit/comments/r/" in url and "t3_zzzzzzzz" in url:
            return {"status": 200, "body": ""}
        if "/svc/shreddit/comments/r/" in url:
            return {"status": 200, "body": SMALL if "1wn1d94" in url else TOP}
        if "/svc/shreddit/more-comments/" in url:
            assert method == "POST" and headers["content-type"] == "application/x-www-form-urlencoded"
            return {"status": 200, "body": TOP if "top-level=1" in url else BRANCH}
        if "/svc/shreddit/comment/t1_" in url:
            return {"status": 200, "body": ONE if "pbefx01" in url else ""}
        if "/r/technology/comments/?" in url:
            return {"status": 200, "body": STREAM}
        if "/r/lounge/comments/?" in url:
            return {"status": 403, "body": "<html><title>lounge: premium members only</title></html>"}
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def box(monkeypatch):
    fake = _Box()
    monkeypatch.setattr(B, "_CHROME", fake)
    monkeypatch.setattr(config, "REDDIT_BROWSER_ATTEMPTS", 3)
    post = json.load(open(os.path.join(FIXTURES, "post_comments.json")))[0]["data"]["children"][0]
    post["data"]["id"] = "1wn1d94"
    post["data"]["subreddit"] = "Python"
    monkeypatch.setattr(posts, "_post_only", lambda post_id, subreddit: P.post(post))
    fake.lookups = []
    monkeypatch.setattr(S, "posts_by_ids", lambda fullnames: fake.lookups.append(list(fullnames))
                        or [post for f in fullnames if f == "t3_1wn1d94"])
    yield fake


# ---- endpoints over the fake browser -----------------------------------------------------------------

def test_comments_reads_the_tree_partial_and_follows_the_tail_up_to_limit(box):
    out = posts.comments({"id": "1wn1d94", "share": None}, sort="new", limit=100)
    assert out["post"]["id"] == "1wn1d94" and out["sort"] == "new"
    assert out["comment_count"] == 11 and out["more_comments"] is None
    assert len(box.calls) == 1
    url = box.calls[0]["url"]
    assert url == "https://www.reddit.com/svc/shreddit/comments/r/Python/t3_1wn1d94?sort=NEW"
    assert box.calls[0]["headers"]["accept"].startswith("text/vnd.reddit.partial+html")
    # a big thread: the tail is followed while fewer than `limit` top-level comments are in
    box.calls.clear()
    out = posts.comments({"id": "1wnwlhv", "share": None}, sort="best", limit=15)
    assert len(box.calls) == 2 and box.calls[1]["method"] == "POST"
    body = parse_qs(box.calls[1]["body"])
    assert body["cursor"][0] and "csrf_token" in box.calls[1]["body"]
    assert len(out["comments"]) == 18 and out["more_comments"]["cursor"]
    box.calls.clear()
    posts.comments({"id": "1wnwlhv", "share": None}, sort="best", limit=5)
    assert len(box.calls) == 1                     # 9 top-level comments already satisfy limit=5


def test_more_replays_the_loader_and_marks_the_parent(box):
    page = posts.comments({"id": "1wnwlhv", "share": None}, sort="best", limit=5)
    stub = page["comments"][1]["more_replies"]
    out = posts.more(stub["cursor"])
    assert box.calls[-1]["method"] == "POST" and "/more-comments/AskReddit/t3_1wnwlhv?" in box.calls[-1]["url"]
    assert unquote(parse_qs(box.calls[-1]["body"])["cursor"][0]) == P.decode_cursor(stub["cursor"])["cur"]
    assert all(c["parent_comment_id"] == "pbic8om" for c in out["comments"])
    assert out["more_comments"]["count"] == 47
    with pytest.raises(RedditBadRequest):
        posts.more(P.encode_cursor({"post": "1wnwlhv", "sort": "best", "ids": ["a", "b"]}))     # a pre-2026-10-23 cursor
    with pytest.raises(RedditBadRequest):
        posts.more("garbage")


def test_comments_via_cursor_param_is_more(box):
    page = posts.comments({"id": "1wnwlhv", "share": None}, sort="best", limit=5)
    out = posts.comments(None, cursor=page["more_comments"]["cursor"])
    assert out["post"] is None and out["comment_count"] == 10 and out["comments"]


def test_export_expands_folds_breadth_first_within_the_budget(box):
    out = posts.export({"id": "1wnwlhv", "share": None}, sort="best", max_comments=60)
    assert out["comment_count"] == 60 and out["meta"]["is_truncated"] is True
    assert out["meta"]["calls"] > 1 and all("replies" not in c and "reply_count" in c for c in out["comments"])
    assert out["comments"][0]["depth"] == 0
    small = posts.export({"id": "1wn1d94", "share": None}, sort="new", max_comments=500)
    assert small["comment_count"] == 11 and small["meta"] == {"calls": 1, "is_truncated": False, "max_comments": 500}
    ids = [c["id"] for c in small["comments"]]
    assert ids.index("pbe12oz") == ids.index("pbcrehs") + 1           # thread order: replies follow their parent


def test_comment_details_uses_the_single_partial_and_the_focused_page(box):
    out = posts.comment_details({"id": "pbefx01", "post_id": None})
    assert out["comment"]["id"] == "pbefx01" and box.calls[-1]["url"].endswith("/svc/shreddit/comment/t1_pbefx01?depth=0")
    assert out["comment"]["post"]["id"] == "1wn1d94" and out["comment"]["post"]["title"]     # parent-post summary via /by_id
    with pytest.raises(RedditNotFound):
        posts.comment_details({"id": "nope123", "post_id": None})
    with pytest.raises(RedditBadRequest):
        posts.comment_details({"id": "pbe12oz", "post_id": None}, context=2)
    out = posts.comment_details({"id": "pbe12oz", "post_id": "1wn1d94"}, context=2)
    assert out["post"]["id"] == "1wn1d94" and out["comment"]["id"] == "pbcrehs"      # the chain holding the comment
    assert box.calls[-1]["url"].endswith("/svc/shreddit/comments/r/Python/t3_1wn1d94/t1_pbe12oz?sort=CONFIDENCE&context=2")


def test_focused_comments_keep_only_the_chain_holding_the_comment(box):
    out = posts.comments({"id": "1wn1d94", "share": None}, comment="pbdol0a", context=1)
    assert [c["id"] for c in out["comments"]] == ["pbcrehs"] and out["more_comments"] is None
    with pytest.raises(RedditNotFound):
        posts.comments({"id": "1wn1d94", "share": None}, comment="zzzzzzz", context=1)


def test_comments_by_ids_hydrates_search_hits_through_the_partial(box):
    found = S.comments_by_ids(["pbefx01", "t1_nope123"])
    assert [c["id"] for c in found] == ["pbefx01"]
    post = found[0]["post"]
    assert post["id"] == "1wn1d94" and post["title"] and post["link"].startswith("https://www.reddit.com/")
    assert set(post) == {"id", "title", "link", "external_link", "author", "comment_count", "is_nsfw"}
    assert box.lookups == [["t3_1wn1d94"]]                       # one /by_id per distinct post
    assert S.with_post_refs([{"id": "x", "post_id": "gone123"}])[0]["post"] is None


def test_unknown_post_tree_is_a_404(box):
    with pytest.raises(RedditNotFound):
        H.tree("zzzzzzzz", "Python", "best", 25)


# ---- the local Chrome: a wall reopens the browser --------------------------------------------------

def test_a_walled_answer_reopens_the_browser_and_asks_again(box):
    box.refuse = 2
    out = posts.comments({"id": "1wn1d94", "share": None}, sort="new", limit=100)
    assert out["comment_count"] == 11 and len(box.calls) == 3 and box.closed == 2


def test_walled_on_every_attempt_fails_as_blocked(box):
    box.refuse = 50
    with pytest.raises(RedditBlocked):
        posts.comments({"id": "1wn1d94", "share": None}, sort="new", limit=100)
    assert len(box.calls) == config.REDDIT_BROWSER_ATTEMPTS == 3


def test_a_missing_browser_is_reported_at_once(box, monkeypatch):
    def missing(url, method, headers, body):
        box.calls.append({"url": url})
        raise B.BrowserMissing("comments need a browser: pip install patchright && python -m patchright install chromium")
    monkeypatch.setattr(box, "fetch", missing)
    with pytest.raises(B.BrowserMissing):
        H.page_fetch("/svc/shreddit/comment/t1_pbefx01?depth=0", "comment pbefx01")
    assert len(box.calls) == 1


def test_wall_detection_and_proxy_parsing():
    assert B.walled(WALL) and not B.walled('<shreddit-comment thingId="t1_x"></shreddit-comment>')
    assert B._playwright_proxy("http://user%40x:p%3Ass@gw.example:823") == {
        "server": "http://gw.example:823", "username": "user@x", "password": "p:ss"}
    assert B._playwright_proxy("http://gw.example:3128") == {"server": "http://gw.example:3128"}


def test_sticky_is_the_hot_listings_pinned_post_with_its_tree(box, monkeypatch):
    from reddit import subreddits
    thing = json.load(open(os.path.join(FIXTURES, "post_comments.json")))[0]["data"]["children"][0]
    pinned = json.loads(json.dumps(thing)); pinned["data"].update(id="1wn1d94", stickied=True, subreddit="Python")
    plain = json.loads(json.dumps(thing)); plain["data"].update(id="1aaaaaa", stickied=False)
    listing = {"kind": "Listing", "data": {"children": [pinned, plain], "after": None}}
    asked = []
    monkeypatch.setattr(subreddits, "get_json", lambda path, params=None, **kw: asked.append((path, params)) or listing)
    out = subreddits.sticky({"name": "python", "id": None}, 1)
    assert asked == [("/r/python/hot", {"limit": 5})]
    assert out["post"]["id"] == "1wn1d94" and out["post"]["flags"]["is_stickied"] is True
    assert len(out["comments"]) == 4 and out["more_comments"] is None
    assert box.calls[-1]["url"].endswith("/svc/shreddit/comments/r/python/t3_1wn1d94?sort=CONFIDENCE")
    with pytest.raises(RedditNotFound):
        subreddits.sticky({"name": "python", "id": None}, 2)



# ---- the subreddit comment stream (legacy www page) ------------------------------------------

def test_stream_page_parses_comments_with_their_post():
    out = H.parse_stream(STREAM)
    assert len(out["comments"]) == 5 and out["next_cursor"] == "t1_19rkldhddj08x" and out["has_more"] is True
    c = out["comments"][0]
    assert c["id"] == "19rklfeycyazl" and c["post_id"] == "1xdhh1x" and c["created_at"] == "2026-10-24T07:11:15Z"
    assert c["author"]["username"] == "KellyCTargaryen" and c["author"]["id"] == "jbyw9"
    assert c["subreddit"] == {"name": "technology", "id": "2qh16", "link": "https://www.reddit.com/r/technology/"}
    assert c["stats"]["score"] == 1 and c["text"].startswith("Worse, “cloning” costs")
    assert c["flags"]["is_submitter"] is False and c["flags"]["is_edited"] is False and c["edited_at"] is None
    assert c["post"]["title"].startswith("Cool, Cool, Cool, Palmer Luckey") and c["post"]["comment_count"] == 277
    assert c["post"]["external_link"].startswith("https://www.wired.com/") and c["post"]["is_nsfw"] is False
    assert c["post"]["link"] == "https://www.reddit.com/r/technology/comments/1xdhh1x/cool_cool_cool_palmer_luckey_and_ben_lamm_made_a/"
    assert all(x["post"] and x["author"] for x in out["comments"])


def test_subreddit_comments_page_through_the_stream(box):
    from reddit import subreddits
    out = subreddits.comments({"name": "technology", "id": None}, cursor="t1_19rkldhddj08x", limit=5)
    assert out["subreddit"] == "technology" and len(out["comments"]) == 5 and out["next_cursor"]
    assert box.calls[-1]["url"] == "https://www.reddit.com/r/technology/comments/?limit=5&after=t1_19rkldhddj08x"
    assert box.calls[-1]["headers"]["accept"] == "text/html"


def test_refused_stream_is_explained_by_about(box, monkeypatch):
    from reddit import subreddits
    def gold_only(kind, name, label=None):
        raise RedditNotFound(f"r/{name} is gold only", reason="subreddit_gold_only")
    monkeypatch.setattr(S, "about_json", gold_only)
    with pytest.raises(RedditNotFound) as err:
        subreddits.comments({"name": "lounge", "id": None})
    assert err.value.reason == "subreddit_gold_only"
