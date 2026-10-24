"""A local Chrome for the comment trees (reddit/shreddit.py).

Since October 2026 Reddit's JSON API answers /comments/<id> and
/r/<sub>/comments with its "blocked by network security" page (every
token, every IP), while www.reddit.com still renders comment trees for a
real browser. So one Chrome (patchright: a Playwright build that does not
look automated) is started on first use, through REDDIT_PROXY when set,
opens r/python once, and from then on every request is an in-page fetch()
that carries the page's cookies and TLS.

What Reddit checks (measured 2026-10-24):
  * the browser must be HEADED. Headless Chrome gets a JavaScript challenge
    and then the wall on every request. On a desktop the window is placed
    off-screen; on a Linux server run under a virtual display
    (`xvfb-run python run.py`, which the Dockerfile does).
  * the IP. Reddit judges it on the first page load: a walled IP (most
    cloud / datacenter ranges) answers that page with HTTP 403, and every
    request after it too. The browser is then closed and started again —
    a rotating proxy hands the new one a fresh IP — up to
    REDDIT_BROWSER_ATTEMPTS times per request.

Install once:   python -m patchright install chromium
"""
import queue
import threading
from urllib.parse import unquote, urlsplit

import config
from .fetch import RETRY_STATUSES, RedditBlocked, RedditUpstreamError

WWW = "https://www.reddit.com"
PARK_URL = WWW + "/r/python/"
PARTIAL_ACCEPT = "text/vnd.reddit.partial+html, text/html;q=0.9"
WALL_MARKERS = ("blocked by network security", "prove your humanity")
SKIPPED_RESOURCES = ("image", "media", "font")      # never needed for an in-page fetch
CALL_TIMEOUT = 150                                   # seconds one browser job may take
OFF_SCREEN = ["--window-position=-32000,-32000", "--window-size=1280,900"]

_FETCH_JS = """async ({url, method, headers, body}) => {
  const r = await fetch(url, {method, headers, body, credentials: 'include'});
  return {status: r.status, body: await r.text()};
}"""


class BrowserMissing(RedditUpstreamError):
    """patchright or its Chromium is not installed."""


def _playwright_proxy(url):
    """http://user:pass@host:port -> Playwright's {server, username, password}."""
    u = urlsplit(url)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out.update(username=unquote(u.username), password=unquote(u.password or ""))
    return out


def _skip_heavy(route):
    if route.request.resource_type in SKIPPED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def walled(text):
    """Reddit's wall / captcha page (its text sits at the END of 190 KB)."""
    low = (text or "").lower()
    head, tail = low[:8000], low[-4000:]
    return any(m in head or m in tail for m in WALL_MARKERS)


class _Chrome:
    """One Chrome, driven from one thread: Playwright's sync API must stay on
    the thread that started it, and the API server answers on 16 threads,
    so requests queue here and run one at a time."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._start_lock = threading.Lock()
        self._pw = self._browser = self._page = None

    def call(self, fn):
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="reddit-chrome", daemon=True)
                self._thread.start()
        done, box = threading.Event(), {}
        self._jobs.put((fn, box, done))
        if not done.wait(CALL_TIMEOUT):
            raise RedditUpstreamError(f"the local Chrome did not answer within {CALL_TIMEOUT}s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _loop(self):
        while True:
            fn, box, done = self._jobs.get()
            try:
                box["result"] = fn()
            except BaseException as e:      # handed back to the caller
                box["error"] = e
            finally:
                done.set()

    # ---- the rest runs on the browser thread ----------------------------------------------------

    def page(self):
        if self._page is not None:
            return self._page
        if self._pw is None:
            try:
                from patchright.sync_api import sync_playwright
            except ImportError:
                raise BrowserMissing("comments need a browser: pip install patchright && python -m patchright install chromium")
            self._pw = sync_playwright().start()
        kw = {"headless": config.REDDIT_BROWSER_HEADLESS}
        if not kw["headless"]:
            kw["args"] = OFF_SCREEN
        if config.REDDIT_CHROME_PATH:
            kw["executable_path"] = config.REDDIT_CHROME_PATH
        proxy = config.reddit_proxy()
        if proxy:
            kw["proxy"] = _playwright_proxy(proxy)
        try:
            self._browser = self._pw.chromium.launch(**kw)
        except Exception as e:
            hint = (" — on a server without a display run it under xvfb-run" if "display" in str(e).lower()
                    else " — run: python -m patchright install chromium")
            raise BrowserMissing(f"could not start Chrome ({str(e).splitlines()[0][:160]}){hint}")
        context = self._browser.new_context(locale="en-US")
        context.route("**/*", _skip_heavy)
        page = context.new_page()
        try:
            resp = page.goto(PARK_URL, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)   # Reddit's JS check reloads the page once
            except Exception:
                pass
        except Exception as e:
            self.close()
            raise RedditBlocked(f"Chrome could not open www.reddit.com ({str(e).splitlines()[0][:160]})")
        if resp is not None and resp.status in (403, 429):
            self.close()
            raise RedditBlocked(f"Reddit walls this IP (HTTP {resp.status} on www.reddit.com); "
                                "set REDDIT_PROXY to a rotating residential proxy")
        self._page = page
        return page

    def fetch(self, url, method, headers, body):
        page = self.page()
        for attempt in range(2):
            try:
                return page.evaluate(_FETCH_JS, {"url": url, "method": method, "headers": headers, "body": body})
            except Exception as e:
                if attempt or "Execution context was destroyed" not in str(e):
                    raise
                page.wait_for_load_state("load", timeout=15_000)      # a late navigation of the parked page

    def close(self):
        browser, self._browser, self._page = self._browser, None, None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


_CHROME = _Chrome()


def page_fetch(path, label, method="GET", body=None, partial=True):
    """One in-page fetch of www.reddit.com<path> -> (status, html). A walled
    IP, a 429 or the wall page reopens the browser (a rotating proxy gives
    it a fresh IP) and asks again, up to REDDIT_BROWSER_ATTEMPTS times."""
    headers = {"accept": PARTIAL_ACCEPT if partial else "text/html"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    last = None
    for _ in range(max(1, int(config.REDDIT_BROWSER_ATTEMPTS))):
        try:
            res = _CHROME.call(lambda: _CHROME.fetch(WWW + path, method, headers, body)) or {}
        except BrowserMissing:
            raise
        except RedditUpstreamError as e:        # walled IP on the first page load, or a timeout
            last = e
            continue
        except Exception as e:                  # Chrome crashed or the page broke: start over
            _CHROME.call(_CHROME.close)
            last = RedditUpstreamError(f"{label}: the local Chrome failed ({type(e).__name__}: {str(e)[:160]})")
            continue
        status, text = int(res.get("status") or 0), res.get("body") or ""
        if status == 429 or walled(text):
            _CHROME.call(_CHROME.close)
            last = RedditBlocked(f"{label}: Reddit refused the browser (HTTP {status})")
            continue
        if status in RETRY_STATUSES or status == 0:
            last = RedditUpstreamError(f"HTTP {status} from www.reddit.com on {label}")
            continue
        return status, text
    raise last
