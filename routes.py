"""The 40 Reddit endpoints. Every path is served with and without the
`/reddit` prefix, so code generated against the hosted API on RapidAPI
(paths like /posts/comments) runs unchanged against this server.

Params are validated by the same marshmallow schemas the hosted API uses
(reddit/schemas.py): `subreddit`, `post`, `user` and `comment` each accept
a bare name / id or a pasted reddit.com link."""
import json

from bottle import request, response, route

from reddit import discovery, insights, posts, schemas, subreddits, users
from reddit.fetch import RedditBadRequest, RedditNotFound
from schema_fields import load_query


def json_response(data, status=200):
    response.status = status
    response.content_type = "application/json"
    return json.dumps(data, ensure_ascii=False)


def query_dict():
    """The query as unicode strings (bottle 0.12's .get() hands back latin-1
    decoded bytes, so a UTF-8 "Amélie" would arrive as "AmÃ©lie")."""
    return {key: request.query.getunicode(key) for key in request.query.keys()}


def call(label, schema, fn):
    """Validate, run, map errors: bad params -> 400, missing entity -> 404,
    transport/blocks -> 500."""
    data, error = load_query(schema, query_dict())
    if error:
        return json_response(error, 400)
    try:
        return json_response(fn(**data))
    except ValueError as e:                # bad value
        return json_response({"error": str(e)}, 400)
    except RedditBadRequest as e:          # upstream rejected the request (e.g. a bad cursor)
        return json_response({"error": f"reddit rejected the request: {e}"}, 400)
    except RedditNotFound as e:            # missing, private, banned, or login-only
        return json_response({"error": str(e) or "not found"}, 404)
    except Exception as e:                 # retries exhausted / blocked
        return json_response({"error": f"reddit {label} failed: {e}"}, 500)


def mount(path, schema, fn):
    """Serve fn at /path and /reddit/path."""
    def handler():
        return call(path.strip("/"), schema, fn)
    handler.__name__ = "reddit_" + path.strip("/").replace("/", "_").replace("-", "_")
    route(path, method="GET")(handler)
    route("/reddit" + path, method="GET")(handler)


S = schemas
ENDPOINTS = [
    # posts & comments
    ("/posts/comments", S.PostCommentsSchema, posts.comments),
    ("/posts/details", S.PostSchema, posts.details),
    ("/posts/comments/export", S.PostExportSchema, posts.export),
    ("/posts/media", S.PostSchema, posts.media),
    ("/posts/duplicates", S.PostPageSchema, posts.duplicates),
    ("/posts/batch", S.PostBatchSchema, posts.batch),
    ("/posts/by-url", S.PostsByUrlSchema, posts.by_url),
    ("/comments/details", S.CommentSchema, posts.comment_details),
    # search
    ("/search/autocomplete", S.AutocompleteSchema, discovery.autocomplete),
    ("/search/posts", S.SearchPostsSchema, discovery.search_posts),
    ("/search/comments", S.SearchWwwSchema, discovery.search_comments),
    ("/search/media", S.SearchWwwSchema, discovery.search_media),
    ("/search/subreddits", S.SearchListSchema, discovery.search_subreddits),
    ("/search/users", S.SearchListSchema, discovery.search_users),
    # subreddits
    ("/subreddits/posts", S.SubredditPostsSchema, subreddits.posts),
    ("/subreddits/details", S.SubredditDetailsSchema, subreddits.details),
    ("/subreddits/search", S.SubredditSearchSchema, subreddits.search),
    ("/subreddits/comments", S.SubredditPageSchema, subreddits.comments),
    ("/subreddits/insights", S.SubredditInsightsSchema, insights.subreddit_insights),
    ("/subreddits/rules", S.SubredditSchema, subreddits.rules),
    ("/subreddits/wiki", S.SubredditWikiSchema, subreddits.wiki),
    ("/subreddits/similar", S.SubredditSchema, subreddits.similar),
    ("/subreddits/sticky", S.SubredditStickySchema, subreddits.sticky),
    ("/subreddits/flairs", S.SubredditFlairsSchema, subreddits.flairs),
    ("/subreddits/batch", S.SubredditBatchSchema, subreddits.batch),
    ("/subreddits/leaderboard", S.LeaderboardSchema, subreddits.leaderboard),
    ("/subreddits/popular", S.DirectorySchema, subreddits.popular),
    ("/subreddits/new", S.DirectorySchema, subreddits.new),
    # users
    ("/users/details", S.UserDetailsSchema, users.details),
    ("/users/posts", S.UserHistorySchema, users.posts),
    ("/users/comments", S.UserHistorySchema, users.comments),
    ("/users/overview", S.UserHistorySchema, users.overview),
    ("/users/insights", S.UserSampleSchema, insights.user_insights),
    ("/users/active-subreddits", S.UserSampleSchema, users.active_subreddits),
    ("/users/trophies", S.UserSchema, users.trophies),
    ("/users/moderated", S.UserSchema, users.moderated),
    ("/users/batch", S.UserBatchSchema, users.batch),
    # feeds
    ("/feeds/popular", S.PopularSchema, discovery.popular),
    ("/feeds/all", S.FeedSchema, discovery.all_posts),
    ("/feeds/best", S.DirectorySchema, discovery.best),
]

for _path, _schema, _fn in ENDPOINTS:
    mount(_path, _schema, _fn)


@route("/", method="GET")
@route("/health", method="GET")
def health():
    return json_response({"status": "ok", "endpoints": [p for p, _, _ in ENDPOINTS]})
