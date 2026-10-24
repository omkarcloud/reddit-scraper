"""Offline tests for the Reddit transport's rejection handling
(reddit/fetch.py: the 190 KB oauth wall page, 429 and 401 discarding the
exit AND the token, one re-mint shared by every thread, no exit rotation
ahead of a rejection) and the /about memo (reddit/shared.py). No network.

    python -m pytest reddit/test_fetch.py -q
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from reddit import fetch as F, shared as S, subreddits, users  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# The oauth wall page as served on 2026-10-23: 190 KB of inline CSS / base64
# images with the marker text in the last 600 bytes, no content-encoding.
WALL_PAGE = ('<body class=theme-beta><div><style>.theme-light,:root{--rem360:22.5rem}</style>' + "A" * 189_000
             + '<div class="font-bold">You\'ve been blocked by network security.</div>'
             '<div>If you think you\'ve been blocked by mistake, file a ticket.</div></div></body>')


class _Resp:
    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = ""


class _Sess:
    """A curl session stand-in answering from a queue (the last answer repeats).
    Records (path, params, bearer) per call."""

    def __init__(self, answers, calls):
        self.answers = answers          # shared queue: a dropped-and-rebuilt session continues it
        self.calls = calls

    def get(self, url, params=None, headers=None, **kw):
        self.calls.append((url.replace(F.OAUTH, ""), params, (headers or {}).get("authorization", "")[7:]))
        resp = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return resp

    def close(self):
        pass


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    S._abouts.clear()
    F._token.update(value="token-1", expires=time.time() + 3600)
    minted = iter(f"token-{n}" for n in range(2, 100))
    monkeypatch.setattr(F, "_mint_token", lambda session: next(minted))
    dropped = []
    monkeypatch.setattr(F, "_drop_session", lambda: dropped.append(1))
    F._test_dropped = dropped
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "MAX_RETRIES", 3)
    yield
    F._token.update(value=None, expires=0.0)
    S._abouts.clear()


def _serve(monkeypatch, *answers):
    calls = []
    queue = list(answers)
    monkeypatch.setattr(F, "_session", lambda: _Sess(queue, calls))
    return calls


LISTING = json.dumps({"kind": "Listing", "data": {"children": []}})
OK = lambda: _Resp(200, LISTING, {"x-ratelimit-remaining": "1"})   # noqa: E731


# ---- wall detection ----------------------------------------------------------------------

def test_wall_marker_at_the_end_of_a_403_body_is_a_wall():
    assert F._looks_like_wall(WALL_PAGE, 403)
    assert not F._looks_like_wall(WALL_PAGE, 200)          # a 200 page: only the head markers count
    assert F._looks_like_wall("<html>Prove your humanity</html>", 200)
    assert F._looks_like_wall("<html><p>You've been blocked by network security.</p>")
    assert not F._looks_like_wall('{"kind": "Listing"}', 403)
    assert not F._looks_like_wall("", 403)


# ---- a rejection forgets the exit and the token ----------------------------------------------

def test_a_wall_drops_the_exit_and_the_token_and_the_retry_uses_new_ones(monkeypatch):
    calls = _serve(monkeypatch, _Resp(403, WALL_PAGE), OK())
    assert F.get_json("/search", {"q": "x"})["kind"] == "Listing"
    assert [c[2] for c in calls] == ["token-1", "token-2"]      # the retry carried the new bearer
    assert len(F._test_dropped) == 1                            # and left on a fresh exit
    assert F.bearer() == "token-2"


def test_429_and_401_are_rejections_too(monkeypatch):
    calls = _serve(monkeypatch, _Resp(429, ""), _Resp(401, '{"message": "Unauthorized"}'), OK())
    assert F.get_json("/r/python/hot")["kind"] == "Listing"
    assert [c[2] for c in calls] == ["token-1", "token-2", "token-3"]
    assert len(F._test_dropped) == 2


def test_a_wall_on_every_attempt_is_a_blocked_error_after_max_retries(monkeypatch):
    calls = _serve(monkeypatch, _Resp(403, WALL_PAGE))
    with pytest.raises(F.RedditBlocked) as err:
        F.get_json("/comments/1xbnrhi", {"limit": 1, "depth": 0}, label="post 1xbnrhi")
    assert len(calls) == config.MAX_RETRIES == 3
    assert "post 1xbnrhi" in str(err.value)
    assert not isinstance(err.value, F.RedditNotFound)         # route_glue: 502


def test_the_token_is_replaced_once_however_many_threads_saw_it_fail():
    assert F.bearer(replace="token-1") == "token-2"
    assert F.bearer(replace="token-1") == "token-2"             # already replaced: no second mint
    assert F.bearer(replace="token-1") == "token-2"
    assert F.bearer(replace="token-2") == "token-3"             # the NEW one failing mints again
    assert F.bearer() == "token-3"


def test_a_failed_mint_leaves_no_token_so_the_next_caller_mints(monkeypatch):
    attempts = []

    def flaky(session):
        attempts.append(1)
        if len(attempts) == 1:
            raise F.RedditRateLimited("token endpoint rate-limited")
        return "token-fresh"

    monkeypatch.setattr(F, "_mint_token", flaky)
    with pytest.raises(F.RedditRateLimited):
        F.bearer(replace="token-1")
    assert F._token["value"] is None
    assert F.bearer() == "token-fresh"


def test_an_exit_is_kept_for_any_number_of_good_answers(monkeypatch):
    calls = _serve(monkeypatch, _Resp(200, LISTING, {"x-ratelimit-remaining": "0"}))
    for _ in range(150):
        F.get_json("/r/python/hot")
    assert len(calls) == 150 and not F._test_dropped and F.bearer() == "token-1"


def test_blocked_retries_do_not_sleep_but_upstream_errors_do(monkeypatch):
    slept = []
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))
    calls = _serve(monkeypatch, _Resp(403, WALL_PAGE), _Resp(429, ""), OK())
    F.get_json("/search", {"q": "x"})
    assert len(calls) == 3 and slept == []
    calls = _serve(monkeypatch, _Resp(503, ""), OK())
    F.get_json("/search", {"q": "x"})
    assert len(calls) == 2 and slept == [config.RETRY_BACKOFF]


# ---- the /about memo -------------------------------------------------------------------------

def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def test_about_is_fetched_once_per_name_within_the_ttl(monkeypatch):
    calls = []
    about = _fixture("subreddit_about.json")

    def fake_get_json(path, params=None, *, label=None, **kw):
        calls.append(path)
        return about

    monkeypatch.setattr(S, "get_json", fake_get_json)
    first = subreddits.details({"name": "python", "id": None}, include_rules=False)
    second = subreddits.details({"name": "Python", "id": None}, include_rules=False)
    assert first == second and first["subreddit"]["name"]
    assert S.subreddit_about("python")["kind"] == "t5"
    assert S.about_json("r", "PYTHON") is about
    assert calls == ["/r/python/about"]
    # expiry: the next lookup goes upstream again
    S._abouts[("r", "python")] = (time.time() - 1, about)
    S.about_json("r", "python")
    assert calls == ["/r/python/about", "/r/python/about"]


def test_user_about_is_memoised_and_errors_are_not(monkeypatch):
    calls = []
    about = _fixture("user_about.json")

    def fake_get_json(path, params=None, *, label=None, **kw):
        calls.append(path)
        if "nosuchuser" in path:
            raise F.RedditNotFound(f"{label} not found")
        return about

    monkeypatch.setattr(S, "get_json", fake_get_json)
    assert users._about("spez")["username"] == users._about("spez")["username"]
    assert calls == ["/user/spez/about"]
    for _ in range(2):
        with pytest.raises(F.RedditNotFound):
            users._about("nosuchuser")
    assert calls == ["/user/spez/about", "/user/nosuchuser/about", "/user/nosuchuser/about"]
