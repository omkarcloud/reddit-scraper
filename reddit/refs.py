"""Reddit reference parsing: ONE param per input that auto-detects its forms
(tripadvisor QueryOrIdField convention — never a sibling `url`/`id` pair).

  subreddit  python | r/python | /r/python/ | t5_2qh0y (fullname)
             | https://www.reddit.com/r/python[/hot|/comments/...]
             | https://old.reddit.com/r/python | u_spez (a profile subreddit)
    -> {"name": "python", "id": None} or {"name": None, "id": "t5_2qh0y"}

  post       1wn1d94 (base-36 id) | t3_1wn1d94
             | https://www.reddit.com/r/python/comments/1wn1d94/slug/
             | https://www.reddit.com/comments/1wn1d94 | https://redd.it/1wn1d94
             | https://www.reddit.com/gallery/1wn1d94
             | https://www.reddit.com/user/spez/comments/1wn1d94/slug/
             | https://www.reddit.com/r/python/s/AbCdEf123 (a share link; the
               id is resolved later by following its redirect)
    -> {"id": "1wn1d94", "share": None} or {"id": None, "share": "/r/python/s/AbCdEf123"}

  user       spez | u/spez | /u/spez/ | t2_1w72 (fullname)
             | https://www.reddit.com/user/spez[/comments] | https://www.reddit.com/u/spez
    -> {"name": "spez", "id": None} or {"name": None, "id": "t2_1w72"}

  comment    paj0q5z | t1_paj0q5z
             | https://www.reddit.com/r/x/comments/1wjjbga/slug/paj0q5z/
             | https://www.reddit.com/comments/1wjjbga/comment/paj0q5z/
    -> {"id": "paj0q5z", "post_id": "1wjjbga" or None}

Ids are Reddit's base-36 strings (case-insensitive, lower-cased here).
"""
import re
from urllib.parse import unquote, urlparse

SITE = "https://www.reddit.com"

_HOST_RE = re.compile(r"(^|\.)(reddit\.com|redd\.it)$")
_BARE_LINK_RE = re.compile(r"^(?:www\.|old\.|new\.|np\.|m\.|sh\.|i\.)?(?:reddit\.com|redd\.it)(?:[/?#]|$)", re.I)
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,23}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,24}$")
_ID36_RE = re.compile(r"^[a-z0-9]{1,12}$")
_FULLNAME_RE = re.compile(r"^t([1235])_([a-z0-9]{1,12})$")
_SHARE_RE = re.compile(r"^/r/[A-Za-z0-9_]+/s/[A-Za-z0-9]+/?$")


def _as_url(text):
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("//"):
        return "https:" + text
    return "https://" + text


def _looks_like_link(text):
    low = text.lower()
    return low.startswith(("http://", "https://", "//")) or bool(_BARE_LINK_RE.match(text))


def _parse_link(text, kind):
    parsed = urlparse(_as_url(text))
    host = (parsed.hostname or "").lower()
    if not _HOST_RE.search(host):
        raise ValueError(f"{kind} link must be a reddit.com link")
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    return parsed, host, parts


def _fullname(text, kind_digit):
    match = _FULLNAME_RE.match(text.lower())
    if match and match.group(1) == kind_digit:
        return match.group(2)
    return None


# ---- subreddit ---------------------------------------------------------------------------

def resolve_subreddit(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("subreddit must not be empty")
    if _looks_like_link(text):
        _, _, parts = _parse_link(text, "subreddit")
        if len(parts) >= 2 and parts[0].lower() == "r":
            text = parts[1]
        elif len(parts) >= 2 and parts[0].lower() in ("user", "u"):
            text = "u_" + parts[1]
        else:
            raise ValueError(f"no /r/<subreddit> in link {value!r}")
    text = text.strip("/")
    if text.lower().startswith("r/"):
        text = text[2:].strip("/")
    fullname = _fullname(text, "5")
    if fullname:
        return {"name": None, "id": "t5_" + fullname}
    if not _SUBREDDIT_RE.match(text):
        raise ValueError(f"subreddit must be a subreddit name, r/name, t5_ id or a reddit.com link, got {value!r}")
    return {"name": text, "id": None}


# ---- post ----------------------------------------------------------------------------------

def resolve_post(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("post must not be empty")
    if _looks_like_link(text):
        parsed, host, parts = _parse_link(text, "post")
        low = [p.lower() for p in parts]
        if host.endswith("redd.it") and parts:
            return _post_id(parts[0], value)
        if "comments" in low:
            i = low.index("comments")
            if i + 1 < len(parts):
                return _post_id(parts[i + 1], value)
        if low and low[0] == "gallery" and len(parts) >= 2:
            return _post_id(parts[1], value)
        if _SHARE_RE.match(parsed.path):
            return {"id": None, "share": parsed.path.rstrip("/")}
        raise ValueError(f"no post id in link {value!r}")
    fullname = _fullname(text, "3")
    if fullname:
        return {"id": fullname, "share": None}
    return _post_id(text, value)


def _post_id(text, original):
    text = text.strip().lower()
    if not _ID36_RE.match(text):
        raise ValueError(f"post must be a Reddit post id, t3_ fullname or a reddit.com post link, got {original!r}")
    return {"id": text, "share": None}


# ---- user ----------------------------------------------------------------------------------

def resolve_user(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("user must not be empty")
    if _looks_like_link(text):
        _, _, parts = _parse_link(text, "user")
        if len(parts) >= 2 and parts[0].lower() in ("user", "u"):
            text = parts[1]
        elif len(parts) >= 2 and parts[0].lower() == "r" and parts[1].lower().startswith("u_"):
            text = parts[1][2:]
        else:
            raise ValueError(f"no /user/<name> in link {value!r}")
    text = text.strip("/")
    if text.lower().startswith("u/"):
        text = text[2:].strip("/")
    fullname = _fullname(text, "2")
    if fullname:
        return {"name": None, "id": "t2_" + fullname}
    if not _USERNAME_RE.match(text):
        raise ValueError(f"user must be a Reddit username, u/name, t2_ id or a reddit.com profile link, got {value!r}")
    return {"name": text, "id": None}


# ---- comment -------------------------------------------------------------------------------

def resolve_comment(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("comment must not be empty")
    if _looks_like_link(text):
        _, _, parts = _parse_link(text, "comment")
        low = [p.lower() for p in parts]
        post_id = None
        comment_id = None
        if "comments" in low:
            i = low.index("comments")
            if i + 1 < len(parts):
                post_id = parts[i + 1].lower()
            rest = parts[i + 2:]
            # /comments/<post>/<slug>/<comment>  or  /comments/<post>/comment/<comment>
            if len(rest) >= 2:
                comment_id = rest[1]
            elif len(rest) == 1 and rest[0].lower() != "comment":
                comment_id = None
        if not comment_id or not _ID36_RE.match(comment_id.lower()):
            raise ValueError(f"no comment id in link {value!r}")
        return {"id": comment_id.lower(), "post_id": post_id if post_id and _ID36_RE.match(post_id) else None}
    fullname = _fullname(text, "1")
    if fullname:
        return {"id": fullname, "post_id": None}
    text = text.lower()
    if not _ID36_RE.match(text):
        raise ValueError(f"comment must be a Reddit comment id, t1_ fullname or a comment link, got {value!r}")
    return {"id": text, "post_id": None}


# ---- links ------------------------------------------------------------------------------------

def subreddit_link(name):
    return f"{SITE}/r/{name}/" if name else None


def user_link(name):
    return f"{SITE}/user/{name}/" if name else None


def post_link(permalink=None, post_id=None):
    if permalink:
        return SITE + permalink if permalink.startswith("/") else permalink
    return f"{SITE}/comments/{post_id}/" if post_id else None


def comment_link(permalink=None, post_id=None, comment_id=None):
    if permalink:
        return SITE + permalink if permalink.startswith("/") else permalink
    if post_id and comment_id:
        return f"{SITE}/comments/{post_id}/comment/{comment_id}/"
    return None


def strip_kind(fullname):
    """'t3_1wn1d94' -> '1wn1d94'; a bare id passes through; None stays None."""
    if not fullname:
        return None
    text = str(fullname)
    return text.split("_", 1)[1] if "_" in text[:3] else text
