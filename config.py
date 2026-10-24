"""Configuration for the Reddit Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT                     port the API listens on (default 8000)
    REDDIT_PROXY             proxy URL for every request, e.g. http://user:pass@host:port
                             (default: none — direct). Reddit allows about 100 requests
                             per 10 minutes from one IP address, and walls most cloud /
                             datacenter IPs for comment pages. Below that limit, from a
                             home connection, you need nothing; otherwise point this at
                             a ROTATING residential proxy — the scraper drops a connection
                             (and so gets a fresh exit IP) as soon as Reddit rate-limits
                             or walls it.
    REDDIT_CHROME_PATH       comments only: an installed Chrome to use instead of
                             patchright's Chromium (e.g. /usr/bin/google-chrome)
    REDDIT_BROWSER_HEADLESS  comments only: 1 = headless. Reddit walls headless Chrome,
                             so leave it off; on a server without a display run
                             `xvfb-run python run.py` instead.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

REDDIT_PROXY = os.environ.get("REDDIT_PROXY") or None

# Reddit's official Android app identity: its anonymous sign-in returns the
# 24-hour token every request carries.
REDDIT_CLIENT_ID = "ohXpoqrZYub1kg"
REDDIT_APP_USER_AGENT = "Reddit/Version 2025.20.0/Build 2352930/Android 14"
REDDIT_TOKEN_TTL = 82800          # re-mint the token after 23 h (issued for 24 h)

# Comment trees come from www.reddit.com through a real Chrome (reddit/browser.py).
REDDIT_CHROME_PATH = os.environ.get("REDDIT_CHROME_PATH") or None
REDDIT_BROWSER_HEADLESS = os.environ.get("REDDIT_BROWSER_HEADLESS", "") == "1"
REDDIT_BROWSER_ATTEMPTS = 3       # browser (re)starts per request when Reddit walls the IP


def reddit_proxy():
    return os.environ.get("REDDIT_PROXY") or None
