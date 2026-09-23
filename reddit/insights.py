"""Computed analytics — no extra upstream surface, just arithmetic over the
listings the other endpoints serve:

  subreddit_insights   activity and engagement of a subreddit from a sample
                       of its posts (top of the window, or newest): posts per
                       day, score / comment medians and means, upvote ratio,
                       best posting hours (UTC) and weekdays, post-type mix,
                       top flairs, domains and authors
  user_insights        a user's activity from their newest overview items:
                       split by kind, by subreddit, by hour and weekday,
                       average scores, posting cadence
"""
from collections import Counter
from datetime import datetime, timezone
from statistics import mean, median

from . import parsers as P
from . import shared as S

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _span_hours(times):
    if len(times) < 2:
        return None
    return round((max(times) - min(times)).total_seconds() / 3600, 1)


def _by_hour(times):
    counter = Counter(t.hour for t in times)
    return [{"hour_utc": hour, "count": counter.get(hour, 0)} for hour in range(24)]


def _by_weekday(times):
    counter = Counter(t.weekday() for t in times)
    return [{"weekday": WEEKDAYS[i], "count": counter.get(i, 0)} for i in range(7)]


def _best(values, key, scores, top=5):
    """Hours / weekdays ranked by mean score of the items posted then."""
    buckets = {}
    for value, score in zip(values, scores):
        buckets.setdefault(value, []).append(score)
    ranked = sorted(buckets.items(), key=lambda kv: (-mean(kv[1]), -len(kv[1])))
    return [{key: v, "mean_score": round(mean(s), 1), "count": len(s)} for v, s in ranked[:top]]


def _top(counter, key, limit=10):
    return [{key: value, "count": count} for value, count in counter.most_common(limit)]


def subreddit_insights(subreddit, sample="top", time="week", sample_size=100):
    name = S.subreddit_name(subreddit)
    size = min(sample_size or 100, 100)
    if sample == "top":
        listing = S.listing(f"/r/{name}/top", {"limit": size, "t": time}, P.post, "posts")
    else:
        listing = S.listing(f"/r/{name}/new", {"limit": size}, P.post, "posts")
    posts = listing["posts"]
    times = [t for t in (_ts(p.get("created_at")) for p in posts) if t]
    scores = [p["stats"].get("score") or 0 for p in posts]
    comment_counts = [p["stats"].get("comment_count") or 0 for p in posts]
    ratios = [p["stats"]["upvote_ratio"] for p in posts if p["stats"].get("upvote_ratio") is not None]
    span = _span_hours(times)
    per_day = round(len(posts) / (span / 24), 2) if span and span > 0 else None
    flair_counter = Counter((p.get("flair") or {}).get("text") for p in posts if (p.get("flair") or {}).get("text"))
    domain_counter = Counter(p.get("domain") for p in posts if p.get("domain") and not p.get("flags", {}).get("is_self"))
    author_counter = Counter((p.get("author") or {}).get("username") for p in posts if p.get("author"))
    type_counter = Counter(p.get("type") for p in posts)
    hours = [t.hour for t in times]
    weekdays = [WEEKDAYS[t.weekday()] for t in times]
    paired_scores = [p["stats"].get("score") or 0 for p, t in zip(posts, (_ts(p.get("created_at")) for p in posts)) if t]
    return {
        "subreddit": name,
        "sample": {"kind": sample, "time": time if sample == "top" else None, "size": len(posts),
                   "span_hours": span, "from": min(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
                   "to": max(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None},
        "activity": {
            "posts_per_day": per_day,
            "comments_per_day": round(sum(comment_counts) / (span / 24), 1) if span and span > 0 else None,
            "by_hour_utc": _by_hour(times),
            "by_weekday": _by_weekday(times),
        },
        "engagement": {
            "score": {"mean": round(mean(scores), 1) if scores else None, "median": median(scores) if scores else None,
                      "max": max(scores) if scores else None},
            "comments": {"mean": round(mean(comment_counts), 1) if comment_counts else None,
                         "median": median(comment_counts) if comment_counts else None,
                         "max": max(comment_counts) if comment_counts else None},
            "upvote_ratio_mean": round(mean(ratios), 3) if ratios else None,
            "nsfw_share": round(sum(1 for p in posts if p["flags"].get("is_nsfw")) / len(posts), 3) if posts else None,
            "self_post_share": round(sum(1 for p in posts if p["flags"].get("is_self")) / len(posts), 3) if posts else None,
        },
        "best_posting_hours_utc": _best(hours, "hour_utc", paired_scores),
        "best_weekdays": _best(weekdays, "weekday", paired_scores, top=3),
        "post_types": _top(type_counter, "type"),
        "top_flairs": _top(flair_counter, "flair"),
        "top_domains": _top(domain_counter, "domain"),
        "top_authors": _top(author_counter, "username"),
    }


def user_insights(user, sample_size=100):
    name = S.username(user)
    listing = S.listing(f"/user/{name}/overview", {"limit": min(sample_size or 100, 100), "sort": "new"}, P.thing, "items")
    items = listing["items"]
    posts = [i for i in items if i["kind"] == "post"]
    comments = [i for i in items if i["kind"] == "comment"]
    times = [t for t in (_ts(i.get("created_at")) for i in items) if t]
    span = _span_hours(times)
    sub_counter = Counter((i.get("subreddit") or {}).get("name") for i in items if i.get("subreddit"))
    post_scores = [p["stats"].get("score") or 0 for p in posts]
    comment_scores = [c["stats"].get("score") or 0 for c in comments]
    return {
        "user": name,
        "sample": {"size": len(items), "span_hours": span,
                   "from": min(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
                   "to": max(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None},
        "activity": {
            "post_count": len(posts),
            "comment_count": len(comments),
            "items_per_day": round(len(items) / (span / 24), 2) if span and span > 0 else None,
            "by_hour_utc": _by_hour(times),
            "by_weekday": _by_weekday(times),
        },
        "engagement": {
            "post_score_mean": round(mean(post_scores), 1) if post_scores else None,
            "comment_score_mean": round(mean(comment_scores), 1) if comment_scores else None,
            "best_post": max(posts, key=lambda p: p["stats"].get("score") or 0, default=None),
            "best_comment": max(comments, key=lambda c: c["stats"].get("score") or 0, default=None),
        },
        "top_subreddits": _top(sub_counter, "subreddit", 15),
    }
