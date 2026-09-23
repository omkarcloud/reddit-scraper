"""Configuration for the Reddit Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT          port the API listens on (default 8000)
    REDDIT_PROXY  proxy URL for every request, e.g. http://user:pass@host:port
                  (default: none — direct). Reddit allows about 100 requests
                  per 10 minutes from one IP address. Below that you need
                  nothing; above it, point this at a ROTATING residential
                  proxy — the scraper opens a fresh connection (and so gets a
                  fresh exit IP) every REDDIT_EXIT_BUDGET requests or as soon
                  as an IP's budget runs out.

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

# Rate-limit handling (100 requests / 10 min per IP).
REDDIT_DIRECT_COOLDOWN = 660      # seconds a rate-limited direct IP rests
REDDIT_EXIT_BUDGET = 90           # requests per proxy connection before rotating


def reddit_proxy():
    return os.environ.get("REDDIT_PROXY") or None


def reddit_fallback_proxy():
    # No automatic fallback here: set REDDIT_PROXY to route every request
    # through your own (rotating) proxy instead.
    return None
