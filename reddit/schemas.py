"""Marshmallow request schemas for every /reddit/* route.

Generic fields live in the shared top-level schema_fields.py; this module
adds the Reddit resolvers and the per-route schemas. Every schema's load()
output is the kwargs dict its endpoint function takes.

ONE param per input (tripadvisor QueryOrIdField convention, never a sibling
`url`/`id` pair), auto-detected by refs.py:
  subreddit  a name, r/name, t5_ fullname or a reddit.com/r/... link
  post       a base-36 id, t3_ fullname, or any post / share / redd.it link
  user       a username, u/name, t2_ fullname or a reddit.com/user/... link
  comment    a base-36 id, t1_ fullname or a comment permalink
Lists page with `cursor` (the `next_cursor` of the previous page) and
`limit` (Reddit's cap is 100 per page).
"""
from marshmallow import ValidationError, missing, post_load, validate

from schema_fields import BaseSchema, ChoiceField, CommaListField, Flag, LimitField, PositiveInt, QueryField, \
    RefField, StrippedString, UrlField
from reddit import refs

# Public enums, as literals so the publishing tooling (derive_facts AST) reads
# them; shared.py imports them from here.
TIME_FILTERS = ["hour", "day", "week", "month", "year", "all"]
POST_SORTS = ["hot", "new", "top", "rising", "controversial"]
COMMENT_SORT_CHOICES = ["best", "top", "new", "controversial", "old", "qa"]
SEARCH_SORTS = ["relevance", "hot", "top", "new", "comments"]
WWW_SEARCH_SORTS = ["relevance", "new", "top"]
USER_SORTS = ["new", "hot", "top", "controversial"]
INSIGHT_SAMPLES = ["top", "new"]
GEO_FILTERS = ["GLOBAL", "US", "GB", "CA", "AU", "DE", "FR", "IN", "JP", "MX", "ES", "IT", "SE", "PL", "TR", "AR"]


class SubredditRefField(RefField):
    resolver = staticmethod(refs.resolve_subreddit)


class PostRefField(RefField):
    resolver = staticmethod(refs.resolve_post)


class UserRefField(RefField):
    resolver = staticmethod(refs.resolve_user)


class CommentRefField(RefField):
    resolver = staticmethod(refs.resolve_comment)


class CursorField(StrippedString):
    """Opaque next_cursor from the previous page (absent = first page)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", missing if kwargs["required"] else None)
        kwargs.setdefault("validate", validate.Length(max=4000))
        super().__init__(**kwargs)


class _RefList(CommaListField):
    """Comma list of refs, each resolved by `resolver`."""

    def __init__(self, resolver, max_items=100, **kwargs):
        if kwargs.get("required"):
            kwargs["load_default"] = missing
        super().__init__(upper=False, max_items=max_items, **kwargs)
        self._resolver = resolver

    def _deserialize(self, value, attr, data, **kwargs):
        items = super()._deserialize(value, attr, data, **kwargs)
        if not items:
            raise ValidationError("Must not be empty.")
        out = []
        for item in items:
            try:
                out.append(self._resolver(item))
            except ValueError as e:
                raise ValidationError(str(e))
        return out


class EmptySchema(BaseSchema):
    pass


# ---- subreddits ----------------------------------------------------------------------------

_SUBREDDIT_DOC = {"description": "subreddit name, r/name, t5_ id or a reddit.com/r/... link"}


class SubredditSchema(BaseSchema):
    subreddit = SubredditRefField(metadata=_SUBREDDIT_DOC)


class SubredditDetailsSchema(SubredditSchema):
    include_rules = Flag(load_default=True)


class SubredditPostsSchema(SubredditSchema):
    sort = ChoiceField(POST_SORTS, load_default="hot")
    time = ChoiceField(TIME_FILTERS, load_default=None)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class SubredditPageSchema(SubredditSchema):
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class SubredditSearchSchema(SubredditSchema):
    query = QueryField(max_length=500)
    sort = ChoiceField(SEARCH_SORTS, load_default="relevance")
    time = ChoiceField(TIME_FILTERS, load_default="all")
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)
    include_nsfw = Flag(load_default=False)


class SubredditWikiSchema(SubredditSchema):
    page = StrippedString(required=False, load_default=None, validate=validate.Length(max=200),
                          metadata={"description": "wiki page name (default index); 'list' returns the page names"})


class SubredditStickySchema(SubredditSchema):
    position = PositiveInt(max_value=2, load_default=1)


class SubredditBatchSchema(BaseSchema):
    subreddits = _RefList(refs.resolve_subreddit, required=True,
                          metadata={"description": "comma-separated subreddit names / ids / links (max 100)"})


class SubredditFlairsSchema(SubredditSchema):
    sample_size = PositiveInt(max_value=100, load_default=100)


class SubredditInsightsSchema(SubredditSchema):
    sample = ChoiceField(INSIGHT_SAMPLES, load_default="top")
    time = ChoiceField(TIME_FILTERS, load_default="week")
    sample_size = PositiveInt(max_value=100, load_default=100)


class DirectorySchema(BaseSchema):
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class LeaderboardSchema(BaseSchema):
    topic = StrippedString(required=False, load_default=None, validate=validate.Length(max=60),
                           metadata={"description": "an explore topic slug (e.g. technology, games); omit for the overall leaderboard"})


# ---- posts -----------------------------------------------------------------------------------

_POST_DOC = {"description": "post id, t3_ fullname, or a reddit.com post / share / redd.it link"}


class PostSchema(BaseSchema):
    post = PostRefField(metadata=_POST_DOC)


class PostCommentsSchema(BaseSchema):
    post = PostRefField(required=False, load_default=None, metadata=_POST_DOC)
    sort = ChoiceField(COMMENT_SORT_CHOICES, load_default="best")
    cursor = CursorField()
    limit = LimitField(default=100, max_size=500)
    depth = PositiveInt(max_value=10, load_default=None)
    comment = StrippedString(required=False, load_default=None, validate=validate.Regexp(r"^(t1_)?[A-Za-z0-9]{1,12}$"),
                             metadata={"description": "focus the tree on one comment id"})
    context = PositiveInt(max_value=10, load_default=None)

    def load(self, data, *args, **kwargs):
        out = super().load(data, *args, **kwargs)
        if not out.get("post") and not out.get("cursor"):
            raise ValidationError({"post": ["Missing data for required field."]})
        if out.get("comment"):
            out["comment"] = out["comment"].split("_", 1)[-1].lower()
        return out


class CommentsMoreSchema(BaseSchema):
    cursor = CursorField(required=True)


class PostExportSchema(PostSchema):
    sort = ChoiceField(COMMENT_SORT_CHOICES, load_default="best")
    max_comments = PositiveInt(max_value=2000, load_default=500)


class PostPageSchema(PostSchema):
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class PostBatchSchema(BaseSchema):
    posts = _RefList(refs.resolve_post, required=True,
                     metadata={"description": "comma-separated post ids / fullnames / links (max 100)"})


class PostsByUrlSchema(BaseSchema):
    url = UrlField(required=True, load_default=missing, metadata={"description": "an external page URL"})
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class CommentSchema(BaseSchema):
    comment = CommentRefField(metadata={"description": "comment id, t1_ fullname or a comment permalink"})
    context = PositiveInt(max_value=10, load_default=None)


# ---- users ---------------------------------------------------------------------------------------

_USER_DOC = {"description": "username, u/name, t2_ id or a reddit.com/user/... link"}


class UserSchema(BaseSchema):
    user = UserRefField(metadata=_USER_DOC)


class UserDetailsSchema(UserSchema):
    include_trophies = Flag(load_default=True)


class UserHistorySchema(UserSchema):
    sort = ChoiceField(USER_SORTS, load_default="new")
    time = ChoiceField(TIME_FILTERS, load_default=None)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)


class UserBatchSchema(BaseSchema):
    users = _RefList(refs.resolve_user, required=True,
                     metadata={"description": "comma-separated usernames / t2_ ids / links (max 100)"})


class UserSampleSchema(UserSchema):
    sample_size = PositiveInt(max_value=100, load_default=100)


# ---- search / feeds --------------------------------------------------------------------------------

class SearchPostsSchema(BaseSchema):
    query = QueryField(max_length=500)
    sort = ChoiceField(SEARCH_SORTS, load_default="relevance")
    time = ChoiceField(TIME_FILTERS, load_default="all")
    subreddit = SubredditRefField(required=False, load_default=None, metadata=_SUBREDDIT_DOC)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)
    include_nsfw = Flag(load_default=False)


class SearchListSchema(BaseSchema):
    query = QueryField(max_length=200)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)
    include_nsfw = Flag(load_default=False)


class SearchWwwSchema(BaseSchema):
    query = QueryField(max_length=500)
    sort = ChoiceField(WWW_SEARCH_SORTS, load_default="relevance")
    time = ChoiceField(TIME_FILTERS, load_default="all")
    cursor = CursorField()


class AutocompleteSchema(BaseSchema):
    query = QueryField(max_length=50)
    limit = LimitField(default=10, max_size=10)
    include_nsfw = Flag(load_default=False)
    include_profiles = Flag(load_default=True)


class PopularSchema(BaseSchema):
    country = ChoiceField(GEO_FILTERS, load_default="GLOBAL")
    sort = ChoiceField(POST_SORTS, load_default="hot")
    time = ChoiceField(TIME_FILTERS, load_default=None)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)

    @post_load
    def _upper_country(self, data, **kwargs):
        data["country"] = (data.get("country") or "GLOBAL").upper()   # Reddit's geo_filter codes are upper-case
        return data


class FeedSchema(BaseSchema):
    sort = ChoiceField(POST_SORTS, load_default="hot")
    time = ChoiceField(TIME_FILTERS, load_default=None)
    cursor = CursorField()
    limit = LimitField(default=25, max_size=100)
