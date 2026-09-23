"""Live endpoint smoke tests: one call per /reddit/* route against a running
service, with example values proven to return data (2026-09-23). The
listing tooling reads each route's FIRST call from this file's AST as its
working example, so the values stay literals.

Skipped unless REDDIT_BASE points at a running service:

    ONLY_SCRAPER=reddit python run.py
    REDDIT_BASE=http://127.0.0.1:6002 python -m pytest reddit/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("REDDIT_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set REDDIT_BASE to run live endpoint tests")


def call(path, status=200, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=180)
    assert resp.status_code == status, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def test_subreddits():
    details = call("/reddit/subreddits/details", subreddit="python")
    assert details["subreddit"]["subscriber_count"] > 1_000_000 and details["rules"]
    posts = call("/reddit/subreddits/posts", subreddit="r/Python", sort="top", time="week", limit=5)
    assert len(posts["posts"]) == 5 and posts["next_cursor"]
    page2 = call("/reddit/subreddits/posts", subreddit="python", sort="top", time="week", limit=5, cursor=posts["next_cursor"])
    assert page2["posts"][0]["id"] != posts["posts"][0]["id"]
    assert call("/reddit/subreddits/comments", subreddit="python", limit=5)["comments"][0]["post"]["title"]
    assert call("/reddit/subreddits/search", subreddit="python", query="flair:Showcase", limit=5)["posts"]
    assert call("/reddit/subreddits/rules", subreddit="AskReddit")["rules"]
    assert "index" in call("/reddit/subreddits/wiki", subreddit="python", page="list")["pages"]
    assert call("/reddit/subreddits/wiki", subreddit="python")["page"]["content_markdown"]
    assert call("/reddit/subreddits/similar", subreddit="python")["subreddits"]
    assert call("/reddit/subreddits/sticky", subreddit="python")["post"]["flags"]["is_stickied"] is True
    batch = call("/reddit/subreddits/batch", subreddits="python,learnpython,t5_2qh1i,doesnotexist123456")
    assert batch["count"] == 3 and batch["missing"] == ["doesnotexist123456"]
    assert call("/reddit/subreddits/flairs", subreddit="python")["flairs"]
    assert call("/reddit/subreddits/popular", limit=5)["subreddits"]
    assert call("/reddit/subreddits/new", limit=5)["subreddits"]
    board = call("/reddit/subreddits/leaderboard")
    assert board["subreddits"][0]["weekly_visitor_count"] > 1_000_000 and board["topics"]
    assert call("/reddit/subreddits/leaderboard", topic="technology")["subreddits"]
    insights = call("/reddit/subreddits/insights", subreddit="python", sample="top", time="week")
    assert insights["activity"]["posts_per_day"] and insights["best_posting_hours_utc"]


def test_posts_and_comments():
    assert call("/reddit/posts/details", post="https://www.reddit.com/r/AskReddit/comments/1wnwlhv/")["post"]["title"]
    tree = call("/reddit/posts/comments", post="1wnwlhv", limit=20, depth=2)
    assert tree["comments"] and tree["more_comments"]["cursor"]
    more = call("/reddit/posts/comments/more", cursor=tree["more_comments"]["cursor"])
    assert more["comments"]
    export = call("/reddit/posts/comments/export", post="1wnwlhv", max_comments=200)
    assert export["comment_count"] == 200 and export["meta"]["calls"] >= 1
    assert "duplicates" in call("/reddit/posts/duplicates", post="1wnwlhv")
    batch = call("/reddit/posts/batch", posts="t3_1wnwlhv,https://redd.it/1wn1d94,zzzzzzz")
    assert batch["count"] == 2 and batch["missing"] == ["zzzzzzz"]
    assert call("/reddit/posts/by-url", url="https://www.python.org/", limit=5)["posts"]
    assert "media" in call("/reddit/posts/media", post="1wnwlhv")
    first = tree["comments"][0]["id"]
    assert call("/reddit/comments/details", comment=first)["comment"]["id"] == first
    assert call("/reddit/comments/details", comment=f"t1_{first}", context=1)["post"]["id"] == "1wnwlhv"


def test_users():
    details = call("/reddit/users/details", user="spez")
    assert details["user"]["karma"]["total"] > 100_000 and details["trophies"]
    assert call("/reddit/users/details", user="t2_1w72", include_trophies="false")["user"]["username"] == "spez"
    assert call("/reddit/users/posts", user="u/spez", limit=5)["posts"]
    assert call("/reddit/users/posts", user="spez", sort="top", time="all", limit=5)["posts"]   # served as time=year
    assert call("/reddit/users/comments", user="spez", limit=5)["comments"][0]["post"]["title"]
    assert call("/reddit/users/overview", user="https://www.reddit.com/user/spez/", limit=5)["items"][0]["kind"]
    assert call("/reddit/users/moderated", user="spez")["subreddits"]
    assert call("/reddit/users/trophies", user="spez")["trophy_count"] > 10
    batch = call("/reddit/users/batch", users="spez,t2_1w72,kn0thing,nonexistentuser9999xx")
    assert batch["count"] == 3 and batch["missing"] == ["nonexistentuser9999xx"]
    assert call("/reddit/users/active-subreddits", user="spez")["subreddits"]
    assert call("/reddit/users/insights", user="spez")["activity"]["comment_count"] >= 0


def test_search_and_feeds():
    assert call("/reddit/search/posts", query="python", sort="top", time="month", limit=5)["posts"]
    assert call("/reddit/search/posts", query="asyncio", subreddit="python", limit=5)["subreddit"] == "python"
    assert call("/reddit/search/subreddits", query="python", limit=5)["subreddits"]
    assert call("/reddit/search/users", query="spez", limit=5)["users"]
    comments = call("/reddit/search/comments", query="python")
    assert comments["comments"] and comments["next_cursor"]
    assert call("/reddit/search/comments", query="python", cursor=comments["next_cursor"])["comments"]
    assert call("/reddit/search/media", query="python", sort="top", time="month")["posts"][0]["media"]
    assert call("/reddit/search/autocomplete", query="pyth")["results"]
    popular = call("/reddit/feeds/popular", country="DE", limit=5)
    assert popular["country"] == "DE" and popular["posts"]
    assert call("/reddit/feeds/all", sort="top", time="day", limit=5)["posts"]
    assert call("/reddit/feeds/best", limit=5)["posts"]


def test_errors():
    assert "not found" in call("/reddit/subreddits/details", status=404, subreddit="thissubdoesnotexist12345")["error"]
    assert "gold only" in call("/reddit/subreddits/details", status=404, subreddit="lounge")["error"]
    assert call("/reddit/users/details", status=404, user="zz_no_such_user_9q7x")["error"]
    assert call("/reddit/posts/details", status=404, post="zzzzzzzz")["error"]
    assert call("/reddit/posts/details", status=404, post="https://www.reddit.com/r/python/s/AbCdEfGhIj")["error"]
    assert "sort" in call("/reddit/subreddits/posts", status=400, subreddit="python", sort="best")["errors"]
    assert "country" in call("/reddit/feeds/popular", status=400, country="BR")["errors"]
    assert "cursor" in call("/reddit/posts/comments/more", status=400, cursor="garbage")["error"]
