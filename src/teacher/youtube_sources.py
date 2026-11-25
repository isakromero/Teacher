"""Knowledge sources from a YouTube video: the video's transcript, stored as Markdown.

`youtube_video_id(url)` tells a YouTube video address from any other: it returns the video id,
None for an address on another host, and raises `UrlSourceError` for an address on a YouTube
host that names no video (a playlist, a channel, a search, the home page). Recognised:
`youtube.com/watch?v=<id>`, `youtu.be/<id>`, `youtube.com/shorts/<id>`, `youtube.com/live/<id>`
and `youtube.com/embed/<id>`, on the bare host and on `www.`, `m.` and `music.` (and the embed
host `youtube-nocookie.com`), over http or https, with any extra query parameters (`t`, `list`,
`si`, ...). `teacher.url_sources.UrlFetcher.fetch` hands every such address to
`YouTubeFetcher.fetch`, which returns an ordinary `FetchedSource` (`kind` "web_page", stored as
`text/markdown`), so the URL endpoints treat a video like any web page; they only copy its
`extra_metadata` into the source's metadata.

Two collaborators, both injectable so tests use fakes:

- the video's details come from the official YouTube Data API v3,
  `GET https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id=<id>&key=<key>`
  (1 quota unit), through an `httpx2.Client`-compatible `client` (by default one per fetch).
  The key is `$YOUTUBE_API_KEY`, read by `teacher.config.youtube_api_key_from_env` on every
  fetch. Used: the title, the channel's name (as the author), the publication date, the
  description (the summary: its opening paragraphs up to `SUMMARY_CHARS`, stopping before a
  chapter list) and the duration (ISO 8601, stored in seconds);
- the transcript comes from the `youtube-transcript-api` library
  (`YouTubeTranscriptApi().list(video_id)`, then `Transcript.fetch()` on the one chosen), through
  `list_transcripts(video_id)`. The Data API cannot download the captions of a video the caller
  does not own, which is why the library is used.

Which transcript (`choose_transcript`): English first, then the video's own language (the Data
API's `defaultAudioLanguage`, else the language of YouTube's automatic track, which is the
spoken one); within a language, one written by a person before one generated automatically,
and the exact code (`en`) before a regional one (`en-GB`). Nothing is translated. The choice is
recorded in the metadata as `transcript_language` (the code) and `transcript_generated`.

The stored file (`transcript_markdown`): `# <title>`, a line naming the channel, the video's
address and the transcript used, then the transcript in paragraphs of about one to one and a
half minutes (a paragraph ends at a sentence end after `PARAGRAPH_MIN_SECONDS`, and in any case
by `PARAGRAPH_MAX_SECONDS`), each starting with a timestamp that links to that moment
(`[12:34](https://www.youtube.com/watch?v=<id>&t=754s)`). Caption text has its HTML entities
unescaped and its whitespace collapsed. If the description lists chapters the way YouTube
recognises them (lines that start with a timestamp, at least `MIN_CHAPTERS`, the first at 0:00,
in increasing order), each becomes a `##` heading where its time is reached; otherwise there
are no headings. `extra_metadata` holds `youtube_video_id`, `channel`, `duration_seconds`,
`transcript_language` and `transcript_generated`; the source's address is the canonical
`https://www.youtube.com/watch?v=<id>`.

Fail loudly: each of these raises `UrlSourceError` (HTTP 422) saying what to do, and nothing is
stored: `$YOUTUBE_API_KEY` not set (or blank); the Data API rejecting the key, its quota used up,
the API not enabled, or any other API error (with Google's reason); an unreachable API; a
video that does not exist or is private; an address that names no video; a live or upcoming
broadcast; captions turned off or no transcript at all; no transcript in English or in the
video's own language; the library being blocked by YouTube, or failing otherwise; a transcript
shorter than `MIN_WORDS` words. There is no fallback: the watch page is never read as HTML, and
a video without a transcript is never stored.

The key is sent only in the Data API request. It never appears in an error message (Google's
text is scrubbed of it, and errors are raised `from None`, so no exception that carries the
request URL is chained to them), in the database or in the stored file. `httpx2` logs each
request URL at INFO; a filter on its logger replaces any `key=` query value in those lines.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from itertools import pairwise
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import requests
import youtube_transcript_api as yta

from teacher.config import ENV_YOUTUBE_API_KEY, youtube_api_key_from_env
from teacher.url_sources import (
    KIND_WEB_PAGE,
    MIN_WORDS,
    FetchedSource,
    PageDetails,
    UrlSourceError,
    word_count,
)

DATA_API_URL = "https://www.googleapis.com/youtube/v3/videos"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
TIMEOUT = 30.0  # seconds, for the Data API request
SUMMARY_CHARS = 500
PARAGRAPH_MIN_SECONDS = 60
PARAGRAPH_MAX_SECONDS = 90
MIN_CHAPTERS = 3  # YouTube's own rule for a chapter list
ENGLISH = "en"
# What the transcript arrives as: YouTube's timed-text captions, an XML document.
TRANSCRIPT_MEDIA_TYPE = "text/xml"

VIDEO_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_PATH_KINDS = frozenset({"shorts", "live", "embed"})  # /<kind>/<id>
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_CHANNEL_PATHS = frozenset({"channel", "c", "user"})

NO_KEY_MESSAGE = (
    f"Adding a YouTube video needs a YouTube Data API v3 key, and ${ENV_YOUTUBE_API_KEY} is not "
    "set. Create a key in the Google Cloud console (APIs & Services > Credentials, with the "
    f"YouTube Data API v3 enabled), add the line {ENV_YOUTUBE_API_KEY}=<your key> to .env, and "
    "restart the server (uv run --env-file .env teacher serve)."
)

# Google's error reasons (the legacy `errors[].reason` and the `ErrorInfo` reason).
_QUOTA_REASONS = frozenset(
    {
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "RATE_LIMIT_EXCEEDED",
    }
)
_DISABLED_REASONS = frozenset({"accessNotConfigured", "SERVICE_DISABLED"})
_KEY_REASONS = frozenset({"keyInvalid", "keyExpired", "ipRefererBlocked"})


def watch_url(video_id: str) -> str:
    return WATCH_URL.format(video_id=video_id)


# -- the address --------------------------------------------------------------------------


def youtube_video_id(url: str) -> str | None:
    """The video id `url` names if it is a YouTube video address; None if it is not on a
    YouTube host (or is not an http(s) address at all). An address on a YouTube host that names
    no video is a UrlSourceError, not None: it must not be fetched as a web page."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None  # not an address; UrlFetcher.check_url says so
    if parts.scheme not in ("http", "https") or host not in VIDEO_HOSTS | SHORT_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    query = parse_qs(parts.query)
    candidate: str | None = None
    if host in SHORT_HOSTS:
        candidate = segments[0] if len(segments) == 1 else None
    elif segments == ["watch"]:
        candidate = (query.get("v") or [None])[0]
    elif len(segments) == 2 and segments[0] in _PATH_KINDS and segments[1] != "videoseries":
        candidate = segments[1]
    if candidate is None:
        raise UrlSourceError(_not_a_video(url.strip(), segments, query))
    if not _VIDEO_ID.fullmatch(candidate):
        raise UrlSourceError(
            f"{url.strip()!r} does not hold a valid YouTube video id ({candidate!r}); a video "
            "id is 11 letters, digits, '-' or '_'. Copy the address from the video's page or "
            "its Share button."
        )
    return candidate


def _not_a_video(url: str, segments: list[str], query: dict[str, list[str]]) -> str:
    if "list" in query or segments[:1] == ["playlist"] or "videoseries" in segments:
        what = "a YouTube playlist"
    elif segments and (segments[0].startswith("@") or segments[0] in _CHANNEL_PATHS):
        what = "a YouTube channel"
    else:
        what = "a YouTube page"
    return (
        f"{url} is {what}, not a video. Teacher adds one video at a time: open the video and "
        "use its address (youtube.com/watch?v=..., youtu.be/..., or youtube.com/shorts/...)."
    )


# -- the transcript -----------------------------------------------------------------------


def _primary(language_code: str) -> str:
    """The language of a code without its region or script: `en-GB` -> `en`."""
    return language_code.replace("_", "-").split("-")[0].lower()


_UNKNOWN_LANGUAGES = frozenset({"und", "zxx", "mul"})


def choose_transcript(available: Sequence[Any], audio_language: str | None, url: str) -> Any:
    """The transcript to store among `available` (objects with `language_code` and
    `is_generated`): English, then the video's own language (`audio_language`, else the
    language of the automatic track); a person's before an automatic one; the exact code
    before a regional one. UrlSourceError if there is none of these."""
    if not available:
        raise UrlSourceError(
            f"YouTube offers no transcript for {url} (no captions, neither written nor "
            "generated automatically), so there is no text to study from."
        )
    video_language = _primary(audio_language) if audio_language else None
    if video_language in _UNKNOWN_LANGUAGES:
        video_language = None
    if video_language is None:
        video_language = next(
            (_primary(t.language_code) for t in available if t.is_generated), None
        )
    for language in dict.fromkeys(filter(None, (ENGLISH, video_language))):
        for generated in (False, True):
            matches = [
                t
                for t in available
                if _primary(t.language_code) == language and bool(t.is_generated) == generated
            ]
            if matches:
                return min(matches, key=lambda t: t.language_code.lower() != language)
    offered = ", ".join(
        f"{t.language_code}{' (automatic)' if t.is_generated else ''}" for t in available
    )
    own = (
        f" or in the video's own language ({video_language})"
        if video_language
        else " and the video's own language is not known"
    )
    raise UrlSourceError(
        f"{url} has no transcript in English{own}; YouTube offers only: {offered}. Teacher "
        "does not translate transcripts."
    )


def list_transcripts(video_id: str) -> Iterable[Any]:
    """The default transcript getter: every transcript YouTube offers for the video, as
    `youtube_transcript_api.Transcript` objects (`language_code`, `is_generated`, `fetch()`).
    A new `YouTubeTranscriptApi` per call, as it is not thread-safe."""
    return yta.YouTubeTranscriptApi().list(video_id)


def _library() -> str:
    return f"youtube-transcript-api {version('youtube-transcript-api')}"


def _transcript_error(exc: Exception, url: str) -> UrlSourceError:
    """The library's exception as a message a reader can act on (its own messages are long
    and addressed to programmers)."""
    name = exc.__class__.__name__
    if isinstance(exc, yta.RequestBlocked):  # IpBlocked too
        return UrlSourceError(
            f"YouTube is blocking transcript requests from this computer's network address "
            f"({name}), so the transcript of {url} could not be fetched. This happens after "
            "many requests in a short time and from cloud or VPN addresses: try again later or "
            "from another network."
        )
    if isinstance(exc, yta.TranscriptsDisabled):
        return UrlSourceError(
            f"YouTube has no transcript for {url}: its captions are turned off or there are "
            "none, neither written nor generated automatically."
        )
    if isinstance(exc, yta.AgeRestricted):
        return UrlSourceError(
            f"{url} is age-restricted: YouTube gives its transcript only to a signed-in "
            "account, which Teacher does not use."
        )
    if isinstance(exc, (yta.VideoUnavailable, yta.InvalidVideoId)):
        return UrlSourceError(
            f"YouTube says {url} is unavailable (removed, private, or blocked where you are)."
        )
    if isinstance(exc, yta.VideoUnplayable):
        reason = exc.reason or "no reason given"
        details = "; ".join(filter(None, exc.sub_reasons))
        return UrlSourceError(
            f"YouTube says {url} cannot be played: {reason}{f' ({details})' if details else ''}"
        )
    if isinstance(exc, yta.YouTubeRequestFailed):
        reason = exc.reason.split(" for url:")[0]
        return UrlSourceError(
            f"A request to YouTube for the transcript of {url} failed ({reason}). Try again "
            "later."
        )
    return UrlSourceError(
        f"The transcript of {url} could not be fetched ({_library()}: {name}). YouTube may "
        "have changed how it serves transcripts; updating youtube-transcript-api may help."
    )


@dataclass(frozen=True)
class Chapter:
    start: int  # seconds
    title: str


_STAMP = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_CHAPTER_LINE = re.compile(rf"^\s*[\[(]?({_STAMP})[\])]?\s*(?:[-–—:|•·]\s*)?(\S.*?)\s*$")


def _seconds(stamp: str) -> int:
    total = 0
    for part in stamp.split(":"):
        total = total * 60 + int(part)
    return total


def _squash(text: str) -> str:
    return " ".join(text.split())


def chapters(description: str) -> list[Chapter]:
    """The chapters a description lists, by YouTube's rule (lines starting with a timestamp,
    at least `MIN_CHAPTERS`, the first at 0:00, increasing); [] when it lists none."""
    found: list[Chapter] = []
    for line in description.splitlines():
        match = _CHAPTER_LINE.match(line)
        if match:
            found.append(Chapter(_seconds(match.group(1)), _squash(match.group(2))))
    if (
        len(found) < MIN_CHAPTERS
        or found[0].start != 0
        or any(b.start <= a.start for a, b in pairwise(found))
    ):
        return []
    return found


def _cut(text: str, limit: int) -> str:
    """`text` shortened to at most `limit` characters: at a sentence end in its second half,
    else at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", head)]
    if ends and ends[-1] >= limit // 2:
        return head[: ends[-1]]
    return head[: head.rfind(" ")].rstrip(" ,;:") + "…" if " " in head else head + "…"


def summary(description: str) -> str | None:
    """The opening of a description: its paragraphs before any chapter list, as many as fit
    in `SUMMARY_CHARS` (the first one shortened if it alone does not); None if it is empty."""
    kept: list[str] = []
    for line in description.splitlines():
        if _CHAPTER_LINE.match(line):
            break
        kept.append(line)
    paragraphs = [_squash(p) for p in re.split(r"\n\s*\n", "\n".join(kept))]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return None
    text = ""
    for paragraph in paragraphs:
        longer = f"{text}\n\n{paragraph}" if text else paragraph
        if len(longer) > SUMMARY_CHARS:
            break
        text = longer
    return text or _cut(paragraphs[0], SUMMARY_CHARS)


def _clock(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def timestamp_link(video_id: str, seconds: float) -> str:
    at = int(seconds)
    return f"[{_clock(at)}]({watch_url(video_id)}&t={at}s)"


def caption_text(text: str) -> str:
    """A caption's text with HTML entities unescaped and whitespace collapsed."""
    return _squash(html.unescape(text))


_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")


def transcript_markdown(
    *,
    title: str,
    channel: str,
    video_id: str,
    snippets: Iterable[Any],
    chapter_list: Sequence[Chapter],
    transcript_language: str,
    transcript_generated: bool,
) -> str:
    """The stored file: see the module docstring. `snippets` have `text` and `start`
    (seconds)."""
    kind = "generated automatically" if transcript_generated else "written by a person"
    blocks = [
        f"# {title}",
        f"Video by {channel} on YouTube: {watch_url(video_id)} "
        f"(transcript: {transcript_language}, {kind})",
    ]
    pending = list(chapter_list)
    paragraph: list[str] = []
    started = 0.0

    def flush() -> None:
        if paragraph:
            blocks.append(f"{timestamp_link(video_id, started)} {' '.join(paragraph)}")
            paragraph.clear()

    for snippet in sorted(snippets, key=lambda s: s.start):
        text = caption_text(snippet.text)
        if not text:
            continue
        while pending and pending[0].start <= snippet.start:
            flush()
            blocks.append(f"## {pending.pop(0).title}")
        elapsed = snippet.start - started
        if paragraph and (
            elapsed >= PARAGRAPH_MAX_SECONDS
            or (elapsed >= PARAGRAPH_MIN_SECONDS and _SENTENCE_END.search(paragraph[-1]))
        ):
            flush()
        if not paragraph:
            started = snippet.start
        paragraph.append(text)
    flush()
    return "\n\n".join(blocks) + "\n"


# -- the Data API -------------------------------------------------------------------------

_DURATION = re.compile(
    r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?"
)
_PUBLISHED = re.compile(r"(\d{4}-\d{2}-\d{2})T")


def duration_seconds(value: str) -> int:
    """An ISO 8601 duration as the Data API gives it (`PT1H2M3S`, `P1DT2M`, `P0D`) in whole
    seconds; UrlSourceError for anything else."""
    match = _DURATION.fullmatch(value)
    if match is None or value.endswith(("P", "T")):
        raise UrlSourceError(
            f"The YouTube Data API gave a video duration Teacher cannot read: {value!r}"
        )
    weeks, days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return int(weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "<redacted>") if key else text


def _google_error(response: Any, key: str) -> tuple[list[str], str]:
    """Google's error reasons and message from an error response, scrubbed of the key."""
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if not isinstance(error, dict):
        return [], _scrub(_squash(response.text)[:300], key)
    reasons: list[str] = []
    for item in [*(error.get("errors") or []), *(error.get("details") or [])]:
        reason = item.get("reason") if isinstance(item, dict) else None
        if isinstance(reason, str) and reason not in reasons:
            reasons.append(reason)
    return reasons, _scrub(_squash(str(error.get("message") or "")), key)


def _api_error(response: Any, key: str) -> UrlSourceError:
    status = response.status_code
    reasons, message = _google_error(response, key)
    said = f"HTTP {status}, {', '.join(reasons) or 'no reason given'}"
    if message:
        said += f": {message}"
    found = set(reasons)
    if found & _QUOTA_REASONS:
        return UrlSourceError(
            f"The YouTube Data API quota of ${ENV_YOUTUBE_API_KEY} is used up ({said}). The "
            "daily quota resets at midnight Pacific time; try again then, or raise the quota in "
            "the Google Cloud console."
        )
    if found & _DISABLED_REASONS:
        return UrlSourceError(
            "The YouTube Data API v3 is not enabled for the Google Cloud project of "
            f"${ENV_YOUTUBE_API_KEY} ({said}). Enable it in the Google Cloud console (APIs & "
            "Services > Library) and try again."
        )
    if (
        found & _KEY_REASONS
        or any(r.startswith("API_KEY_") for r in reasons)
        or "API key" in message
    ):
        return UrlSourceError(
            f"The YouTube Data API rejected ${ENV_YOUTUBE_API_KEY} ({said}). Check the key "
            "in .env (and its restrictions in the Google Cloud console), then restart the "
            "server."
        )
    if status in (400, 401, 403):
        return UrlSourceError(
            f"The YouTube Data API refused the request ({said}). Check ${ENV_YOUTUBE_API_KEY} "
            "in .env and restart the server."
        )
    return UrlSourceError(f"The YouTube Data API answered with an error ({said}).")


def _text_field(data: dict[str, Any], name: str, url: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise UrlSourceError(
            f"The YouTube Data API's answer for {url} has no {name}; Teacher cannot read it."
        )
    return value.strip()


# -- the fetcher --------------------------------------------------------------------------


class _RedactKeyFilter(logging.Filter):
    """Replaces the value of every `key=` query parameter in a log line: `httpx2` logs each
    request URL at INFO, and the Data API request carries the API key there."""

    _KEY_PARAM = re.compile(r"([?&]key=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "key=" in message:
            record.msg, record.args = self._KEY_PARAM.sub(r"\1<redacted>", message), None
        return True


_HTTPX_LOGGER = logging.getLogger("httpx2")
if not any(isinstance(f, _RedactKeyFilter) for f in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_RedactKeyFilter())


TranscriptLister = Callable[[str], Iterable[Any]]


class YouTubeFetcher:
    """Fetches a YouTube video as a `FetchedSource` (see the module docstring).

    `client` may be any `httpx2.Client`-compatible object for the Data API request (tests pass
    one with a `MockTransport`); by default a client is created per fetch. `list_transcripts`
    returns the transcripts YouTube offers for a video id (by default through
    `youtube_transcript_api`); each has `language_code`, `is_generated` and `fetch()`, which
    returns snippets with `text` and `start`.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        list_transcripts: TranscriptLister = list_transcripts,
        timeout: float = TIMEOUT,
    ) -> None:
        self._client = client
        self.list_transcripts = list_transcripts
        self.timeout = timeout

    def fetch(self, requested_url: str, video_id: str) -> FetchedSource:
        url = watch_url(video_id)
        video = self._video(video_id, url)
        snippet, content = video.get("snippet"), video.get("contentDetails")
        if not isinstance(snippet, dict) or not isinstance(content, dict):
            raise UrlSourceError(
                f"The YouTube Data API's answer for {url} has no snippet or contentDetails; "
                "Teacher cannot read it."
            )
        title = _squash(_text_field(snippet, "title", url))
        channel = _squash(_text_field(snippet, "channelTitle", url))
        published = _PUBLISHED.match(_text_field(snippet, "publishedAt", url))
        duration = duration_seconds(_text_field(content, "duration", url))
        broadcast = snippet.get("liveBroadcastContent")
        if broadcast in ("live", "upcoming"):
            state = "has not started yet" if broadcast == "upcoming" else "is still running"
            raise UrlSourceError(
                f"{url} is a live broadcast that {state}; YouTube has no transcript of it "
                "until it has ended. Add it again after the broadcast."
            )
        description = snippet.get("description") or ""

        try:
            available = list(self.list_transcripts(video_id))
            chosen = choose_transcript(available, snippet.get("defaultAudioLanguage"), url)
            snippets = list(chosen.fetch())
        except yta.YouTubeTranscriptApiException as exc:
            raise _transcript_error(exc, url) from exc
        except requests.RequestException as exc:
            raise UrlSourceError(
                f"YouTube could not be reached for the transcript of {url} "
                f"({exc.__class__.__name__}). Check the internet connection and try again."
            ) from exc

        words = sum(word_count(caption_text(s.text)) for s in snippets)
        if words < MIN_WORDS:
            raise UrlSourceError(
                f"The transcript of {url} has too little text ({words} words; at least "
                f"{MIN_WORDS} are needed)."
            )
        language, generated = chosen.language_code, bool(chosen.is_generated)
        markdown = transcript_markdown(
            title=title,
            channel=channel,
            video_id=video_id,
            snippets=snippets,
            chapter_list=chapters(description),
            transcript_language=language,
            transcript_generated=generated,
        )
        return FetchedSource(
            requested_url,
            url,
            KIND_WEB_PAGE,
            "text/markdown",
            ".md",
            markdown.encode("utf-8"),
            TRANSCRIPT_MEDIA_TYPE,
            PageDetails(
                title=title,
                authors=(channel,),
                published=published.group(1) if published else None,
                summary=summary(description),
                site_name="YouTube",
            ),
            words,
            extra_metadata={
                "youtube_video_id": video_id,
                "channel": channel,
                "duration_seconds": duration,
                "transcript_language": language,
                "transcript_generated": generated,
            },
        )

    def _video(self, video_id: str, url: str) -> dict[str, Any]:
        """The Data API's resource for the video. Every failure is a UrlSourceError raised
        `from None`: the exceptions of the request carry its URL, and the URL the key."""
        try:
            key = youtube_api_key_from_env()
        except ValueError as exc:
            raise UrlSourceError(f"Adding a YouTube video needs an API key: {exc}") from None
        if key is None:
            raise UrlSourceError(NO_KEY_MESSAGE)
        params = {"part": "snippet,contentDetails", "id": video_id, "key": key}
        try:
            if self._client is not None:
                response = self._client.get(DATA_API_URL, params=params, timeout=self.timeout)
            else:
                with httpx2.Client() as client:
                    response = client.get(DATA_API_URL, params=params, timeout=self.timeout)
        except httpx2.TimeoutException:
            raise UrlSourceError(
                f"The YouTube Data API did not answer within {self.timeout:g} seconds."
            ) from None
        except httpx2.HTTPError as exc:
            raise UrlSourceError(
                f"The YouTube Data API could not be reached ({exc.__class__.__name__}: "
                f"{_scrub(str(exc), key)})."
            ) from None
        if response.status_code != 200:
            raise _api_error(response, key) from None
        try:
            data = response.json()
        except ValueError:
            raise UrlSourceError(
                "The YouTube Data API answered with something that is not JSON."
            ) from None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise UrlSourceError(
                f"The YouTube Data API's answer for {url} lists no items; Teacher cannot read it."
            )
        if not items or not isinstance(items[0], dict):
            raise UrlSourceError(
                f"YouTube has no video {video_id!r} ({url}): it does not exist, was removed, or "
                "is private. Check the address; a private video cannot be added."
            )
        return items[0]


__all__ = [
    "DATA_API_URL",
    "MIN_CHAPTERS",
    "PARAGRAPH_MAX_SECONDS",
    "PARAGRAPH_MIN_SECONDS",
    "SUMMARY_CHARS",
    "Chapter",
    "YouTubeFetcher",
    "chapters",
    "choose_transcript",
    "duration_seconds",
    "list_transcripts",
    "summary",
    "timestamp_link",
    "transcript_markdown",
    "watch_url",
    "youtube_video_id",
]
