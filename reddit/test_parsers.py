"""Offline tests for the Reddit refs, parsers, www extractors and schemas —
no network.

Every fixture in reddit/fixtures/ is a real upstream payload captured on
2026-09-22/23 through reddit/fetch.py: oauth.reddit.com JSON verbatim
(listings trimmed to a few children) and trimmed www shreddit pages. The
assertions pin the field mapping decoded from live data, so a silent
upstream rename shows up here rather than as nulls in a customer's
response.

    python -m pytest reddit/test_parsers.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from reddit import parsers as P, refs, schemas, www  # noqa: E402
from schema_fields import load_query  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle) if name.endswith(".json") else handle.read()


# ---- refs ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "python", "r/python", "/r/python/", "https://www.reddit.com/r/python", "https://old.reddit.com/r/python/hot/",
    "reddit.com/r/python/comments/1wn1d94/x/",
])
def test_resolve_subreddit_forms(value):
    assert refs.resolve_subreddit(value) == {"name": "python", "id": None}


def test_resolve_subreddit_ids_and_profiles():
    assert refs.resolve_subreddit("t5_2qh0y") == {"name": None, "id": "t5_2qh0y"}
    assert refs.resolve_subreddit("https://www.reddit.com/user/spez/") == {"name": "u_spez", "id": None}
    with pytest.raises(ValueError):
        refs.resolve_subreddit("https://example.com/r/python")
    with pytest.raises(ValueError):
        refs.resolve_subreddit("bad name!")


@pytest.mark.parametrize("value", [
    "1wn1d94", "1WN1D94", "t3_1wn1d94", "https://www.reddit.com/r/python/comments/1wn1d94/slug/",
    "https://www.reddit.com/comments/1wn1d94", "https://redd.it/1wn1d94", "https://www.reddit.com/gallery/1wn1d94",
    "https://old.reddit.com/user/spez/comments/1wn1d94/slug/", "reddit.com/r/python/comments/1wn1d94/slug/abc123/",
])
def test_resolve_post_forms(value):
    assert refs.resolve_post(value) == {"id": "1wn1d94", "share": None}


def test_resolve_post_share_and_errors():
    assert refs.resolve_post("https://www.reddit.com/r/python/s/AbCdEf123") == {"id": None, "share": "/r/python/s/AbCdEf123"}
    with pytest.raises(ValueError):
        refs.resolve_post("https://www.reddit.com/r/python/")
    with pytest.raises(ValueError):
        refs.resolve_post("not an id!")


@pytest.mark.parametrize("value", ["spez", "u/spez", "/u/spez/", "https://www.reddit.com/user/spez", "https://www.reddit.com/u/spez/comments/"])
def test_resolve_user_forms(value):
    assert refs.resolve_user(value) == {"name": "spez", "id": None}


def test_resolve_user_ids():
    assert refs.resolve_user("t2_1w72") == {"name": None, "id": "t2_1w72"}
    assert refs.resolve_user("https://www.reddit.com/r/u_spez/") == {"name": "spez", "id": None}
    with pytest.raises(ValueError):
        refs.resolve_user("x" * 30)


def test_resolve_comment():
    assert refs.resolve_comment("t1_paj0q5z") == {"id": "paj0q5z", "post_id": None}
    assert refs.resolve_comment("paj0q5z") == {"id": "paj0q5z", "post_id": None}
    link = "https://www.reddit.com/r/x/comments/1wjjbga/slug/paj0q5z/"
    assert refs.resolve_comment(link) == {"id": "paj0q5z", "post_id": "1wjjbga"}
    assert refs.resolve_comment("https://www.reddit.com/comments/1wjjbga/comment/paj0q5z/") == {"id": "paj0q5z", "post_id": "1wjjbga"}
    with pytest.raises(ValueError):
        refs.resolve_comment("https://www.reddit.com/r/x/comments/1wjjbga/slug/")


def test_cursor_round_trip():
    payload = {"post": "1wnwlhv", "sort": "best", "ids": ["a", "b"]}
    assert P.decode_cursor(P.encode_cursor(payload)) == payload
    with pytest.raises(ValueError):
        P.decode_cursor("not base64 json")


# ---- posts ----------------------------------------------------------------------------------

def test_text_post():
    post = P.post(load("listing_hot.json")["data"]["children"][0])
    assert post["id"] == "1w6eajz"
    assert post["type"] == "text"
    assert post["link"].startswith("https://www.reddit.com/r/AskReddit/comments/1w6eajz/")
    assert post["external_link"] is None
    assert post["subreddit"] == {"name": "AskReddit", "id": "2qh1i", "link": "https://www.reddit.com/r/AskReddit/",
                                 "subscriber_count": post["subreddit"]["subscriber_count"], "type": "public"}
    assert post["subreddit"]["subscriber_count"] > 50_000_000
    assert post["author"]["username"]
    assert post["flags"]["is_stickied"] is True
    assert post["created_at"].endswith("Z")
    assert isinstance(post["stats"]["score"], int)
    assert post["media"] is None and post["poll"] is None
    for noise in ("ups", "downs", "name", "all_awardings", "saved", "pwls"):
        assert noise not in post


def test_media_post_types():
    media = load("media_posts.json")
    gallery = P.post(media["gallery"])
    assert gallery["type"] == "gallery"
    assert [i["id"] for i in gallery["media"]["images"]] == ["nlt20xoos1rh1", "ew5b0ayos1rh1"]
    assert gallery["media"]["images"][0]["link"] == "https://i.redd.it/nlt20xoos1rh1.png"
    assert gallery["media"]["images"][0]["width"] == 1086
    image = P.post(media["image"])
    assert image["type"] == "image"
    assert image["media"]["images"][0]["link"] == "https://i.redd.it/16pkfmzclyqh1.jpeg"
    assert image["media"]["preview"]["link"].startswith("https://preview.redd.it/")
    video = P.post(media["video"])
    assert video["type"] == "video"
    vid = video["media"]["video"]
    assert vid["link"] == "https://v.redd.it/s75tbjh4qxqh1/CMAF_1080.mp4"
    assert vid["audio_link"] == "https://v.redd.it/s75tbjh4qxqh1/CMAF_AUDIO_128.mp4"
    assert vid["dash_manifest"] == "https://v.redd.it/s75tbjh4qxqh1/DASHPlaylist.mpd"
    assert vid["duration"] == 29 and vid["has_audio"] is True
    embed = P.post(media["link"])
    assert embed["type"] == "embed"
    assert embed["media"]["embed"]["provider"] == "YouTube"
    assert embed["external_link"].startswith("https://")
    poll = P.post(media["poll"])
    assert poll["type"] == "poll"
    assert len(poll["poll"]["options"]) >= 2 and poll["poll"]["voting_ends_at"].endswith("Z")


def test_post_ref_from_comment_stream():
    comment = P.comment(load("sr_comments.json")["data"]["children"][0])
    assert comment["post"]["id"] == "1wktvbr"
    assert comment["post"]["title"]
    assert comment["post"]["comment_count"] == 154
    assert comment["subreddit"]["name"] == "Python"


# ---- comments -------------------------------------------------------------------------------

def test_comment_tree_and_more_stubs():
    payload = load("post_comments.json")
    state = {"sort": "top", "limit": 25, "depth": 4}
    comments, tail = P.comment_tree(payload[1]["data"]["children"], "1wnwlhv", state)
    assert len(comments) == 16
    first = comments[0]
    assert first["id"] == "pbic679" and first["post_id"] == "1wnwlhv"
    assert first["parent_comment_id"] is None and first["depth"] == 0
    assert first["author"]["username"] == "TTdriver"
    assert first["text"].startswith("Honestly")
    assert first["stats"]["score"] > 0
    assert first["flags"]["is_submitter"] is False
    assert first["link"].endswith("/pbic679/")
    # the listing tail holds 2500+ ids -> a skip cursor, not an id list
    assert tail["count"] > 100
    decoded = P.decode_cursor(tail["cursor"])
    assert decoded == {"post": "1wnwlhv", "sort": "top", "limit": 25, "depth": 4, "skip": 0}
    nested = next(c for c in comments if c["more_replies"])
    inner = P.decode_cursor(nested["more_replies"]["cursor"])
    assert inner["post"] == "1wnwlhv" and inner["ids"]
    # replies nest and carry their parent id
    with_replies = next(c for c in comments if c["replies"])
    assert with_replies["replies"][0]["parent_comment_id"] == with_replies["id"]
    assert with_replies["replies"][0]["depth"] == 1


def test_rebuild_tree_from_morechildren():
    things = load("morechildren.json")["json"]["data"]["things"]
    roots, tail = P.rebuild_tree(things, "1wnwlhv", {"sort": "top"})
    assert roots and tail is None
    for root in roots:
        assert "replies" in root and "more_replies" in root


def test_more_stub_forms():
    focus = P.more_stub({"kind": "more", "data": {"count": 0, "children": [], "id": "_", "parent_id": "t1_abc"}}, "p1")
    assert P.decode_cursor(focus["cursor"]) == {"post": "p1", "sort": None, "focus": "abc"}
    ids = P.more_stub({"kind": "more", "data": {"count": 3, "children": ["a", "b", "c"], "parent_id": "t3_p1"}}, "p1", {"sort": "new", "limit": 100})
    assert P.decode_cursor(ids["cursor"]) == {"post": "p1", "sort": "new", "ids": ["a", "b", "c"]}
    assert P.more_stub({"kind": "more", "data": {"count": 0, "children": [], "parent_id": "t3_p1"}}, "p1") is None


# ---- subreddit / user ----------------------------------------------------------------------------

def test_subreddit():
    sub = P.subreddit(load("subreddit_about.json"))
    assert sub["id"] == "2qh0y" and sub["name"] == "Python"
    assert sub["link"] == "https://www.reddit.com/r/Python/"
    assert sub["subscriber_count"] > 1_000_000
    assert sub["active_user_count"] is None          # hidden anonymously
    assert sub["created_at"] == "2008-01-25T03:14:39Z"
    assert sub["icon"].startswith("https://styles.redditmedia.com/")
    assert sub["flags"]["is_nsfw"] is False and sub["flags"]["allows_images"] in (True, False)
    assert sub["submission"]["type"] in ("any", "link", "self")
    assert "user_is_banned" not in sub and "wls" not in sub


def test_rules_and_wiki():
    rules = P.rules(load("subreddit_rules.json"))
    assert rules["rules"][0]["name"] == "No showcase posts"
    assert rules["rules"][0]["applies_to"] == "posts"
    assert len(rules["site_rules"]) == 3
    page = P.wiki_page(load("wiki_page.json"), "index")
    assert page["name"] == "index" and page["content_markdown"] and page["content_html"]
    assert page["revised_by"]["username"] == "IAmKindOfCreative"
    assert page["revised_at"].endswith("Z")


def test_user_and_trophies():
    user = P.user(load("user_about.json"))
    assert user["id"] == "1w72" and user["username"] == "spez"
    assert user["created_at"] == "2005-06-06T04:00:00Z"
    assert user["karma"]["total"] == user["karma"]["post"] + user["karma"]["comment"]
    assert user["flags"]["is_employee"] is True and user["flags"]["is_suspended"] is False
    assert user["profile"]["follower_count"] == 0            # spez hides followers; the key is still served
    trophies = [P.trophy(t) for t in load("user_trophies.json")["data"]["trophies"]]
    assert trophies[0]["name"] == "15-Year Club"
    assert trophies[0]["icon"].endswith("-70.png")
    moderated = P.moderated_subreddit(load("user_moderated.json")["data"][0])
    assert moderated["name"] == "announcements" and moderated["mod_permissions"] == []
    lookup = load("user_by_ids.json")
    compact = P.user_from_account_ids("t2_1w72", lookup["t2_1w72"])
    assert compact["username"] == "spez" and compact["id"] == "1w72"


def test_listings_and_things():
    out = P.parse_listing(load("search_users.json"), P.user, "users")
    assert out["users"][0]["username"] == "python"
    assert out["next_cursor"] and out["has_more"] is True
    kinds = [P.thing(c)["kind"] for c in load("api_info_mixed.json")["data"]["children"]]
    assert sorted(kinds) == ["comment", "post", "subreddit"]
    overview = P.parse_listing(load("user_overview.json"), P.thing, "items")
    assert all(i["kind"] in ("post", "comment") for i in overview["items"])
    popular = P.parse_listing(load("popular_geo.json"), P.post, "posts")
    assert popular["posts"] and load("popular_geo.json")["data"]["geo_filter"] == "DE"
    auto = P.subreddit_compact(load("autocomplete.json")["data"]["children"][0])
    assert auto["name"] == "Python" and auto["subscriber_count"] > 0
    similar = P.parse_listing(load("similar.json"), P.subreddit, "subreddits")
    assert len(similar["subreddits"]) == 9


# ---- www extractors -------------------------------------------------------------------------------

def test_www_comment_search():
    page = load("www_search_comments.html")
    ids = www.search_comment_ids(page)
    assert len(ids) == 9 and ids[0] == "t1_paj0q5z" and all(i.startswith("t1_") for i in ids)
    cursor = www.next_cursor(page)
    assert cursor and cursor.startswith("eyJ") and "%" not in cursor


def test_www_media_search():
    page = load("www_search_media.html")
    ids = www.search_post_ids(page)
    assert ids[0] == "t3_1v5fej6" and len(ids) == 7
    assert www.next_cursor(page)


def test_www_explore():
    page = load("www_explore.html")
    cards = www.explore_cards(page)
    assert cards[0]["name"] == "AskReddit" and cards[0]["id"] == "2qh1i"
    assert cards[0]["weekly_visitor_count"] > 10_000_000
    assert cards[0]["description"].startswith("The go-to subreddit")
    topics = www.explore_topics(page)
    assert {"id": "2unn29s", "slug": "internet_culture", "name": "Internet Culture"} in topics


# ---- schemas --------------------------------------------------------------------------------------

def test_schemas():
    data, err = load_query(schemas.SubredditPostsSchema, {"subreddit": "R/Python", "sort": "TOP", "time": "week", "limit": "10"})
    assert err is None and data["subreddit"]["name"] == "Python" and data["sort"] == "top" and data["limit"] == 10
    _, err = load_query(schemas.SubredditPostsSchema, {"subreddit": "python", "sort": "best"})
    assert "sort" in err["errors"]
    _, err = load_query(schemas.SubredditPostsSchema, {"subreddit": "python", "nope": "1"})
    assert "nope" in err["errors"]
    data, _ = load_query(schemas.PostCommentsSchema, {"post": "t3_1wn1d94", "comment": "t1_abc"})
    assert data["post"]["id"] == "1wn1d94" and data["comment"] == "abc" and data["limit"] == 100
    _, err = load_query(schemas.PostCommentsSchema, {"sort": "top"})
    assert "post" in err["errors"]
    data, _ = load_query(schemas.PostCommentsSchema, {"cursor": "abc"})
    assert data["post"] is None
    data, _ = load_query(schemas.PopularSchema, {"country": "de"})
    assert data["country"] == "DE"
    _, err = load_query(schemas.PopularSchema, {"country": "BR"})
    assert "country" in err["errors"]
    data, _ = load_query(schemas.SubredditBatchSchema, {"subreddits": "python, t5_2qh0y ,python"})
    assert data["subreddits"] == [{"name": "python", "id": None}, {"name": None, "id": "t5_2qh0y"}]
    _, err = load_query(schemas.UserBatchSchema, {"users": ""})
    assert err
    data, _ = load_query(schemas.PostsByUrlSchema, {"url": "https://www.python.org/"})
    assert data["url"] == "https://www.python.org/"


def test_dash_manifests():
    legacy = P.parse_dash_manifest(load("dash_legacy.mpd"), "https://v.redd.it/fko4j44oa9g81")
    assert legacy["audio"][0]["link"] == "https://v.redd.it/fko4j44oa9g81/DASH_audio.mp4"
    assert [v["height"] for v in legacy["videos"]][-1] == 720
    assert all("/DASH_" in v["link"] for v in legacy["videos"])
    cmaf = P.parse_dash_manifest(load("dash_cmaf.mpd"), "https://v.redd.it/s75tbjh4qxqh1")
    assert cmaf["videos"][0]["link"] == "https://v.redd.it/s75tbjh4qxqh1/CMAF_220.mp4"
    assert cmaf["audio"] and all("CMAF_AUDIO" in a["link"] for a in cmaf["audio"])
    assert P.parse_dash_manifest("not xml", "x") is None
    # the listing parser never guesses a legacy audio name
    old = P.reddit_video({"fallback_url": "https://v.redd.it/abc/DASH_1080.mp4?source=fallback", "has_audio": True})
    assert old["audio_link"] is None and old["link"] == "https://v.redd.it/abc/DASH_1080.mp4"
