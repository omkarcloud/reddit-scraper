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
     -> the full legacy JSON API: listings, /comments/<id>, /api/morechildren,
        /search, /user/<name>/*, /r/<sr>/about[/rules|/wiki], /api/info,
        /api/similar_subreddits, /subreddits/*, /api/subreddit_autocomplete_v2.
     www.reddit.com/*.json and api.reddit.com stay 403 even with the bearer;
     ONLY oauth.reddit.com serves JSON.
  3. The same bearer + app UA also renders www.reddit.com shreddit HTML and
     the /svc/shreddit/* partials without a captcha. Used only for what the
     JSON API lacks anonymously: comment search, media search, the explore
     leaderboard (www.py).

Rate limit (measured): 100 oauth requests per 10 minutes PER CLIENT IP —
request 101 is a 429 with an empty body until `x-ratelimit-reset`. The
bucket is per IP, not per token (a new token on the same IP is still 429),
and the same token from a new IP gets a fresh 100. The token mint and the
www pages have their own 200/10 min counters. Hence:

  * ONE bearer per process (re-minted after REDDIT_TOKEN_TTL or on a 401).
  * Direct egress first. Its budget is shared by every thread of the pod,
    so exhaustion (remaining <= RESERVE, or a 429) is process-global: every
    thread moves to residential exits for REDDIT_DIRECT_COOLDOWN seconds.
  * Each thread on a proxied session holds a sticky residential exit and
    rotates it (new port -> new IP) after REDDIT_EXIT_BUDGET requests, when
    the exit's remaining budget runs out, or on a 429.
  * REDDIT_PROXY_COUNTRY set -> proxied from the first request.

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
RESERVE = 2                 # rotate the exit when x-ratelimit-remaining drops to this
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


class RedditBadRequest(BadRequest):
    """Upstream 400, or a redirect to a surface the API does not serve
    anonymously — never retried."""


class RedditNotFound(NotFound):
    """The subreddit / post / user / page does not exist, or is banned,
    private or quarantined — never retried."""


class RedditForbidden(RedditNotFound):
    """Reddit only serves this to a logged-in user (moderator lists, flair
    templates, saved items) — reported like a 404 to the caller."""


# ---- token -----------------------------------------------------------------------------

_token_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}


def _mint_token(session):
    """POST the installed_client grant -> bearer string. Uses the calling
    thread's session (direct or proxied — the mint works from both)."""
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
        raise RedditRateLimited("token endpoint rate-limited")
    if resp.status_code != 200:
        dump_debug("token_error", resp.text)
        raise RedditUpstreamError(f"HTTP {resp.status_code} minting the app token")
    try:
        payload = resp.json()
    except ValueError:
        dump_debug("token_nonjson", resp.text)
        raise RedditBlocked("token endpoint returned a non-JSON body")
    token = payload.get("access_token")
    if not token:
        raise RedditUpstreamError(f"token endpoint answered without a token: {str(payload)[:200]}")
    return token


def bearer(force=False):
    """The process-wide app bearer, minted on first use and re-minted after
    REDDIT_TOKEN_TTL seconds (or immediately with force=True after a 401)."""
    now = time.time()
    if not force and _token["value"] and _token["expires"] > now:
        return _token["value"]
    with _token_lock:
        if not force and _token["value"] and _token["expires"] > time.time():
            return _token["value"]
        token = _mint_token(_session())
        _token["value"] = token
        _token["expires"] = time.time() + config.REDDIT_TOKEN_TTL
        return token


# ---- sessions / exits ----------------------------------------------------------------------
# One curl session per worker thread (a curl handle must not be shared across
# threads). The direct egress is one IP for the whole pod, so its rate-limit
# state is process-global; proxied exits are per thread.

_local = threading.local()
_direct_lock = threading.Lock()
_direct = {"blocked_until": 0.0}


def _direct_available():
    return _direct["blocked_until"] <= time.time()


def _use_proxy():
    if config.reddit_proxy() is not None:
        return True
    return config.reddit_fallback_proxy() is not None and not _direct_available()


def _session():
    want_proxy = _use_proxy()
    sess = getattr(_local, "session", None)
    if sess is not None and getattr(sess, "_rd_proxied", False) != want_proxy:
        _drop_session()
        sess = None
    if sess is None:
        from curl_cffi import requests as curl_requests
        sess = curl_requests.Session(impersonate=IMPERSONATE)
        proxy = None
        if want_proxy:
            proxy = config.reddit_proxy() or config.reddit_fallback_proxy()
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        sess._rd_proxied = bool(proxy)
        sess._rd_used = 0
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


def _exhaust(sess, reset_seconds=None):
    """This session's exit is out of budget: rest the direct egress for the
    cooldown (process-wide) or drop the proxied session so the next call
    lands on a fresh sticky port."""
    if getattr(sess, "_rd_proxied", False):
        _drop_session()
        return
    wait = config.REDDIT_DIRECT_COOLDOWN
    if reset_seconds is not None:
        wait = min(wait, max(reset_seconds, 5) + 5)
    with _direct_lock:
        _direct["blocked_until"] = max(_direct["blocked_until"], time.time() + wait)
    _drop_session()


def _account(sess, resp):
    """Bookkeeping after every oauth/www response: rotate a proxied exit at
    its budget, and treat a nearly spent bucket as spent (the next call
    would be the 429)."""
    sess._rd_used += 1
    remaining = _header_float(resp, "x-ratelimit-remaining")
    reset = _header_float(resp, "x-ratelimit-reset")
    if remaining is not None and remaining <= RESERVE:
        _exhaust(sess, reset)
    elif getattr(sess, "_rd_proxied", False) and sess._rd_used >= config.REDDIT_EXIT_BUDGET:
        _drop_session()


def _header_float(resp, name):
    value = resp.headers.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except (RedditBadRequest, RedditNotFound):
            raise
        except RedditBlocked as e:
            last = e            # _exhaust already moved this thread to a new exit
        except RedditUpstreamError as e:
            last = e
            _drop_session()
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def _looks_like_wall(text):
    head = (text or "")[:4000]
    return ("blocked by network security" in head or "Prove your humanity" in head
            or "captcha" in head.lower() and "<html" in head.lower())


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
        headers = {
            "user-agent": config.REDDIT_APP_USER_AGENT,
            "authorization": "Bearer " + bearer(),
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
            _exhaust(sess, _header_float(resp, "x-ratelimit-reset"))
            raise RedditRateLimited(f"rate-limited on {label}")
        _account(sess, resp)
        if status == 401:
            bearer(force=True)
            raise RedditUpstreamError(f"app token rejected on {label} (re-minted)")
        if status in (301, 302, 303, 307, 308):
            target = resp.headers.get("location") or ""
            if target.startswith(OAUTH + "/") and not getattr(sess, "_rd_followed", False):
                # e.g. /about/sticky -> /r/<sr>/comments/<id>/<slug>/.json: served here
                try:
                    sess._rd_followed = True
                    resp = sess.get(target, headers=headers, timeout=JSON_TIMEOUT, allow_redirects=False)
                finally:
                    sess._rd_followed = False
                status = resp.status_code
                text = resp.text or ""
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
            if status == 403 and _looks_like_wall(text):
                dump_debug("wall", text)
                _exhaust(sess)
                raise RedditBlocked(f"network-security wall on {label}")
            dump_debug("nonjson", text)
            raise RedditUpstreamError(f"HTTP {status} with a non-JSON body on {label}")
        try:
            payload = json.loads(text)
        except ValueError:
            dump_debug("badjson", text)
            raise RedditUpstreamError(f"could not parse JSON from {label}")
        if status == 404:
            raise RedditNotFound(f"{label} not found")
        if status == 403:
            reason = payload.get("reason") if isinstance(payload, dict) else None
            if reason in ("private", "gold_only", "quarantined", "banned", "gated"):
                raise RedditNotFound(f"{label} is {reason.replace('_', ' ')}")
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

def get_www(path, params=None, *, label=None):
    """GET a www.reddit.com page or /svc/shreddit partial with the app bearer
    -> HTML text. A captcha or netsec page is a RedditBlocked (fresh exit)."""
    label = label or path
    url = WWW + path
    query = {k: v for k, v in (params or {}).items() if v not in (None, "")}

    def once():
        sess = _session()
        headers = dict(PAGE_HEADERS)
        headers["user-agent"] = config.REDDIT_APP_USER_AGENT
        headers["authorization"] = "Bearer " + bearer()
        try:
            resp = sess.get(url, params=query, headers=headers, timeout=PAGE_TIMEOUT, allow_redirects=True)
        except Exception as e:
            raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 429:
            _exhaust(sess, _header_float(resp, "x-ratelimit-reset"))
            raise RedditRateLimited(f"rate-limited on {label}")
        _account(sess, resp)
        if status == 401:
            bearer(force=True)
            raise RedditUpstreamError(f"app token rejected on {label} (re-minted)")
        if status == 404:
            raise RedditNotFound(f"{label} not found")
        if status in RETRY_STATUSES:
            raise RedditUpstreamError(f"HTTP {status} on {label}")
        if _looks_like_wall(text):
            dump_debug("www_wall", text)
            _exhaust(sess)
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
        headers = dict(PAGE_HEADERS)
        headers["user-agent"] = config.REDDIT_APP_USER_AGENT
        headers["authorization"] = "Bearer " + bearer()
        try:
            resp = sess.get(url, headers=headers, timeout=PAGE_TIMEOUT, allow_redirects=False)
        except Exception as e:
            raise RedditUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        if status == 429:
            _exhaust(sess, _header_float(resp, "x-ratelimit-reset"))
            raise RedditRateLimited(f"rate-limited resolving {path}")
        _account(sess, resp)
        if status == 404:
            raise RedditNotFound(f"{path} not found")
        if status in (301, 302, 303, 307, 308):
            return resp.headers.get("location") or None
        if _looks_like_wall(resp.text or ""):
            _exhaust(sess)
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
        resp = curl_requests.get(base + "/DASHPlaylist.mpd", headers={"accept": "*/*"},
                                 impersonate=IMPERSONATE, timeout=20)
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
