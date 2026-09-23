"""Cache TTL per /reddit/* endpoint (cache.py, keyed on the validated
params — `subreddit=python`, `subreddit=r/Python` and a pasted link all
validate to the same key).

Reddit is a live feed: scores and comment counts move by the minute on hot
posts and the sort orders reshuffle constantly, so listings stay short.
Subreddit / user metadata, rules, wiki pages and the similar-subreddit
graph change slowly. Cursor pages are cached per cursor.
"""
from datetime import timedelta

SUBREDDIT_CACHE = timedelta(minutes=30)       # details, rules, similar, batch
WIKI_CACHE = timedelta(hours=6)
LISTING_CACHE = timedelta(minutes=5)          # subreddit / user / feed listings
COMMENTS_CACHE = timedelta(minutes=5)         # comment trees, more, export, sticky
POST_CACHE = timedelta(minutes=5)             # details, media, duplicates, batch, by-url
COMMENT_CACHE = timedelta(minutes=10)
USER_CACHE = timedelta(minutes=30)            # details, trophies, moderated, batch
SEARCH_CACHE = timedelta(minutes=10)
AUTOCOMPLETE_CACHE = timedelta(hours=1)
DIRECTORY_CACHE = timedelta(hours=1)          # popular / new subreddits, leaderboard
INSIGHTS_CACHE = timedelta(hours=1)
