"""Public-data client for Xiaoyuzhou FM.

Reads only what the public web pages expose. No account, no token, no forged
device fingerprint, no reverse-engineered app endpoints.

Scope and why it is drawn here:

- Entry point is ``https://www.xiaoyuzhoufm.com/episode/<eid>``, which
  ``robots.txt`` does not disallow (it only excludes ``/www``, ``/www/login``
  and ``/www/recharge``).
- The page embeds a ``__NEXT_DATA__`` JSON blob carrying the episode metadata,
  shownotes, the audio URL and the first page of comments. We parse that
  instead of scraping rendered markup, because it is stable and typed.
- We send an ordinary desktop browser User-Agent. Nothing is spoofed: the
  requests are what a browser visiting the same URL would send.

Deliberately NOT implemented:

- Official transcripts. The page exposes only ``transcript.mediaId`` with no
  URL; obtaining one requires the app's private, token-authenticated endpoint
  whose CDN enforces a User-Agent allowlist. ``audio_url`` is returned so a
  caller can transcribe locally instead.
- Full comment threads. The page carries the first ~20 comments and the web
  surface has no pagination endpoint (probed: HTTP 404). ``comment_count``
  reports the true total so a caller can see what it is missing.
- Anything requiring a login: subscriptions, play history, unread state.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

WEB_BASE = "https://www.xiaoyuzhoufm.com"

# An ordinary desktop Chrome UA. This is not a disguise: we are a program
# fetching a public page, and this is what that page is served to.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# Minimum gap between requests to the same host. Not an evasion measure —
# plain courtesy, so a batch never behaves like a burst against one origin.
MIN_REQUEST_INTERVAL = 1.5

# Xiaoyuzhou is a China service and both the app and its listeners read episode
# dates as Beijing days, while the API reports pubDate in UTC.
CN_TZ = timezone(timedelta(hours=8))

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# Fields on a comment author that identify a person rather than describe the
# comment. Dropped unless the caller explicitly asks for them.
_AUTHOR_PRIVATE_FIELDS = ("uid", "avatar", "bio", "gender", "readTrackInfo")


class XiaoyuzhouError(Exception):
    """Base error carrying a machine-readable kind and an actionable hint."""

    def __init__(self, kind: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        d = {"error": self.kind, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


class _TextExtractor(HTMLParser):
    """Flattens shownotes HTML to text, keeping block breaks and link targets.

    Shownotes routinely carry the episode's chapter list and reference links, so
    a naive tag strip loses the URLs that make them useful.
    """

    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    )

    def __init__(self, keep_links: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.keep_links = keep_links
        self._parts: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "li":
            self._parts.append("\n- ")
        if tag == "a" and self.keep_links:
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "a" and self._href:
            # Keep the target inline; a bare anchor text loses the reference.
            self._parts.append(f" <{self._href}>")
            self._href = None

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse the runs of blank lines the block breaks above produce.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def shownotes_to_text(html: str | None, keep_links: bool = True) -> str:
    """Shownotes HTML → readable plain text, preserving structure and links."""
    if not html:
        return ""
    parser = _TextExtractor(keep_links=keep_links)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Never let malformed markup break an otherwise good episode record.
        return unescape(re.sub(r"<[^>]+>", "", html)).strip()
    return parser.text()


def _parse_ts(value: str | None) -> datetime | None:
    """ISO 8601 → aware datetime, or None when absent/unparseable.

    Upstream mixes '.000Z', bare 'Z' and '+08:00' forms, so timestamps must be
    compared as instants rather than as strings.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_date(pub_date: str | None) -> str:
    """UTC ISO timestamp → Beijing-local 'YYYY-MM-DD'."""
    if not pub_date:
        return ""
    dt = _parse_ts(pub_date)
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d") if dt else pub_date[:10]


def format_duration(seconds: Any) -> str:
    """Seconds → 'H:MM:SS', or '' when the value is missing or not a number."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def extract_eid(value: str) -> str:
    """Accepts a bare eid or any xiaoyuzhoufm.com episode URL.

    Callers paste URLs far more often than raw ids, and silently fetching
    ``/episode/https://...`` would 404 with no explanation.
    """
    value = value.strip()
    if not value:
        raise XiaoyuzhouError("bad_eid", "empty episode id")
    if "xiaoyuzhoufm.com" in value:
        m = re.search(r"/episode/([0-9a-fA-F]{24})", value)
        if not m:
            raise XiaoyuzhouError(
                "bad_eid",
                f"no episode id found in URL: {value!r}",
                hint="expected a link like https://www.xiaoyuzhoufm.com/episode/<24-hex-id>",
            )
        return m.group(1)
    if not re.fullmatch(r"[0-9a-fA-F]{24}", value):
        raise XiaoyuzhouError(
            "bad_eid",
            f"not a valid episode id: {value!r}",
            hint="an eid is 24 hex characters; you can also pass the episode URL",
        )
    return value


def extract_pid(value: str) -> str:
    """Accepts a bare pid or any xiaoyuzhoufm.com podcast URL."""
    value = value.strip()
    if not value:
        raise XiaoyuzhouError("bad_pid", "empty podcast id")
    if "xiaoyuzhoufm.com" in value:
        m = re.search(r"/podcast/([0-9a-fA-F]{24})", value)
        if not m:
            raise XiaoyuzhouError(
                "bad_pid",
                f"no podcast id found in URL: {value!r}",
                hint="expected a link like https://www.xiaoyuzhoufm.com/podcast/<24-hex-id>",
            )
        return m.group(1)
    if not re.fullmatch(r"[0-9a-fA-F]{24}", value):
        raise XiaoyuzhouError(
            "bad_pid",
            f"not a valid podcast id: {value!r}",
            hint="a pid is 24 hex characters; you can also pass the podcast URL",
        )
    return value


@dataclass
class Episode:
    """One episode, as the public page describes it."""

    eid: str
    title: str
    podcast_title: str
    podcast_author: str
    pid: str
    pub_date: str
    pub_date_local: str
    duration_seconds: int | None
    duration: str
    audio_url: str | None
    audio_mime: str | None
    audio_size_bytes: int | None
    shownotes_text: str
    shownotes_html: str
    play_count: int | None
    comment_count: int | None
    clap_count: int | None
    favorite_count: int | None
    pay_type: str | None
    cover_url: str | None
    web_url: str
    has_official_transcript: bool

    def as_dict(self, include_html: bool = False) -> dict[str, Any]:
        d = {
            "eid": self.eid,
            "title": self.title,
            "podcast_title": self.podcast_title,
            "podcast_author": self.podcast_author,
            "pid": self.pid,
            "pub_date": self.pub_date,
            "pub_date_local": self.pub_date_local,
            "duration_seconds": self.duration_seconds,
            "duration": self.duration,
            "audio_url": self.audio_url,
            "audio_mime": self.audio_mime,
            "audio_size_bytes": self.audio_size_bytes,
            "shownotes_text": self.shownotes_text,
            "play_count": self.play_count,
            "comment_count": self.comment_count,
            "clap_count": self.clap_count,
            "favorite_count": self.favorite_count,
            "pay_type": self.pay_type,
            "cover_url": self.cover_url,
            "web_url": self.web_url,
            "has_official_transcript": self.has_official_transcript,
        }
        if include_html:
            d["shownotes_html"] = self.shownotes_html
        return d


def _normalize_comment(c: dict, include_identity: bool = False) -> dict[str, Any]:
    """One comment, with replies. Author identity fields dropped by default."""
    author = c.get("author") or {}
    out: dict[str, Any] = {
        "id": c.get("id"),
        "author": author.get("nickname"),
        # PODCASTER marks the show's own account — worth surfacing, since a
        # host's reply usually carries more weight than a listener's.
        "is_podcaster": c.get("authorAssociation") == "PODCASTER",
        "pinned": bool(c.get("pinned")),
        "text": c.get("text"),
        "like_count": c.get("likeCount"),
        "reply_count": c.get("replyCount"),
        "created_at": c.get("createdAt"),
        "ip_location": c.get("ipLoc"),
    }
    if include_identity:
        out["author_detail"] = {k: author.get(k) for k in _AUTHOR_PRIVATE_FIELDS}
    replies = c.get("replies") or []
    if replies:
        out["replies"] = [
            {
                "id": r.get("id"),
                "author": (r.get("author") or {}).get("nickname"),
                "is_podcaster": r.get("authorAssociation") == "PODCASTER",
                "text": r.get("text"),
                "like_count": r.get("likeCount"),
                "created_at": r.get("createdAt"),
                "ip_location": r.get("ipLoc"),
            }
            for r in replies
            if isinstance(r, dict)
        ]
    return out


class PublicClient:
    """Fetches public Xiaoyuzhou pages. Stateless apart from rate limiting."""

    def __init__(self, timeout: float = 30.0, min_interval: float | None = None) -> None:
        self.min_interval = MIN_REQUEST_INTERVAL if min_interval is None else min_interval
        self._last_request = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PublicClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def _get_page(self, url: str) -> dict:
        """Fetch a page and return its ``__NEXT_DATA__`` pageProps."""
        self._wait_turn()
        try:
            r = self._http.get(url)
        except httpx.HTTPError as e:
            raise XiaoyuzhouError(
                "network_error",
                f"could not reach {url}: {e}",
                hint="check your connection, then retry",
            ) from e
        if r.status_code == 404:
            raise XiaoyuzhouError(
                "not_found",
                f"{url} returned 404",
                hint="the id may be wrong, or the episode was removed",
            )
        if r.status_code != 200:
            raise XiaoyuzhouError(
                "http_error", f"{url} returned HTTP {r.status_code}"
            )
        m = _NEXT_DATA_RE.search(r.text)
        if not m:
            raise XiaoyuzhouError(
                "page_shape_changed",
                "no __NEXT_DATA__ block in the page",
                hint="the site layout likely changed; this tool needs updating",
            )
        try:
            data = json.loads(m.group(1))
        except ValueError as e:
            raise XiaoyuzhouError(
                "page_parse_failed", f"__NEXT_DATA__ is not valid JSON: {e}"
            ) from e
        props = (data.get("props") or {}).get("pageProps")
        if not isinstance(props, dict):
            raise XiaoyuzhouError(
                "page_shape_changed", "__NEXT_DATA__ has no props.pageProps object"
            )
        return props

    def get_episode(self, eid_or_url: str) -> tuple[Episode, list[dict[str, Any]]]:
        """One episode plus the comments the page carries (first page only)."""
        eid = extract_eid(eid_or_url)
        url = f"{WEB_BASE}/episode/{eid}"
        props = self._get_page(url)
        raw = props.get("episode")
        if not isinstance(raw, dict) or not raw.get("eid"):
            raise XiaoyuzhouError(
                "no_episode_data",
                f"page for {eid} carried no episode object",
                hint="the episode may be private, paid-only or withdrawn",
            )
        # `or {}` throughout: upstream sends explicit nulls for absent objects,
        # and a .get default only applies when the key itself is missing.
        podcast = raw.get("podcast") or {}
        enclosure = raw.get("enclosure") or {}
        media = raw.get("media") or {}
        media_source = media.get("source") or {}
        shownotes_html = raw.get("shownotes") or ""
        episode = Episode(
            eid=raw.get("eid") or eid,
            title=raw.get("title") or "",
            podcast_title=podcast.get("title") or "",
            podcast_author=podcast.get("author") or "",
            pid=raw.get("pid") or podcast.get("pid") or "",
            pub_date=raw.get("pubDate") or "",
            pub_date_local=local_date(raw.get("pubDate")),
            duration_seconds=raw.get("duration"),
            duration=format_duration(raw.get("duration")),
            audio_url=media_source.get("url") or enclosure.get("url"),
            audio_mime=media.get("mimeType"),
            audio_size_bytes=media.get("size"),
            shownotes_text=shownotes_to_text(shownotes_html),
            shownotes_html=shownotes_html,
            play_count=raw.get("playCount"),
            comment_count=raw.get("commentCount"),
            clap_count=raw.get("clapCount"),
            favorite_count=raw.get("favoriteCount"),
            pay_type=raw.get("payType"),
            cover_url=(raw.get("image") or podcast.get("image") or {}).get("picUrl"),
            web_url=url,
            # An official transcript exists upstream, but the public page gives
            # no URL for it. Reported so callers know local transcription is
            # the only route, rather than assuming none exists.
            has_official_transcript=bool((raw.get("transcript") or {}).get("mediaId")),
        )
        raw_comments = props.get("comments")
        comments = [c for c in (raw_comments or []) if isinstance(c, dict)]
        return episode, comments

    def get_comments(
        self, eid_or_url: str, include_identity: bool = False
    ) -> dict[str, Any]:
        """Comments the public page carries, with an explicit truncation note."""
        episode, raw = self.get_episode(eid_or_url)
        items = [_normalize_comment(c, include_identity=include_identity) for c in raw]
        total = episode.comment_count
        out: dict[str, Any] = {
            "eid": episode.eid,
            "title": episode.title,
            "comment_count": total,
            "returned": len(items),
            "comments": items,
        }
        if isinstance(total, int) and total > len(items):
            # State the gap rather than let a caller mistake 20 for all of them.
            out["truncated"] = True
            out["note"] = (
                f"the public page carries {len(items)} of {total} comments; "
                "the web surface has no pagination endpoint, so the rest are "
                "only reachable through the login-only app API"
            )
        else:
            out["truncated"] = False
        return out

    def get_podcast(self, pid_or_url: str) -> dict[str, Any]:
        """A show's metadata plus the episodes its page lists (newest first)."""
        pid = extract_pid(pid_or_url)
        url = f"{WEB_BASE}/podcast/{pid}"
        props = self._get_page(url)
        pod = props.get("podcast")
        if not isinstance(pod, dict) or not pod.get("pid"):
            raise XiaoyuzhouError(
                "no_podcast_data", f"page for {pid} carried no podcast object"
            )
        episodes = [
            {
                "eid": e.get("eid"),
                "title": e.get("title"),
                "pub_date": e.get("pubDate"),
                "pub_date_local": local_date(e.get("pubDate")),
                "duration_seconds": e.get("duration"),
                "duration": format_duration(e.get("duration")),
                "comment_count": e.get("commentCount"),
                "play_count": e.get("playCount"),
                "web_url": f"{WEB_BASE}/episode/{e.get('eid')}",
            }
            for e in (pod.get("episodes") or [])
            if isinstance(e, dict) and e.get("eid")
        ]
        total = pod.get("episodeCount")
        out: dict[str, Any] = {
            "pid": pod.get("pid") or pid,
            "title": pod.get("title"),
            "author": pod.get("author"),
            "brief": pod.get("brief"),
            "description": pod.get("description"),
            "subscription_count": pod.get("subscriptionCount"),
            "episode_count": total,
            "latest_episode_pub_date": pod.get("latestEpisodePubDate"),
            "cover_url": (pod.get("image") or {}).get("picUrl"),
            "web_url": url,
            "episodes_returned": len(episodes),
            "episodes": episodes,
        }
        if isinstance(total, int) and total > len(episodes):
            out["truncated"] = True
            out["note"] = (
                f"the public page lists {len(episodes)} of {total} episodes; "
                "older ones are not exposed on the web surface"
            )
        else:
            out["truncated"] = False
        return out

    def iter_audio(
        self, url: str, chunk_size: int = 1 << 16
    ) -> Iterator[tuple[bytes, int | None]]:
        """Stream an episode's audio, yielding (chunk, total_bytes_or_None)."""
        self._wait_turn()
        try:
            with self._http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise XiaoyuzhouError(
                        "audio_http_error",
                        f"audio CDN returned HTTP {r.status_code}",
                        hint="the signed URL may have expired; re-fetch the episode",
                    )
                raw_len = r.headers.get("content-length")
                total = int(raw_len) if raw_len and raw_len.isdigit() else None
                for chunk in r.iter_bytes(chunk_size):
                    yield chunk, total
        except httpx.HTTPError as e:
            raise XiaoyuzhouError(
                "audio_fetch_failed", f"could not download audio: {e}"
            ) from e

    def download_audio(
        self, episode: Episode, dest: Path, on_progress: Any = None
    ) -> Path:
        """Download an episode's audio to ``dest``.

        Writes to a temp file and renames on success, so an interrupted run
        never leaves a half file that looks complete.
        """
        if not episode.audio_url:
            raise XiaoyuzhouError(
                "no_audio_url",
                f"episode {episode.eid} exposes no audio URL",
                hint="paid or private episodes do not publish a public media URL",
            )
        dest = Path(dest)
        if dest.is_dir():
            dest = dest / default_audio_name(episode)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        written = 0
        try:
            with open(tmp, "wb") as f:
                for chunk, total in self.iter_audio(episode.audio_url):
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dest


_EXT_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
}


def audio_extension(episode: Episode) -> str:
    """Pick a file extension from the MIME type, falling back to the URL.

    Xiaoyuzhou serves both mp3 and m4a; naming an AAC file `.mp3` confuses
    players and transcription tools alike.
    """
    mime = (episode.audio_mime or "").split(";")[0].strip().lower()
    if mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    url = (episode.audio_url or "").split("?")[0].lower()
    for ext in (".mp3", ".m4a", ".ogg", ".wav", ".aac"):
        if url.endswith(ext):
            return ext
    return ".audio"


_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(name: str, max_length: int = 80) -> str:
    """Make a string safe for a filename on macOS, Linux and Windows alike."""
    cleaned = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or "episode"


def default_audio_name(episode: Episode) -> str:
    """A readable default filename: date, title and the right extension."""
    date = episode.pub_date_local or "unknown-date"
    return f"{date} {safe_filename(episode.title)}{audio_extension(episode)}"
