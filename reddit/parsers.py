"""Reddit normalizers: the legacy JSON API's "things" (t1 comment, t2 user,
t3 post, t5 subreddit, trophies, wiki pages, rules) -> one clean snake_case
shape per entity.

Output conventions (shared with the other scrapers here): `link` for
canonical reddit.com URLs, `*_link` for other URLs, `*_count` for counters,
`is_*` / `has_*` / `allows_*` for booleans, ISO-8601 UTC timestamps (the
upstream's epoch seconds), numbers as numbers, null for missing, [] for
empty lists. Ids are Reddit's base-36 strings WITHOUT the kind prefix
(`1wn1d94`, not `t3_1wn1d94`); pagination cursors keep the prefixed form
the API wants back.

Entity shapes:
  post        {id, title, link, type, text, text_html, external_link, domain,
               created_at, edited_at, subreddit{...}, author{...}, flair{...},
               stats{score, upvote_ratio, comment_count, crosspost_count},
               flags{is_*}, media{...}, poll{...}, crosspost_parent, thumbnail,
               distinguished, suggested_sort, removed_by, category,
               content_categories, discussion_type, treatment_tags}
               `type`: text | image | gallery | video | gif | embed | link |
               poll | crosspost
  comment     {id, link, post_id, parent_comment_id, depth, text, text_html,
               created_at, edited_at, subreddit{name, id}, author{...},
               stats{score, is_score_hidden, controversiality}, flags{is_*},
               distinguished, media[...], post{...} (when the listing carries
               the parent post: user / subreddit comment streams),
               replies[...], more_replies{count, cursor}}
  subreddit   {id, name, link, title, description, description_html,
               sidebar, sidebar_html, subscriber_count, active_user_count,
               created_at, type, language, icon, banner{...}, colors{...},
               header{...}, flags{is_*, allows_*}, submission{...},
               suggested_comment_sort, advertiser_category,
               comment_score_hide_minutes, link_flair_position,
               user_flair_position}
  user        {id, username, link, created_at, avatar, snoovatar,
               karma{total, post, comment, awardee, awarder}, flags{is_*},
               profile{title, description, banner, icon, follower_count,
               is_nsfw, previous_names}}
  trophy      {id, name, description, granted_at, icon, link, award_id}

Deliberately dropped as noise:
  viewer state    saved, clicked, hidden, visited, likes, user_reports,
                  mod_reports, num_reports, report_reasons, can_gild,
                  can_mod_post, send_replies, no_follow, author_is_blocked,
                  is_blocked, is_friend, has_subscribed, notification_level,
                  user_is_* / user_has_favorited / user_can_flair_in_sr /
                  user_sr_* on subreddits, is_default_* on profiles —
                  the anonymous viewer's own state, always null/false
  mod-only        approved_at_utc, approved_by, banned_at_utc, banned_by,
                  mod_note, mod_reason_by, mod_reason_title, removal_reason,
                  removed_by (the category is kept as `removed_by`)
  awards          all_awardings, awarders, gildings, gilded, top_awarded_type,
                  total_awards_received, associated_award — Reddit retired
                  awards in 2023; every value is [] / 0 / null now
  duplicates      name (= kind + id), created (= created_utc in local time),
                  ups (= score), downs (always 0), subreddit_name_prefixed,
                  display_name_prefixed, sr_display_name_prefixed, media_embed /
                  secure_media_embed (the oembed html again), media (=
                  secure_media), *_html of a flair richtext, thumbnail_width /
                  thumbnail_height when there is no thumbnail, icon_size /
                  banner_size / header_size (the image's own dimensions)
  presentation    pwls / wls (ad whitelist status), websocket_url,
                  hide_ads, emojis_custom_size, prediction_* / allow_prediction*
                  (a retired feature), collapse_deleted_comments,
                  comment_contribution_settings, should_show_media_in_comments_setting,
                  show_media_preview, public_traffic, is_enrolled_in_new_modmail,
                  disable_contributor_requests, original_content_tag_enabled,
                  pref_show_snoovatar, snoovatar_size, icon_color, default_set,
                  is_created_from_ads_ui (kept only as a flag), location_*
                  (always null), collapsed_because_crowd_control (the
                  collapsed_reason_code says why), unrepliable_reason,
                  comment_type, may_revise / reason on wiki pages
"""
import base64
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from . import refs

_MISSING_AUTHORS = {"[deleted]", "[removed]"}


# ---- scalars ---------------------------------------------------------------------------

def clean(value):
    """Whitespace-trimmed string, or None for empty / non-strings."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def to_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    try:
        return int(text)
    except ValueError:
        return None


def to_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value):
    """True/False for real booleans (and 0/1), None when absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def iso_utc(epoch):
    """Epoch seconds (or milliseconds) -> 2026-09-17T14:27:44Z."""
    seconds = to_float(epoch)
    if not seconds:
        return None
    if seconds > 1e11:          # milliseconds
        seconds = seconds / 1000
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def edited_at(value):
    """`edited` is False, or the epoch of the last edit."""
    if value is None or value is False:
        return None
    return iso_utc(value)


def image_url(value):
    """A redditmedia / redd.it image URL as given (they are signed; the
    query must stay), or None for the empty / placeholder values Reddit
    uses ("", "self", "default", "nsfw", "spoiler", "image")."""
    text = clean(value)
    if not text or not text.startswith("http"):
        return None
    return text


def strip_query(url):
    """A v.redd.it rendition URL without its ?source=fallback marker."""
    text = clean(url)
    if not text:
        return None
    parts = urlsplit(text)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _data(thing):
    """A "thing" {kind, data} -> data; a bare data dict passes through."""
    if not isinstance(thing, dict):
        return {}
    if "kind" in thing and isinstance(thing.get("data"), dict):
        return thing["data"]
    return thing


# ---- cursors -------------------------------------------------------------------------------

def encode_cursor(payload):
    """A small dict -> an opaque URL-safe token (used for the `more`
    comment stubs, whose state is a list of ids rather than one fullname)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token):
    """Inverse of encode_cursor; ValueError on garbage."""
    text = str(token or "").strip()
    if not text:
        raise ValueError("empty cursor")
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        raise ValueError("invalid cursor: pass the cursor value from the previous response unchanged")
    if not isinstance(payload, dict):
        raise ValueError("invalid cursor: pass the cursor value from the previous response unchanged")
    return payload


# ---- shared pieces ---------------------------------------------------------------------------

def flair(data, prefix):
    """The `<prefix>_flair_*` fields of a post (link_) or author (author_)
    -> {text, type, css_class, template_id, background_color, text_color,
    emojis[{name, image}]} or None when there is no flair."""
    text = clean(data.get(f"{prefix}_flair_text"))
    richtext = data.get(f"{prefix}_flair_richtext") or []
    template = clean(data.get(f"{prefix}_flair_template_id"))
    css = clean(data.get(f"{prefix}_flair_css_class"))
    if not (text or richtext or template or css):
        return None
    emojis = []
    pieces = []
    for part in richtext:
        if not isinstance(part, dict):
            continue
        if part.get("e") == "emoji":
            emojis.append({"name": clean(part.get("a")), "image": image_url(part.get("u"))})
        elif part.get("e") == "text" and clean(part.get("t")):
            pieces.append(clean(part.get("t")))
    return {
        "text": text or (" ".join(pieces) or None),
        "type": clean(data.get(f"{prefix}_flair_type")),
        "css_class": css,
        "template_id": template,
        "background_color": clean(data.get(f"{prefix}_flair_background_color")),
        "text_color": clean(data.get(f"{prefix}_flair_text_color")),
        "emojis": emojis,
    }


def author(data):
    """The author block of a post or comment; None for deleted authors."""
    name = clean(data.get("author"))
    if not name or name in _MISSING_AUTHORS:
        return None
    return {
        "username": name,
        "id": refs.strip_kind(clean(data.get("author_fullname"))),
        "link": refs.user_link(name),
        "flair": flair(data, "author"),
        "is_premium": to_bool(data.get("author_premium")),
        "has_patreon_flair": to_bool(data.get("author_patreon_flair")),
    }


def subreddit_ref(data):
    name = clean(data.get("subreddit"))
    if not name:
        return None
    out = {
        "name": name,
        "id": refs.strip_kind(clean(data.get("subreddit_id"))),
        "link": refs.subreddit_link(name),
    }
    if "subreddit_subscribers" in data:
        out["subscriber_count"] = to_int(data.get("subreddit_subscribers"))
    if "subreddit_type" in data:
        out["type"] = clean(data.get("subreddit_type"))
    return out


def _dimensions(source):
    if not isinstance(source, dict):
        return None
    return {"link": image_url(source.get("url") or source.get("u")),
            "width": to_int(source.get("width") or source.get("x")),
            "height": to_int(source.get("height") or source.get("y"))}


# ---- media -----------------------------------------------------------------------------------

def reddit_video(video, poster=None):
    """A `reddit_video` block -> the download-ready renditions.

    v.redd.it file names depend on when the video was encoded (probed
    2026-09-23): newer uploads serve CMAF_<height>.mp4 + CMAF_AUDIO_128.mp4,
    older ones DASH_<height>.mp4 + DASH_audio.mp4 (and the other family is
    403). The fallback_url names the best video-only file of the right
    family; the audio name is only certain for CMAF, so older videos leave
    `audio_link` null here and /posts/media reads the exact files from the
    DASH manifest (parse_dash_manifest). Manifests need no signature."""
    if not isinstance(video, dict):
        return None
    fallback = strip_query(video.get("fallback_url"))
    base = fallback.rsplit("/", 1)[0] if fallback and "/" in fallback else None
    filename = fallback.rsplit("/", 1)[-1] if fallback else ""
    has_audio = to_bool(video.get("has_audio"))
    audio = f"{base}/CMAF_AUDIO_128.mp4" if base and has_audio and filename.startswith("CMAF_") else None
    return {
        "link": fallback,
        "audio_link": audio,
        "dash_manifest": f"{base}/DASHPlaylist.mpd" if base else clean(video.get("dash_url")),
        "hls_manifest": f"{base}/HLSPlaylist.m3u8" if base else clean(video.get("hls_url")),
        "scrubber_link": strip_query(video.get("scrubber_media_url")),
        "poster": poster,
        "duration": to_int(video.get("duration")),
        "width": to_int(video.get("width")),
        "height": to_int(video.get("height")),
        "bitrate_kbps": to_int(video.get("bitrate_kbps")),
        "has_audio": has_audio,
        "is_gif": to_bool(video.get("is_gif")),
        "transcoding_status": clean(video.get("transcoding_status")),
    }


def parse_dash_manifest(xml_text, base):
    """A v.redd.it DASHPlaylist.mpd -> {"videos": [{height, width,
    bitrate_kbps, codec, link}] (smallest first), "audio": [{bitrate_kbps,
    codec, link}] (best first)}. None when the XML does not parse."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text or "")
    except ET.ParseError:
        return None

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    videos, audio = [], []
    for aset in root.iter():
        if local(aset.tag) != "AdaptationSet":
            continue
        kind = (aset.get("contentType") or aset.get("mimeType") or "").split("/")[0]
        for rep in aset:
            if local(rep.tag) != "Representation":
                continue
            base_url = next((c.text for c in rep if local(c.tag) == "BaseURL" and c.text), None)
            if not base_url:
                continue
            rep_kind = kind or (rep.get("mimeType") or "").split("/")[0]
            entry = {"bitrate_kbps": round(to_int(rep.get("bandwidth")) / 1000) if to_int(rep.get("bandwidth")) else None,
                     "codec": clean(rep.get("codecs")), "link": f"{base}/{base_url.strip()}"}
            if rep_kind == "audio":
                audio.append(entry)
            else:
                videos.append({"height": to_int(rep.get("height")), "width": to_int(rep.get("width")), **entry})
    videos.sort(key=lambda v: (v["height"] or 0))
    audio.sort(key=lambda a: -(a["bitrate_kbps"] or 0))
    return {"videos": videos, "audio": audio}


def _preview_image(data):
    """preview.images[0] -> {source{link,width,height}, gif_link, mp4_link}."""
    preview = data.get("preview") or {}
    images = preview.get("images") or []
    if not images or not isinstance(images[0], dict):
        return None
    first = images[0]
    variants = first.get("variants") or {}
    return {
        "source": _dimensions(first.get("source")),
        "gif_link": image_url(((variants.get("gif") or {}).get("source") or {}).get("url")),
        "mp4_link": image_url(((variants.get("mp4") or {}).get("source") or {}).get("url")),
    }


def _gallery_images(data):
    """gallery_data (order + captions) joined with media_metadata (the
    files) -> images in gallery order; originals are i.redd.it/<id>.<ext>."""
    meta = data.get("media_metadata") or {}
    order = ((data.get("gallery_data") or {}).get("items")) or []
    if not order:
        order = [{"media_id": key} for key in meta.keys()]
    out = []
    for item in order:
        if not isinstance(item, dict):
            continue
        media_id = clean(item.get("media_id"))
        info = meta.get(media_id) or {}
        out.append(_metadata_image(media_id, info, caption=clean(item.get("caption")),
                                   outbound=clean(item.get("outbound_url"))))
    return out


def _metadata_image(media_id, info, caption=None, outbound=None):
    kind = clean(info.get("e"))
    mime = clean(info.get("m"))
    source = info.get("s") or {}
    ext = mime.split("/")[-1] if mime else None
    if ext == "jpeg":
        ext = "jpg"
    original = f"https://i.redd.it/{media_id}.{ext}" if media_id and ext and kind == "Image" else None
    return {
        "id": media_id,
        "type": {"Image": "image", "AnimatedImage": "animated_image", "RedditVideo": "video"}.get(kind, kind.lower() if kind else None),
        "link": original or image_url(source.get("u")) or image_url(source.get("gif")),
        "width": to_int(source.get("x")),
        "height": to_int(source.get("y")),
        "mime": mime,
        "gif_link": image_url(source.get("gif")),
        "mp4_link": image_url(source.get("mp4")),
        "caption": caption,
        "outbound_link": outbound,
        "status": clean(info.get("status")),
    }


def comment_media(data):
    """Images / gifs embedded in a comment (media_metadata) -> [images]."""
    meta = data.get("media_metadata")
    if not isinstance(meta, dict) or not meta:
        return []
    return [_metadata_image(key, info or {}) for key, info in meta.items() if isinstance(info, dict)]


def oembed(data):
    media = data.get("secure_media") or data.get("media") or {}
    embed = media.get("oembed") if isinstance(media, dict) else None
    if not isinstance(embed, dict):
        return None
    return {
        "type": clean(embed.get("type")) or clean(media.get("type")),
        "provider": clean(embed.get("provider_name")) or clean(media.get("type")),
        "provider_link": clean(embed.get("provider_url")),
        "title": clean(embed.get("title")),
        "author": clean(embed.get("author_name")),
        "author_link": clean(embed.get("author_url")),
        "html": clean(embed.get("html")),
        "thumbnail": {"link": image_url(embed.get("thumbnail_url")),
                      "width": to_int(embed.get("thumbnail_width")),
                      "height": to_int(embed.get("thumbnail_height"))} if embed.get("thumbnail_url") else None,
        "width": to_int(embed.get("width")),
        "height": to_int(embed.get("height")),
    }


def post_type_and_media(data):
    """-> (type, media dict or None). See the module docstring for types."""
    hint = clean(data.get("post_hint"))
    url = clean(data.get("url_overridden_by_dest")) or clean(data.get("url"))
    preview = _preview_image(data)
    poster = (preview or {}).get("source")
    media_block = data.get("secure_media") or data.get("media") or {}
    reddit_video_block = media_block.get("reddit_video") if isinstance(media_block, dict) else None
    video_preview = (data.get("preview") or {}).get("reddit_video_preview")

    if data.get("crosspost_parent_list"):
        parent = data["crosspost_parent_list"][0] if isinstance(data["crosspost_parent_list"], list) else None
        _, media = post_type_and_media(parent) if isinstance(parent, dict) else (None, None)
        return "crosspost", media
    if data.get("poll_data"):
        return "poll", None
    if to_bool(data.get("is_gallery")):
        return "gallery", {"type": "gallery", "images": _gallery_images(data), "video": None, "embed": None,
                           "preview": poster}
    if to_bool(data.get("is_video")) or reddit_video_block:
        return "video", {"type": "video", "images": [], "video": reddit_video(reddit_video_block, poster),
                         "embed": None, "preview": poster}
    if hint == "image" or (url and url.split("?")[0].lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))):
        is_gif = bool((preview or {}).get("mp4_link")) or (url or "").split("?")[0].lower().endswith(".gif")
        image = {"id": None, "type": "animated_image" if is_gif else "image", "link": url,
                 "width": (poster or {}).get("width"), "height": (poster or {}).get("height"),
                 "mime": None, "gif_link": (preview or {}).get("gif_link"),
                 "mp4_link": (preview or {}).get("mp4_link"), "caption": None, "outbound_link": None,
                 "status": None}
        return ("gif" if is_gif else "image"), {"type": "gif" if is_gif else "image", "images": [image],
                                                 "video": None, "embed": None, "preview": poster}
    embed = oembed(data)
    if hint == "rich:video" or embed:
        return "embed", {"type": "embed", "images": [], "video": reddit_video(video_preview, poster) if video_preview else None,
                         "embed": embed, "preview": poster}
    if to_bool(data.get("is_self")):
        return "text", None
    if video_preview:
        return "gif", {"type": "gif", "images": [], "video": reddit_video(video_preview, poster), "embed": None,
                       "preview": poster}
    return "link", ({"type": "link", "images": [], "video": None, "embed": None, "preview": poster}
                    if poster else None)


def poll(data):
    block = data.get("poll_data")
    if not isinstance(block, dict):
        return None
    options = []
    for option in block.get("options") or []:
        if isinstance(option, dict):
            options.append({"id": clean(option.get("id")), "text": clean(option.get("text")),
                            "vote_count": to_int(option.get("vote_count"))})
    return {
        "options": options,
        "total_vote_count": to_int(block.get("total_vote_count")),
        "voting_ends_at": iso_utc(block.get("voting_end_timestamp")),
        "is_prediction": bool(block.get("is_prediction")),
    }


def thumbnail(data):
    link = image_url(data.get("thumbnail"))
    if not link:
        return None
    return {"link": link, "width": to_int(data.get("thumbnail_width")), "height": to_int(data.get("thumbnail_height"))}


# ---- post -----------------------------------------------------------------------------------

def post(thing):
    data = _data(thing)
    if not data or not data.get("id"):
        return None
    kind, media = post_type_and_media(data)
    is_self = to_bool(data.get("is_self"))
    url = clean(data.get("url_overridden_by_dest")) or clean(data.get("url"))
    permalink = clean(data.get("permalink"))
    external = None if is_self or not url or url.startswith("https://www.reddit.com" + (permalink or "\0")) else url
    crosspost_parent = None
    parents = data.get("crosspost_parent_list")
    if isinstance(parents, list) and parents and isinstance(parents[0], dict):
        crosspost_parent = post(parents[0])
    removed = clean(data.get("removed_by_category"))
    return {
        "id": clean(data.get("id")),
        "title": clean(data.get("title")),
        "link": refs.post_link(permalink, data.get("id")),
        "type": kind,
        "text": clean(data.get("selftext")),
        "text_html": clean(data.get("selftext_html")),
        "external_link": external,
        "domain": clean(data.get("domain")),
        "created_at": iso_utc(data.get("created_utc")),
        "edited_at": edited_at(data.get("edited")),
        "subreddit": subreddit_ref(data),
        "author": author(data),
        "flair": flair(data, "link"),
        "stats": {
            "score": to_int(data.get("score")),
            "upvote_ratio": to_float(data.get("upvote_ratio")),
            "comment_count": to_int(data.get("num_comments")),
            "crosspost_count": to_int(data.get("num_crossposts")),
            "view_count": to_int(data.get("view_count")),
        },
        "flags": {
            "is_self": is_self,
            "is_nsfw": to_bool(data.get("over_18")),
            "is_spoiler": to_bool(data.get("spoiler")),
            "is_stickied": to_bool(data.get("stickied")),
            "is_pinned": to_bool(data.get("pinned")),
            "is_locked": to_bool(data.get("locked")),
            "is_archived": to_bool(data.get("archived")),
            "is_edited": bool(data.get("edited")),
            "is_original_content": to_bool(data.get("is_original_content")),
            "is_contest_mode": to_bool(data.get("contest_mode")),
            "is_crosspost": crosspost_parent is not None or bool(data.get("crosspost_parent")),
            "is_crosspostable": to_bool(data.get("is_crosspostable")),
            "is_meta": to_bool(data.get("is_meta")),
            "is_media_only": to_bool(data.get("media_only")),
            "is_reddit_media": to_bool(data.get("is_reddit_media_domain")),
            "is_robot_indexable": to_bool(data.get("is_robot_indexable")),
            "is_score_hidden": to_bool(data.get("hide_score")),
            "is_quarantined": to_bool(data.get("quarantine")),
            "is_removed": removed is not None,
            "is_created_from_ads_ui": to_bool(data.get("is_created_from_ads_ui")),
            "allows_live_comments": to_bool(data.get("allow_live_comments")),
        },
        "media": media,
        "poll": poll(data),
        "crosspost_parent": crosspost_parent,
        "crosspost_parent_id": refs.strip_kind(clean(data.get("crosspost_parent"))),
        "thumbnail": thumbnail(data),
        "distinguished": clean(data.get("distinguished")),
        "suggested_sort": clean(data.get("suggested_sort")),
        "removed_by": removed,
        "category": clean(data.get("category")),
        "content_categories": [clean(c) for c in (data.get("content_categories") or []) if clean(c)],
        "discussion_type": clean(data.get("discussion_type")),
        "treatment_tags": [clean(t) for t in (data.get("treatment_tags") or []) if clean(t)],
    }


def post_ref(data):
    """The parent-post block a comment carries in user / subreddit comment
    streams (link_* fields)."""
    data = _data(data)
    post_id = refs.strip_kind(clean(data.get("link_id")))
    if not post_id and not data.get("link_title"):
        return None
    permalink = clean(data.get("link_permalink"))
    author_name = clean(data.get("link_author"))
    return {
        "id": post_id,
        "title": clean(data.get("link_title")),
        "link": permalink or refs.post_link(None, post_id),
        "external_link": clean(data.get("link_url")) if clean(data.get("link_url")) and not str(data.get("link_url")).startswith("https://www.reddit.com/r/") else None,
        "author": {"username": author_name, "link": refs.user_link(author_name)} if author_name and author_name not in _MISSING_AUTHORS else None,
        "comment_count": to_int(data.get("num_comments")),
        "is_nsfw": to_bool(data.get("over_18")),
    }


# ---- comment ----------------------------------------------------------------------------------

MORE_BATCH = 100        # ids per /api/morechildren call


def more_stub(data, post_id, state=None):
    """A `more` node -> {count, cursor} for /posts/comments/more.

    `state` is the listing's own {sort, limit, depth}. Three cursor forms:
      {"post", "sort", "ids": [...]}         a nested node (<= MORE_BATCH ids)
      {"post", "sort", "limit", "depth", "skip": N}
                                             the listing's tail when it holds
                                             more than MORE_BATCH ids: the
                                             consumer re-reads the listing and
                                             expands the next 100 from `skip`
      {"post", "sort", "focus": comment id}  a "continue this thread" stub
                                             (no children, id "_")
    """
    data = _data(data)
    state = state or {}
    sort = state.get("sort")
    children = [clean(c) for c in (data.get("children") or []) if clean(c)]
    parent = clean(data.get("parent_id")) or ""
    payload = {"post": post_id, "sort": sort}
    if children:
        if len(children) > MORE_BATCH and parent == f"t3_{post_id}" and state.get("limit"):
            payload.update({"limit": state.get("limit"), "depth": state.get("depth"), "skip": 0})
        else:
            payload["ids"] = children[:MORE_BATCH]
    elif parent.startswith("t1_"):
        payload["focus"] = refs.strip_kind(parent)
    else:
        return None
    return {"count": to_int(data.get("count")) or len(children), "cursor": encode_cursor(payload)}


def comment(thing, state=None, include_replies=True):
    data = _data(thing)
    if not data or not data.get("id"):
        return None
    post_id = refs.strip_kind(clean(data.get("link_id")))
    parent = clean(data.get("parent_id")) or ""
    permalink = clean(data.get("permalink"))
    replies = []
    more = None
    if include_replies:
        block = data.get("replies")
        if isinstance(block, dict):
            for child in ((block.get("data") or {}).get("children")) or []:
                if not isinstance(child, dict):
                    continue
                if child.get("kind") == "more":
                    more = more_stub(child, post_id, state)
                elif child.get("kind") == "t1":
                    parsed = comment(child, state, include_replies=True)
                    if parsed:
                        replies.append(parsed)
    out = {
        "id": clean(data.get("id")),
        "link": refs.comment_link(permalink, post_id, data.get("id")),
        "post_id": post_id,
        "parent_comment_id": refs.strip_kind(parent) if parent.startswith("t1_") else None,
        "depth": to_int(data.get("depth")),
        "text": clean(data.get("body")),
        "text_html": clean(data.get("body_html")),
        "created_at": iso_utc(data.get("created_utc")),
        "edited_at": edited_at(data.get("edited")),
        "subreddit": subreddit_ref(data),
        "author": author(data),
        "stats": {
            "score": to_int(data.get("score")),
            "is_score_hidden": to_bool(data.get("score_hidden")),
            "controversiality": to_int(data.get("controversiality")),
        },
        "flags": {
            "is_submitter": to_bool(data.get("is_submitter")),
            "is_stickied": to_bool(data.get("stickied")),
            "is_locked": to_bool(data.get("locked")),
            "is_archived": to_bool(data.get("archived")),
            "is_collapsed": to_bool(data.get("collapsed")),
            "is_edited": bool(data.get("edited")),
            "is_deleted": clean(data.get("author")) in _MISSING_AUTHORS or clean(data.get("body")) in _MISSING_AUTHORS,
        },
        "collapsed_reason": clean(data.get("collapsed_reason_code")) or clean(data.get("collapsed_reason")),
        "distinguished": clean(data.get("distinguished")),
        "media": comment_media(data),
    }
    parent_post = post_ref(data)
    if parent_post:
        out["post"] = parent_post
    if include_replies:
        out["replies"] = replies
        out["more_replies"] = more
    return out


def comment_tree(children, post_id, state=None):
    """A comment listing's children -> (comments[], more stub for the
    listing's own tail or None)."""
    comments = []
    more = None
    for child in children or []:
        if not isinstance(child, dict):
            continue
        if child.get("kind") == "more":
            more = more_stub(child, post_id, state)
        elif child.get("kind") == "t1":
            parsed = comment(child, state)
            if parsed:
                comments.append(parsed)
    return comments, more


def rebuild_tree(things, post_id, state=None):
    """/api/morechildren answers a FLAT list of t1 / more things (replies
    are ""); nest them by parent_id. Nodes whose parent is not in the batch
    become roots (their parents are already in the caller's tree)."""
    nodes = {}
    order = []
    stubs = []
    for thing in things or []:
        data = _data(thing)
        if thing.get("kind") == "more":
            stubs.append((clean(data.get("parent_id")) or "", more_stub(data, post_id, state)))
            continue
        if thing.get("kind") != "t1":
            continue
        parsed = comment(thing, state, include_replies=False)
        if not parsed:
            continue
        parsed["replies"] = []
        parsed["more_replies"] = None
        nodes["t1_" + parsed["id"]] = parsed
        order.append((clean(data.get("parent_id")) or "", parsed))
    roots = []
    for parent, node in order:
        holder = nodes.get(parent)
        if holder is not None:
            holder["replies"].append(node)
        else:
            roots.append(node)
    tail = None
    for parent, stub in stubs:
        if stub is None:
            continue
        holder = nodes.get(parent)
        if holder is not None:
            holder["more_replies"] = stub
        else:
            tail = stub
    return roots, tail


# ---- subreddit ----------------------------------------------------------------------------------

def subreddit(thing):
    data = _data(thing)
    if not data or not data.get("display_name"):
        return None
    name = clean(data.get("display_name"))
    return {
        "id": refs.strip_kind(clean(data.get("name"))) or clean(data.get("id")),
        "name": name,
        "link": refs.subreddit_link(name),
        "title": clean(data.get("title")),
        "description": clean(data.get("public_description")),
        "description_html": clean(data.get("public_description_html")),
        "sidebar": clean(data.get("description")),
        "sidebar_html": clean(data.get("description_html")),
        "subscriber_count": to_int(data.get("subscribers")),
        "active_user_count": to_int(data.get("active_user_count") or data.get("accounts_active")),
        "created_at": iso_utc(data.get("created_utc")),
        "type": clean(data.get("subreddit_type")),
        "language": clean(data.get("lang")),
        "icon": image_url(data.get("community_icon")) or image_url(data.get("icon_img")),
        "banner": {
            "image": image_url(data.get("banner_img")),
            "background_image": image_url(data.get("banner_background_image")),
            "mobile_image": image_url(data.get("mobile_banner_image")),
            "color": clean(data.get("banner_background_color")),
        },
        "colors": {"primary": clean(data.get("primary_color")), "key": clean(data.get("key_color"))},
        "header": {"image": image_url(data.get("header_img")), "title": clean(data.get("header_title"))},
        "flags": {
            "is_nsfw": to_bool(data.get("over18")) if "over18" in data else to_bool(data.get("over_18")),
            "is_quarantined": to_bool(data.get("quarantine")),
            "is_wiki_enabled": to_bool(data.get("wiki_enabled")),
            "is_spoilers_enabled": to_bool(data.get("spoilers_enabled")),
            "is_link_flair_enabled": to_bool(data.get("link_flair_enabled")),
            "is_user_flair_enabled": to_bool(data.get("user_flair_enabled_in_sr")),
            "is_emojis_enabled": to_bool(data.get("emojis_enabled")),
            "is_crosspostable": to_bool(data.get("is_crosspostable_subreddit")),
            "is_all_original_content": to_bool(data.get("all_original_content")),
            "is_community_reviewed": to_bool(data.get("community_reviewed")),
            "accepts_followers": to_bool(data.get("accept_followers")),
            "allows_images": to_bool(data.get("allow_images")),
            "allows_videos": to_bool(data.get("allow_videos")),
            "allows_video_gifs": to_bool(data.get("allow_videogifs")),
            "allows_galleries": to_bool(data.get("allow_galleries")),
            "allows_polls": to_bool(data.get("allow_polls")),
            "allows_talks": to_bool(data.get("allow_talks")),
            "allows_discovery": to_bool(data.get("allow_discovery")),
            "allows_free_form_reports": to_bool(data.get("free_form_reports")),
            "restricts_posting": to_bool(data.get("restrict_posting")),
            "restricts_commenting": to_bool(data.get("restrict_commenting")),
            "shows_media": to_bool(data.get("show_media")),
            "archives_posts": to_bool(data.get("should_archive_posts")),
            "has_menu_widget": to_bool(data.get("has_menu_widget")),
        },
        "submission": {
            "type": clean(data.get("submission_type")),
            "text": clean(data.get("submit_text")),
            "text_html": clean(data.get("submit_text_html")),
            "link_label": clean(data.get("submit_link_label")),
            "text_label": clean(data.get("submit_text_label")),
            "allowed_media_in_comments": [clean(m) for m in (data.get("allowed_media_in_comments") or []) if clean(m)],
        },
        "suggested_comment_sort": clean(data.get("suggested_comment_sort")),
        "advertiser_category": clean(data.get("advertiser_category")),
        "comment_score_hide_minutes": to_int(data.get("comment_score_hide_mins")),
        "link_flair_position": clean(data.get("link_flair_position")),
        "user_flair_position": clean(data.get("user_flair_position")),
    }


def subreddit_compact(thing):
    """The short form used inside lists that carry only a few fields
    (moderated subreddits, autocomplete)."""
    data = _data(thing)
    name = clean(data.get("display_name") or data.get("sr"))
    if not name:
        return None
    return {
        "id": refs.strip_kind(clean(data.get("name"))) or clean(data.get("id")),
        "name": name,
        "link": refs.subreddit_link(name),
        "title": clean(data.get("title")),
        "description": clean(data.get("public_description")),
        "subscriber_count": to_int(data.get("subscribers")),
        "created_at": iso_utc(data.get("created_utc")),
        "type": clean(data.get("subreddit_type")),
        "icon": image_url(data.get("community_icon")) or image_url(data.get("icon_img")),
        "banner_image": image_url(data.get("banner_img")),
        "primary_color": clean(data.get("primary_color")),
        "is_nsfw": to_bool(data.get("over_18")) if "over_18" in data else to_bool(data.get("over18")),
    }


def moderated_subreddit(data):
    out = subreddit_compact(data)
    if out is None:
        return None
    out["mod_permissions"] = [clean(p) for p in (_data(data).get("mod_permissions") or []) if clean(p)]
    return out


def rules(payload):
    """/r/<sr>/about/rules -> {rules[], site_rules[]}."""
    payload = payload if isinstance(payload, dict) else {}
    out = []
    for rule in payload.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        out.append({
            "name": clean(rule.get("short_name")),
            "description": clean(rule.get("description")),
            "description_html": clean(rule.get("description_html")),
            "applies_to": {"all": "posts and comments", "link": "posts", "comment": "comments"}.get(clean(rule.get("kind")), clean(rule.get("kind"))),
            "violation_reason": clean(rule.get("violation_reason")),
            "priority": to_int(rule.get("priority")),
            "created_at": iso_utc(rule.get("created_utc")),
        })
    site_rules = [clean(r) for r in (payload.get("site_rules") or []) if clean(r)]
    return {"rules": out, "site_rules": site_rules}


def wiki_page(payload, name=None):
    data = _data(payload)
    if not isinstance(data, dict) or ("content_md" not in data and "content_html" not in data):
        return None
    reviser = data.get("revision_by")
    return {
        "name": name,
        "content_markdown": clean(data.get("content_md")),
        "content_html": clean(data.get("content_html")),
        "revised_at": iso_utc(data.get("revision_date")),
        "revised_by": user_compact(reviser) if reviser else None,
        "revision_id": clean(data.get("revision_id")),
    }


# ---- user -----------------------------------------------------------------------------------

def user_compact(thing):
    data = _data(thing)
    name = clean(data.get("name"))
    if not name:
        return None
    return {"id": refs.strip_kind(clean(data.get("id"))) if clean(data.get("id")) else None,
            "username": name, "link": refs.user_link(name)}


def user(thing):
    data = _data(thing)
    name = clean(data.get("name"))
    if not data or not name:
        return None
    profile = data.get("subreddit") or {}
    is_suspended = to_bool(data.get("is_suspended"))
    return {
        "id": refs.strip_kind(clean(data.get("id"))),
        "username": name,
        "link": refs.user_link(name),
        "created_at": iso_utc(data.get("created_utc")),
        "avatar": image_url(data.get("icon_img")),
        "snoovatar": image_url(data.get("snoovatar_img")),
        "karma": {
            "total": to_int(data.get("total_karma")),
            "post": to_int(data.get("link_karma")),
            "comment": to_int(data.get("comment_karma")),
            "awardee": to_int(data.get("awardee_karma")),
            "awarder": to_int(data.get("awarder_karma")),
        },
        "flags": {
            "is_premium": to_bool(data.get("is_gold")),
            "is_moderator": to_bool(data.get("is_mod")),
            "is_employee": to_bool(data.get("is_employee")),
            "is_verified": to_bool(data.get("verified")),
            "has_verified_email": to_bool(data.get("has_verified_email")),
            "is_suspended": bool(is_suspended),
            "accepts_followers": to_bool(data.get("accept_followers")),
            "accepts_chats": to_bool(data.get("accept_chats")),
            "accepts_private_messages": to_bool(data.get("accept_pms")),
            "hides_from_robots": to_bool(data.get("hide_from_robots")),
        },
        "profile": {
            "title": clean(profile.get("title")),
            "description": clean(profile.get("public_description")),
            "banner": image_url(profile.get("banner_img")),
            "icon": image_url(profile.get("community_icon")) or image_url(profile.get("icon_img")),
            "follower_count": to_int(profile.get("subscribers")),
            "is_nsfw": to_bool(profile.get("over_18")),
            "previous_names": [clean(n) for n in (profile.get("previous_names") or []) if clean(n)],
            "primary_color": clean(profile.get("primary_color")),
            "key_color": clean(profile.get("key_color")),
        } if profile else None,
    }


def user_from_account_ids(user_id, data):
    """/api/user_data_by_account_ids item -> the compact user + karma."""
    data = data or {}
    name = clean(data.get("name"))
    if not name:
        return None
    return {
        "id": refs.strip_kind(user_id),
        "username": name,
        "link": refs.user_link(name),
        "created_at": iso_utc(data.get("created_utc")),
        "avatar": image_url(data.get("profile_img")),
        "karma": {"post": to_int(data.get("link_karma")), "comment": to_int(data.get("comment_karma"))},
        "is_nsfw": to_bool(data.get("profile_over_18")),
    }


def trophy(thing):
    data = _data(thing)
    if not data or not data.get("name"):
        return None
    return {
        "id": clean(data.get("id")),
        "name": clean(data.get("name")),
        "description": clean(data.get("description")),
        "granted_at": iso_utc(data.get("granted_at")),
        "icon": image_url(data.get("icon_70")) or image_url(data.get("icon_40")),
        "link": clean(data.get("url")),
        "award_id": clean(data.get("award_id")),
    }


# ---- listings ----------------------------------------------------------------------------------

def listing_children(payload):
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return [], None
    return data.get("children") or [], clean(data.get("after"))


def parse_listing(payload, item_parser, key="items", **extra):
    """A Listing -> {key: [...], next_cursor, has_more, ...extra}."""
    children, after = listing_children(payload)
    items = []
    for child in children:
        parsed = item_parser(child)
        if parsed:
            items.append(parsed)
    out = {key: items, "next_cursor": after, "has_more": bool(after)}
    out.update(extra)
    return out


def thing(child):
    """Any t1 / t3 / t5 / t2 thing -> its parsed entity with a `kind`."""
    if not isinstance(child, dict):
        return None
    kind = child.get("kind")
    if kind == "t3":
        parsed = post(child)
        label = "post"
    elif kind == "t1":
        parsed = comment(child, include_replies=False)
        label = "comment"
    elif kind == "t5":
        parsed = subreddit(child)
        label = "subreddit"
    elif kind == "t2":
        parsed = user(child)
        label = "user"
    else:
        return None
    if parsed is None:
        return None
    return {"kind": label, **parsed}
