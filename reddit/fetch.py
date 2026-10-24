"""Reddit transport: plain curl_cffi, no browser, no login.

Anonymous reddit.com is walled for EVERY client and egress in Sept 2026:
each `.json` URL on www/old/api.reddit.com answers 403 "You've been blocked
by network security" and each HTML page answers a reCAPTCHA ("Prove your
humanity") — probed 2026-09-22 from direct egress, residential and
datacenter proxy exits in six countries, every curl_cffi
impersonation and a real headed Chrome (patchright). The wall is Reddit's
own, not Cloudflare, so the patchright pattern does not help either.

What IS open from everywhere is the mobile app's anonymous OAuth flow:

  1. POST https://www.reddit.com/api/v1/access_token
       Authorization: Basic base64("<android client id>:")
       User-Agent: Reddit/Version 2025.20.0/Build 2352930/Android 14
       grant_type=https://oauth.reddit.com/grants/installed_client&device_id=<uuid4>
     -> {"access_token": ..., "expires_in": 86400, "scope": "*"}
  2. GET https://oauth.reddit.com/<legacy path without .json>?raw_json=1
       Authorization: Bearer <token>        (any User-Agent works from here)
     -> the legacy JSON API: listings, /comments/<id>[/_/<comment>], /by_id (ONE id),
        /search, /user/<name>/*, /r/<sr>/about[/rules|/wiki],
        /api/user_data_by_account_ids, /api/similar_subreddits, /subreddits/*,
        /api/subreddit_autocomplete_v2.
     Since 2026-10-16 /api/info (every form) and /api/morechildren answer
     this bearer with the 403 "blocked by network security" page (every app
     client id, every UA): shared.py hydrates by id through what is left.
     Since 2026-10-20 20:00 UTC /comments/<id> in every form (bare,
     /r/<sr>/comments/<id>, /comments/<post>/_/<comment>, any params, any
     exit) answers the same page, while /by_id,
     /duplicates, listings, /search and /about still answer JSON. Comment
     trees therefore come from www.reddit.com through a local Chrome
     (shreddit.py, browser.py); posts/details and
     posts/media read the post from /by_id.
     www.reddit.com/*.json and api.reddit.com stay 403 even with the bearer;
     ONLY oauth.reddit.com serves JSON.

     That wall page is 190 KB of UNCOMPRESSED HTML (no content-encoding)
     with its marker text in the last 600 bytes, whereas a JSON answer is
     5-90 KB gzip on the wire. The wall is ALSO a verdict on the token and
     the exit that asked (2026-10-23: /search walled for the server's
     bearer on every exit for hours while a freshly minted bearer passed),
     so a wall discards both and the retry asks with new ones.
  3. The same bearer + app UA also renders www.reddit.com shreddit HTML and
     the /svc/shreddit/* partials without a captcha. Used only for what the
     JSON API lacks anonymously: comment search, media search, the explore
     leaderboard (www.py).

Rate limit (measured): 100 oauth requests per 10 minutes PER CLIENT IP —
request 101 is a 429 with an empty body until `x-ratelimit-reset`. The
bucket is per IP, not per token (a new token on the same IP is still 429),
and the same token from a new IP gets a fresh 100. The token mint and the
www pages have their own 200/10 min counters. The limit is not paced or
budgeted (2026-10-23): a token and an exit are used until
Reddit answers them with anything but data, and then both are forgotten.

  * ONE bearer per process, used until Reddit rejects it (401, 429, a wall
    page) or REDDIT_TOKEN_TTL passes; the thread that saw the rejection
    mints the replacement on its fresh exit, the other threads pick it up.
  * Every request — token mint, oauth, www, v.redd.it manifests — goes
    through REDDIT_PROXY when it is set (config.py), direct otherwise.
  * Each thread holds its own sticky exit for as long as Reddit serves it:
    it is dropped (new port -> new IP) on a 429, a wall page, a 401 or a
    transport failure, never on a request count.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  RedditUpstreamError  transport failure / 5xx / unparsable body   — retryable
  RedditBlocked        captcha or netsec HTML where JSON/HTML was due — retryable on a new exit
  RedditRateLimited    429 (a Blocked)                            — retryable on a new exit
  RedditBadRequest     upstream 400 / unsupported redirect         — never retried
  RedditNotFound       404, banned / private / quarantined / gone  — never retried
  RedditForbidden      needs a logged-in user (a NotFound to the   — never retried
                       caller: the data is not available anonymously)
"""
import base64
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError

WWW = "https://www.reddit.com"
OAUTH = "https://oauth.reddit.com"
TOKEN_URL = WWW + "/api/v1/access_token"
IMPERSONATE = "chrome"

JSON_TIMEOUT = 40
PAGE_TIMEOUT = 60           # shreddit pages are 300-800 KB
TOKEN_TIMEOUT = 30
FANOUT_WORKERS = 6
RETRY_STATUSES = (500, 502, 503, 504)

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}


class RedditUpstreamError(UpstreamError):
    """Transport failure, 5xx or an unparsable body — retryable."""


class RedditBlocked(RedditUpstreamError, Blocked):
    """A captcha / network-security page where data was expected —
    retryable on a fresh exit."""


class RedditRateLimited(RedditBlocked):
    """HTTP 429: this exit's 10-minute budget is spent."""


class RedditTokenRejected(RedditBlocked):
    """HTTP 401: the bearer is dead — a new one was minted, retry at once."""


class _Reasoned:
    """A machine-readable `reason` code (and optional details such as the
    subreddit / user involved) next to the human message; route_glue adds
    both to the error body."""

    default_reason = None

    def __init__(self, message="", reason=None, **details):
        super().__init__(message)
        self.reason = reason or self.default_reason
        self.details = {k: v for k, v in details.items() if v is not None}


class RedditBadRequest(_Reasoned, BadRequest):
    """Upstream 400, or a redirect to a surface the API does not serve
    anonymously — never retried."""


class RedditNotFound(_Reasoned, NotFound):
    """The subreddit / post / user / page does not exist, or is banned,
    private or quarantined — never retried."""


class RedditForbidden(RedditNotFound):
    """Reddit only serves this to a logged-in user (moderator lists, flair
    templates, saved items) — reported like a 404 to the caller."""

    default_reason = "login_required"


# Reddit's own `reason` on a 403 / 404 JSON body. Every one of them is about
# the SUBREDDIT in the path (a user profile is the subreddit u_<name>).
SUBREDDIT_REASONS = {
    "banned": "subreddit_banned",
    "private": "subreddit_private",
    "gold_only": "subreddit_gold_only",
    "quarantined": "subreddit_quarantined",
    "gated": "subreddit_gated",
}


# ---- token -----------------------------------------------------------------------------

_token_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}


def _mint_token(session):
    """POST the installed_client grant -> bearer string. Uses the calling
    thread's (always proxied) session."""
    basic = base64.b64encode(f"{config.REDDIT_CLIENT_ID}:".encode()).decode()
    headers = {
        "user-agent": config.REDDIT_APP_USER_AGENT,
        "authorization": "Basic " + basic,
        "accept-language": "en-US",
        "x-reddit-device-id": str(uuid.uuid4()),
        "client-vendor-id": str(uuid.uuid4()),
    }
    body = {"grant_type": "https://oauth.reddit.com/grants/installed_client",
            "device_id": str(uuid.uuid4())}
    try:
        resp = session.post(TOKEN_URL, data=body, headers=headers, timeout=TOKEN_TIMEOUT)
    except Exception as e:
        raise RedditUpstreamError(f"token request failed: {type(e).__name__}: {e}")
    if resp.status_code == 429:
        _drop_session()     # retry the mint on a fresh exit, not the same spent one
        raise RedditRateLimited("token endpoint rate-limited")
    if resp.status_code != 200:
        dump_debug("token_error", resp.text)
        raise RedditUpstreamError(f"HTTP {resp.status_code} minting the app token")
    try:
        payload = resp.json()
    except ValueError:
        dump_debug("token_nonjson", resp.text)
        _drop_session()     # a Blocked is retried on a fresh exit (see _retrying)
        raise RedditBlocked("token endpoint returned a non-JSON body")
    token = payload.get("access_token")
    if not token:
        raise RedditUpstreamError(f"token endpoint answered without a token: {str(payload)[:200]}")
    return token


def bearer(force=False, replace=None):
    """The process-wide app bearer, minted on first use and re-minted after
    REDDIT_TOKEN_TTL seconds, with force=True, or with replace=<token>
    when Reddit rejected `token`: then a new one is minted only if `token`
    is still the current bearer, so ten threads rejected at once cause one
    mint, and the nine others pick up the first thread's replacement."""
    now = time.time()
    current = _token["value"]
    if replace is not None and current != replace:
        return bearer()
    if not force and replace is None and current and _token["expires"] > now:
        return current
    with _token_lock:
        current = _token["value"]
        if replace is not None and current != replace:
            return current
        if not force and replace is None and current and _token["expires"] > time.time():
            return current
        _token["value"] = None          # a failed mint leaves no token: the next caller mints again
        token = _mint_token(_session())
        _token["value"] = token
        _token["expires"] = time.time() + config.REDDIT_TOKEN_TTL
        return token


# ---- sessions / exits ----------------------------------------------------------------------
# One curl session per worker thread (a curl handle must not be shared across
# threads), kept until Reddit rejects it. With REDDIT_PROXY set every session
# goes through it (a rotating proxy hands each new session a fresh IP);
# without it they go direct.

_local = threading.local()


def _proxy():
    """The proxy for a new session (REDDIT_PROXY), or None = direct."""
    return config.reddit_proxy()


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        from curl_cffi import requests as curl_requests
        proxy = _proxy()
        sess = curl_requests.Session(impersonate=IMPERSONATE)
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        sess._rd_proxied = bool(proxy)
        _local.session = sess
    return sess


def _drop_session():
    sess = getattr(_local, "session", None)
    _local.session = None
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _discard(token):
    """Reddit answered this thread's exit and `token` with something other
    than data (429, 401, a wall page): forget both. The exit is dropped so
    the retry lands on a fresh one, and the bearer is replaced — minted on
    that fresh exit — unless another thread already replaced it."""
    _drop_session()
    try:
        bearer(replace=token)
    except RedditUpstreamError:
        pass            # the retry mints again through its own headers


def dump_debug(name, text):
    """Write a raw response to $REDDIT_DEBUG_DIR/<name>.txt."""
    dbg = os.environ.get("REDDIT_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w") as f:
                f.write(text)
        except OSError:
            pass


def _retrying(fn):
    """Up to MAX_RETRIES attempts. A Blocked (429, 401, wall) already
    discarded the exit and the token, so the retry goes straight out on
    new ones; only a transport failure / 5xx waits RETRY_BACKOFF first."""
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except (RedditBadRequest, RedditNotFound):
            raise
        except RedditBlocked as e:
            last = e
            continue
        except RedditUpstreamError as e:
            last = e
            _drop_session()
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def _looks_like_wall(text, status=None):
    """A captcha / network-security page. The markers are looked for in the
    first 4 KB (a 200 shreddit page may legitimately quote them further
    down); on a 403 the whole body is searched too, because the oauth wall
    page carries "blocked by network security" at the very END of 190 KB
    of inline CSS and base64 images (first seen 2026-10-23)."""
    text = text or ""
    head = text[:4000]
    if ("blocked by network security" in head or "Prove your humanity" in head
            or "captcha" in head.lower() and "<html" in head.lower()):
        return True
    return status == 403 and "blocked by network security" in text


# ---- oauth JSON ---------------------------------------------------------------------------

def _describe(payload):
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("reason") or payload.get("explanation") or payload)[:200]
    return str(payload)[:200]


def get_json(path, params=None, *, label=None, method="GET", data=None):
    """One oauth.reddit.com call -> parsed JSON. `path` is the legacy path
    without `.json` (e.g. "/r/python/hot"); raw_json=1 is always sent so
    HTML entities come back unescaped."""
    label = label or path
    query = {"raw_json": 1}
    query.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = OAUTH + path

    def once():
        sess = _session()
        token = bearer()
        headers = {
            "user-agent": config.REDDIT_APP_USER_AGENT,
            "authorization": "Bearer " + token,
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
        }
        try:
            if method == "POST":
                resp = sess.post(url, params=query, data=data, headers=headers, timeout=JSON_TIMEOUT, allow_redirects=False)
            else:
                resp = sess.get(url, params=query, headers=headers, timeout=JSON_TIMEOUT, allow_redirects=False)
        except Exception as e:
            raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 429:
            _discard(token)
            raise RedditRateLimited(f"rate-limited on {label}")
        if status == 401:
            _discard(token)
            raise RedditTokenRejected(f"app token rejected on {label} (re-minted)")
        if status in (301, 302, 303, 307, 308):
            target = resp.headers.get("location") or ""
            if "/subreddits/search" in target:
                # an unknown subreddit redirects to the subreddit search
                # listing (PRAW's Redirect); following it would parse search
                # results as the requested data
                raise RedditNotFound(f"{label} not found", reason="subreddit_not_found")
            if target.startswith(OAUTH + "/"):
                # e.g. /about/sticky -> /r/<sr>/comments/<id>/<slug>/.json: served here.
                try:
                    resp = sess.get(target, params=None if "raw_json=" in target else {"raw_json": 1},
                                    headers=headers, timeout=JSON_TIMEOUT, allow_redirects=False)
                except Exception as e:
                    raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
                status = resp.status_code
                text = resp.text or ""
                if status == 429:
                    _discard(token)
                    raise RedditRateLimited(f"rate-limited on {label}")
                if status == 401:
                    _discard(token)
                    raise RedditTokenRejected(f"app token rejected on {label} (re-minted)")
                if status in (301, 302, 303, 307, 308):
                    raise RedditBadRequest(f"{label} is not served anonymously")
            else:
                raise RedditBadRequest(f"{label} is not served anonymously (redirected to {target[:120] or 'www'})")
        if status in RETRY_STATUSES:
            raise RedditUpstreamError(f"HTTP {status} on {label}")
        stripped = text.lstrip()
        if not stripped.startswith(("{", "[")):
            if status == 404:
                raise RedditNotFound(f"{label} not found")
            if status == 403 and _looks_like_wall(text, status):
                dump_debug("wall", text)
                _discard(token)
                raise RedditBlocked(f"network-security wall on {label}")
            dump_debug("nonjson", text)
            raise RedditUpstreamError(f"HTTP {status} with a non-JSON body on {label}")
        try:
            payload = json.loads(text)
        except ValueError:
            dump_debug("badjson", text)
            raise RedditUpstreamError(f"could not parse JSON from {label}")
        reason = payload.get("reason") if isinstance(payload, dict) and status in (403, 404) else None
        if reason in SUBREDDIT_REASONS:
            raise RedditNotFound(f"{label} is {reason.replace('_', ' ')}", reason=SUBREDDIT_REASONS[reason])
        if status == 404:
            raise RedditNotFound(f"{label} not found")
        if status == 403:
            raise RedditForbidden(f"{label} is only available to logged-in users")
        if status == 400:
            raise RedditBadRequest(f"reddit rejected {label}: {_describe(payload)}")
        if status != 200:
            raise RedditUpstreamError(f"HTTP {status} on {label}: {_describe(payload)}")
        errors = payload.get("json", {}).get("errors") if isinstance(payload, dict) else None
        if errors:
            code = str(errors[0][0]) if errors[0] else "ERROR"
            if code in ("USER_REQUIRED",):
                raise RedditForbidden(f"{label} is only available to logged-in users")
            raise RedditBadRequest(f"reddit rejected {label}: {errors[0]}")
        return payload

    return _retrying(once)


# ---- www shreddit ---------------------------------------------------------------------------

def get_www(path, params=None, *, label=None, over18=False):
    """GET a www.reddit.com page or /svc/shreddit partial with the app bearer
    -> HTML text. A captcha or netsec page is a RedditBlocked (fresh exit).
    `over18` sends the `over18=1` preference cookie, the only thing that
    makes the search partials include NSFW posts (there is no query flag)."""
    label = label or path
    url = WWW + path
    query = {k: v for k, v in (params or {}).items() if v not in (None, "")}

    def once():
        sess = _session()
        token = bearer()
        headers = dict(PAGE_HEADERS)
        headers["user-agent"] = config.REDDIT_APP_USER_AGENT
        headers["authorization"] = "Bearer " + token
        if over18:
            headers["cookie"] = "over18=1"
        try:
            resp = sess.get(url, params=query, headers=headers, timeout=PAGE_TIMEOUT, allow_redirects=True)
        except Exception as e:
            raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 429:
            _discard(token)
            raise RedditRateLimited(f"rate-limited on {label}")
        if status == 401:
            _discard(token)
            raise RedditTokenRejected(f"app token rejected on {label} (re-minted)")
        if status == 404:
            raise RedditNotFound(f"{label} not found")
        if status in RETRY_STATUSES:
            raise RedditUpstreamError(f"HTTP {status} on {label}")
        if _looks_like_wall(text, status):
            dump_debug("www_wall", text)
            _discard(token)
            raise RedditBlocked(f"captcha wall on {label}")
        if status == 403:
            raise RedditForbidden(f"{label} is only available to logged-in users")
        if status != 200:
            raise RedditUpstreamError(f"HTTP {status} on {label}")
        return text

    return _retrying(once)


def resolve_redirect(path):
    """Where a www.reddit.com path redirects to (share links /r/<sr>/s/<code>
    -> the post's permalink), or None when it does not redirect."""
    url = WWW + path

    def once():
        sess = _session()
        token = bearer()
        headers = dict(PAGE_HEADERS)
        headers["user-agent"] = config.REDDIT_APP_USER_AGENT
        headers["authorization"] = "Bearer " + token
        try:
            resp = sess.get(url, headers=headers, timeout=PAGE_TIMEOUT, allow_redirects=False)
        except Exception as e:
            raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        if status == 429:
            _discard(token)
            raise RedditRateLimited(f"rate-limited resolving {path}")
        if status == 401:
            _discard(token)
            raise RedditTokenRejected(f"app token rejected resolving {path} (re-minted)")
        if status == 404:
            raise RedditNotFound(f"{path} not found")
        if status in (301, 302, 303, 307, 308):
            return resp.headers.get("location") or None
        if _looks_like_wall(resp.text or "", status):
            _discard(token)
            raise RedditBlocked(f"captcha wall on {path}")
        return None

    return _retrying(once)


def get_video_manifest(base):
    """GET <v.redd.it base>/DASHPlaylist.mpd -> XML text, or None on any
    failure (media stays usable from the post's own fields). v.redd.it is a
    plain CDN: no token, no Reddit rate limit, but an HTML-first Accept
    header redirects to the captcha-walled www, hence `*/*`."""
    try:
        from curl_cffi import requests as curl_requests
        proxy = _proxy()
        resp = curl_requests.get(base + "/DASHPlaylist.mpd", headers={"accept": "*/*"},
                                 impersonate=IMPERSONATE, timeout=20,
                                 proxies={"http": proxy, "https": proxy} if proxy else None)
    except Exception:
        return None
    if resp.status_code != 200 or "<MPD" not in (resp.text or "")[:500]:
        return None
    return resp.text


# ---- fan-out ------------------------------------------------------------------------------

def run_parallel(fns):
    """Run zero-arg callables in parallel; results align with `fns`. The
    first exception propagates."""
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    with ThreadPoolExecutor(max_workers=min(FANOUT_WORKERS, len(fns))) as ex:
        futures = [ex.submit(fn) for fn in fns]
        return [f.result() for f in futures]


if __name__ == "__main__":
    # Smoke test: python -m reddit.fetch [subreddit]
    name = sys.argv[1] if len(sys.argv) > 1 else "python"
    listing = get_json(f"/r/{name}/hot", {"limit": 3})
    for child in listing.get("data", {}).get("children", []):
        d = child.get("data", {})
        print(d.get("id"), d.get("score"), d.get("title"))
