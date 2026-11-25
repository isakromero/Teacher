"""teacher.youtube_sources: recognising video addresses, choosing and rendering the transcript,
reading the YouTube Data API, and failing loudly. No network: the Data API is a MockTransport and
the transcript getter a fake."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
import requests
import youtube_transcript_api as yta
from youtube_transcript_api import FetchedTranscriptSnippet

from teacher.config import ENV_YOUTUBE_API_KEY
from teacher.url_sources import MIN_WORDS, UrlFetcher, UrlSourceError
from teacher.youtube_sources import (
    SUMMARY_CHARS,
    Chapter,
    YouTubeFetcher,
    chapters,
    choose_transcript,
    duration_seconds,
    summary,
    timestamp_link,
    transcript_markdown,
    youtube_video_id,
)

API_KEY = "AIzaSyTEST-key_0123456789abcdef"
VIDEO_ID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"
DESCRIPTION = (
    "How attention works, from the ground up.\n\n"
    "Slides: https://example.org/slides\n\n"
    "0:00 Intro\n1:00 - Queries and keys\n2:00 Values\n"
)


def video(**snippet: Any) -> dict[str, Any]:
    """A Data API `videos` resource."""
    return {
        "kind": "youtube#video",
        "id": VIDEO_ID,
        "snippet": {
            "publishedAt": "2024-03-05T17:00:12Z",
            "channelId": "UC123",
            "title": "Attention & transformers",
            "description": DESCRIPTION,
            "channelTitle": "Ada Lectures",
            "liveBroadcastContent": "none",
            "defaultAudioLanguage": "en",
            **snippet,
        },
        "contentDetails": {"duration": "PT3M20S"},
    }


def snippets(count: int = 40, step: float = 5.0) -> list[FetchedTranscriptSnippet]:
    """`count` captions of five words each, `step` seconds apart."""
    return [
        FetchedTranscriptSnippet(text=f"word{n} a b c d", start=n * step, duration=step)
        for n in range(count)
    ]


class FakeTranscript:
    def __init__(
        self,
        code: str,
        generated: bool = False,
        lines: list[FetchedTranscriptSnippet] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.language_code = code
        self.is_generated = generated
        self.lines = snippets() if lines is None else lines
        self.error = error
        self.fetched = 0

    def fetch(self) -> list[FetchedTranscriptSnippet]:
        self.fetched += 1
        if self.error is not None:
            raise self.error
        return self.lines


class FakeLister:
    """Stands in for `list_transcripts(video_id)`."""

    def __init__(self, *transcripts: FakeTranscript, error: Exception | None = None) -> None:
        self.transcripts = list(transcripts)
        self.error = error
        self.calls: list[str] = []

    def __call__(self, video_id: str) -> list[FakeTranscript]:
        self.calls.append(video_id)
        if self.error is not None:
            raise self.error
        return self.transcripts


Handler = Callable[[httpx2.Request], httpx2.Response]


def api(status: int = 200, body: Any = None, resource: dict[str, Any] | None = None) -> Handler:
    payload = body if body is not None else {"items": [resource or video()]}
    return lambda request: httpx2.Response(status, json=payload)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(ENV_YOUTUBE_API_KEY, API_KEY)
    return API_KEY


def make(
    handler: Handler | None = None, lister: FakeLister | None = None
) -> tuple[YouTubeFetcher, list[httpx2.Request], FakeLister]:
    seen: list[httpx2.Request] = []
    handler = handler or api()

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    lister = lister if lister is not None else FakeLister(FakeTranscript("en"))
    fetcher = YouTubeFetcher(
        httpx2.Client(transport=httpx2.MockTransport(record)), list_transcripts=lister
    )
    return fetcher, seen, lister


def fail(fetcher: YouTubeFetcher, video_id: str = VIDEO_ID) -> str:
    """The message of the UrlSourceError the fetch raises; it never contains the key."""
    with pytest.raises(UrlSourceError) as info:
        fetcher.fetch(WATCH, video_id)
    error: BaseException | None = info.value
    while error is not None:  # the message and every exception shown with it
        assert API_KEY not in str(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return str(info.value)


# -- addresses ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"http://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}&feature=share",
        f"https://www.youtube.com/watch?feature=youtu.be&v={VIDEO_ID}&t=1m30s&list=PL123&index=2",
        f"  https://WWW.YouTube.com/watch?v={VIDEO_ID}  ",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=abc&t=42",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://youtube.com/shorts/{VIDEO_ID}/?feature=share",
        f"https://www.youtube.com/live/{VIDEO_ID}?si=x",
        f"https://www.youtube.com/embed/{VIDEO_ID}?start=10",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://m.youtube.com/shorts/{VIDEO_ID}",
    ],
)
def test_video_addresses_are_recognised(url: str) -> None:
    assert youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
        "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "not a url",
    ],
)
def test_other_addresses_are_not_youtube(url: str) -> None:
    assert youtube_video_id(url) is None


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://www.youtube.com/playlist?list=PL123", "is a YouTube playlist, not a video"),
        ("https://www.youtube.com/watch?list=PL123", "is a YouTube playlist, not a video"),
        ("https://www.youtube.com/embed/videoseries?list=PL1", "is a YouTube playlist"),
        ("https://www.youtube.com/@AdaLectures", "is a YouTube channel, not a video"),
        ("https://www.youtube.com/@AdaLectures/videos", "is a YouTube channel"),
        ("https://www.youtube.com/channel/UC1234567890", "is a YouTube channel"),
        ("https://www.youtube.com/c/Ada", "is a YouTube channel"),
        ("https://www.youtube.com/user/ada", "is a YouTube channel"),
        ("https://www.youtube.com/", "is a YouTube page, not a video"),
        ("https://www.youtube.com/results?search_query=attention", "is a YouTube page"),
        ("https://www.youtube.com/watch?v=", "is a YouTube page"),
        ("https://youtu.be/", "is a YouTube page"),
        ("https://www.youtube.com/shorts/", "is a YouTube page"),
        ("https://www.youtube.com/watch?v=short", "valid YouTube video id ('short')"),
        ("https://youtu.be/dQw4w9WgXcQ-extra", "valid YouTube video id"),
    ],
)
def test_youtube_addresses_without_a_video_fail_loudly(url: str, message: str) -> None:
    with pytest.raises(UrlSourceError) as info:
        youtube_video_id(url)
    assert message in str(info.value)


def test_url_fetcher_hands_videos_to_youtube_and_never_reads_the_page(key: str) -> None:
    page_requests: list[str] = []

    def page(request: httpx2.Request) -> httpx2.Response:
        page_requests.append(str(request.url))
        return httpx2.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    youtube, api_requests, lister = make()
    fetcher = UrlFetcher(
        httpx2.Client(transport=httpx2.MockTransport(page)),
        resolve=lambda host: ["93.184.216.34"],
        youtube=youtube,
    )
    fetched = fetcher.fetch(f" https://youtu.be/{VIDEO_ID}?t=3 ")
    assert (fetched.requested_url, fetched.url) == (f"https://youtu.be/{VIDEO_ID}?t=3", WATCH)
    assert fetched.kind == "web_page" and fetched.media_type == "text/markdown"
    assert len(api_requests) == 1 and lister.calls == [VIDEO_ID]

    with pytest.raises(UrlSourceError, match="playlist, not a video"):
        fetcher.fetch("https://www.youtube.com/playlist?list=PL123")
    assert page_requests == [] and len(api_requests) == 1


# -- transcript choice ----------------------------------------------------------------------


def codes(chosen: FakeTranscript) -> tuple[str, bool]:
    return chosen.language_code, chosen.is_generated


@pytest.mark.parametrize(
    ("available", "audio", "expected"),
    [
        # A person's transcript before YouTube's automatic one.
        ([("en", True), ("en", False)], "en", ("en", False)),
        # English before the video's own language, even an automatic English one.
        ([("de", False), ("en", True)], "de", ("en", True)),
        # The exact code before a regional variant, same kind.
        ([("en-GB", False), ("en", False)], "en", ("en", False)),
        ([("en-GB", False), ("en", True)], "en", ("en-GB", False)),
        # No English: the video's own language, a person's first.
        ([("fr", False), ("de", True), ("de", False)], "de", ("de", False)),
        ([("fr", False), ("de", True)], "de-DE", ("de", True)),
        # The video's language unknown to the API: the automatic track's is the spoken one.
        ([("fr", False), ("de", True)], None, ("de", True)),
        ([("fr", False), ("de", True)], "zxx", ("de", True)),
    ],
)
def test_choose_transcript_order(
    available: list[tuple[str, bool]], audio: str | None, expected: tuple[str, bool]
) -> None:
    transcripts = [FakeTranscript(code, generated) for code, generated in available]
    assert codes(choose_transcript(transcripts, audio, WATCH)) == expected


def test_choose_transcript_never_translates() -> None:
    transcripts = [FakeTranscript("fr"), FakeTranscript("es", generated=True)]
    with pytest.raises(UrlSourceError) as info:
        choose_transcript(transcripts, "de", WATCH)
    message = str(info.value)
    assert "no transcript in English or in the video's own language (de)" in message
    assert "fr, es (automatic)" in message and "does not translate" in message

    with pytest.raises(UrlSourceError, match="the video's own language is not known"):
        choose_transcript([FakeTranscript("fr")], None, WATCH)
    with pytest.raises(UrlSourceError, match="offers no transcript"):
        choose_transcript([], "en", WATCH)


# -- the stored Markdown ------------------------------------------------------------------


def test_transcript_markdown_shape() -> None:
    lines = [
        FetchedTranscriptSnippet("Welcome &amp; hello,", 0.0, 3.0),
        FetchedTranscriptSnippet("this is   the\nstart.", 30.0, 3.0),
        FetchedTranscriptSnippet("   ", 45.0, 1.0),
        FetchedTranscriptSnippet("Next we go on", 61.5, 3.0),
        FetchedTranscriptSnippet("without stopping", 70.0, 3.0),
        FetchedTranscriptSnippet("Here &#39;s the main part", 100.0, 3.0),
        FetchedTranscriptSnippet("still going", 130.0, 3.0),
        FetchedTranscriptSnippet("and a new paragraph.", 191.0, 3.0),
    ]
    text = transcript_markdown(
        title="Attention",
        channel="Ada Lectures",
        video_id=VIDEO_ID,
        snippets=reversed(lines),  # sorted by time
        chapter_list=[Chapter(0, "Intro"), Chapter(100, "Main part"), Chapter(900, "Unreached")],
        transcript_language="en",
        transcript_generated=True,
    )
    assert text == (
        "# Attention\n\n"
        f"Video by Ada Lectures on YouTube: {WATCH} (transcript: en, generated automatically)\n\n"
        "## Intro\n\n"
        f"[0:00]({WATCH}&t=0s) Welcome & hello, this is the start.\n\n"
        f"[1:01]({WATCH}&t=61s) Next we go on without stopping\n\n"
        "## Main part\n\n"
        f"[1:40]({WATCH}&t=100s) Here 's the main part still going\n\n"
        f"[3:11]({WATCH}&t=191s) and a new paragraph.\n"
    )


def test_paragraphs_are_about_a_minute_without_chapters() -> None:
    text = transcript_markdown(
        title="T", channel="C", video_id=VIDEO_ID, snippets=snippets(60, 5.0),
        chapter_list=[], transcript_language="de", transcript_generated=False,
    )  # fmt: skip
    assert "##" not in text and "(transcript: de, written by a person)" in text
    stamps = [line.split("]")[0] for line in text.split("\n\n")[2:]]
    # No sentence ends in these captions: a paragraph ends at 90 seconds.
    assert stamps == ["[0:00", "[1:30", "[3:00", "[4:30"]


def test_timestamps() -> None:
    assert timestamp_link(VIDEO_ID, 754.9) == f"[12:34]({WATCH}&t=754s)"
    assert timestamp_link(VIDEO_ID, 3725) == f"[1:02:05]({WATCH}&t=3725s)"


def test_chapters_follow_youtubes_rule() -> None:
    found = chapters("Intro text\n0:00 Intro\n1:05 - Part one\n(12:30) Part two: more\n1:02:03 End")
    assert found == [
        Chapter(0, "Intro"),
        Chapter(65, "Part one"),
        Chapter(750, "Part two: more"),
        Chapter(3723, "End"),
    ]
    assert chapters("0:10 A\n1:00 B\n2:00 C") == []  # does not start at 0:00
    assert chapters("0:00 A\n1:00 B") == []  # fewer than three
    assert chapters("0:00 A\n2:00 B\n1:00 C") == []  # not in order
    assert chapters("No chapters here.\nSee 1:00 in the talk.") == []


def test_summary_is_the_opening_of_the_description() -> None:
    assert summary(DESCRIPTION) == (
        "How attention works, from the ground up.\n\nSlides: https://example.org/slides"
    )
    assert summary("0:00 Intro\n1:00 B\n2:00 C") is None
    assert summary("  \n ") is None
    long = "This is one sentence. " * 40
    cut = summary(long)
    assert cut is not None and len(cut) <= SUMMARY_CHARS and cut.endswith("sentence.")
    words = summary("word " * 200)
    assert words is not None and len(words) <= SUMMARY_CHARS + 1 and words.endswith("word…")


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT3M20S", 200), ("PT1H2M3S", 3723), ("PT45S", 45), ("P1DT2M", 86520), ("P0D", 0),
     ("PT1H", 3600), ("P1W", 604800)],
)  # fmt: skip
def test_duration_seconds(value: str, seconds: int) -> None:
    assert duration_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "P", "PT", "3:20", "PT3X"])
def test_unreadable_duration_fails_loudly(value: str) -> None:
    with pytest.raises(UrlSourceError, match="duration Teacher cannot read"):
        duration_seconds(value)


# -- a whole fetch --------------------------------------------------------------------------


def test_fetch_returns_an_ordinary_web_page_source(key: str) -> None:
    other = FakeTranscript("en", generated=True)
    chosen = FakeTranscript("en", generated=False)
    fetcher, seen, lister = make(lister=FakeLister(other, chosen))
    fetched = fetcher.fetch(f"https://youtu.be/{VIDEO_ID}", VIDEO_ID)

    assert len(seen) == 1
    request = seen[0]
    assert (request.method, request.url.host, request.url.path) == (
        "GET", "www.googleapis.com", "/youtube/v3/videos",
    )  # fmt: skip
    assert dict(request.url.params) == {"part": "snippet,contentDetails", "id": VIDEO_ID, "key": API_KEY}
    assert lister.calls == [VIDEO_ID] and (chosen.fetched, other.fetched) == (1, 0)

    assert (fetched.requested_url, fetched.url) == (f"https://youtu.be/{VIDEO_ID}", WATCH)
    assert (fetched.kind, fetched.media_type, fetched.suffix) == ("web_page", "text/markdown", ".md")
    assert fetched.words == 200
    details = fetched.details
    assert details is not None
    assert details.title == "Attention & transformers"
    assert details.authors == ("Ada Lectures",)
    assert (details.published, details.year, details.site_name) == ("2024-03-05", 2024, "YouTube")
    assert details.summary == "How attention works, from the ground up.\n\nSlides: https://example.org/slides"
    assert (details.arxiv_id, details.doi) == (None, None)
    assert fetched.extra_metadata == {
        "youtube_video_id": VIDEO_ID,
        "channel": "Ada Lectures",
        "duration_seconds": 200,
        "transcript_language": "en",
        "transcript_generated": False,
    }
    text = fetched.content.decode("utf-8")
    assert text.startswith(
        f"# {details.title}\n\nVideo by Ada Lectures on YouTube: {WATCH} "
        "(transcript: en, written by a person)\n\n## Intro\n\n"
        f"[0:00]({WATCH}&t=0s) word0 a b c d"
    )
    assert "\n\n## Queries and keys\n\n" in text and "\n\n## Values\n\n" in text
    assert f"\n\n[1:00]({WATCH}&t=60s) word12 a b c d" in text
    assert API_KEY not in text


def test_the_key_is_redacted_from_httpx_log_lines(key: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="httpx2")
    fetcher, _, _ = make()
    fetcher.fetch(WATCH, VIDEO_ID)
    assert "HTTP Request: GET https://www.googleapis.com/youtube/v3/videos" in caplog.text
    assert "key=<redacted>" in caplog.text
    assert API_KEY not in caplog.text


# -- failing loudly -----------------------------------------------------------------------


def test_missing_key_says_how_to_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_YOUTUBE_API_KEY, raising=False)
    fetcher, seen, lister = make()
    message = fail(fetcher)
    assert "$YOUTUBE_API_KEY is not set" in message
    assert "YOUTUBE_API_KEY=<your key> to .env" in message and "restart the server" in message
    assert seen == [] and lister.calls == []


def test_blank_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_YOUTUBE_API_KEY, "  ")
    fetcher, seen, _ = make()
    assert "$YOUTUBE_API_KEY is set but empty" in fail(fetcher)
    assert seen == []


def google_error(status: int, reason: str, message: str, info_reason: str | None = None) -> Handler:
    error: dict[str, Any] = {
        "code": status,
        "message": message,
        "errors": [{"message": message, "domain": "global", "reason": reason}],
    }
    if info_reason:
        error["details"] = [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": info_reason}
        ]
    return api(status, {"error": error})


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (
            google_error(400, "badRequest", "API key not valid. Please pass a valid API key.",
                         "API_KEY_INVALID"),
            ["rejected $YOUTUBE_API_KEY", "HTTP 400, badRequest, API_KEY_INVALID: API key not valid",
             "restart the server"],
        ),
        (
            google_error(403, "quotaExceeded", "The request cannot be completed because you have "
                         "exceeded your quota."),
            ["quota of $YOUTUBE_API_KEY is used up", "HTTP 403, quotaExceeded", "midnight Pacific"],
        ),
        (
            google_error(403, "accessNotConfigured", "YouTube Data API v3 has not been used in "
                         "project 123 before or it is disabled.", "SERVICE_DISABLED"),
            ["is not enabled", "accessNotConfigured, SERVICE_DISABLED"],
        ),
        (
            google_error(403, "forbidden", "Requests from this referer are blocked.",
                         "API_KEY_HTTP_REFERRER_BLOCKED"),
            ["rejected $YOUTUBE_API_KEY", "API_KEY_HTTP_REFERRER_BLOCKED"],
        ),
        (
            google_error(403, "somethingElse", "Nope."),
            ["refused the request (HTTP 403, somethingElse: Nope.)"],
        ),
        (
            api(503, {"error": {"code": 503, "message": "Backend Error", "errors": [{"reason": "backendError"}]}}),
            ["answered with an error (HTTP 503, backendError: Backend Error)"],
        ),
        (
            lambda request: httpx2.Response(502, text="<html>Bad gateway</html>"),
            ["answered with an error (HTTP 502, no reason given: <html>Bad gateway</html>)"],
        ),
        (
            google_error(400, "keyInvalid", f"The key {API_KEY} is not valid."),
            ["rejected $YOUTUBE_API_KEY", "The key <redacted> is not valid."],
        ),
    ],
)  # fmt: skip
def test_data_api_errors_fail_loudly_with_googles_reason(
    key: str, handler: Handler, expected: list[str]
) -> None:
    fetcher, _, lister = make(handler)
    message = fail(fetcher)
    for fragment in expected:
        assert fragment in message
    assert lister.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx2.ConnectError(f"cannot connect to {WATCH}&key={API_KEY}"), "could not be reached (ConnectError"),
        (httpx2.ReadTimeout(f"timed out: key={API_KEY}"), "did not answer within 30 seconds"),
    ],
)  # fmt: skip
def test_unreachable_api_does_not_chain_the_request(key: str, error: Exception, expected: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    fetcher, _, _ = make(handler)
    with pytest.raises(UrlSourceError) as info:
        fetcher.fetch(WATCH, VIDEO_ID)
    assert expected in str(info.value) and API_KEY not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_unknown_or_private_video(key: str) -> None:
    fetcher, _, lister = make(api(body={"kind": "youtube#videoListResponse", "items": []}))
    message = fail(fetcher)
    assert "has no video 'dQw4w9WgXcQ'" in message and "private" in message
    assert lister.calls == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"nothing": 1}, "lists no items"),
        ({"items": [{"id": VIDEO_ID}]}, "no snippet or contentDetails"),
        ({"items": [{**video(), "snippet": {**video()["snippet"], "title": ""}}]}, "has no title"),
    ],
)
def test_unexpected_api_answers_fail_loudly(key: str, body: Any, expected: str) -> None:
    fetcher, _, _ = make(api(body=body))
    assert expected in fail(fetcher)


@pytest.mark.parametrize(("state", "expected"), [("live", "is still running"), ("upcoming", "has not started yet")])
def test_live_broadcasts_fail_loudly(key: str, state: str, expected: str) -> None:
    resource = video(liveBroadcastContent=state)
    resource["contentDetails"]["duration"] = "P0D"
    fetcher, _, lister = make(api(resource=resource))
    message = fail(fetcher)
    assert "live broadcast" in message and expected in message
    assert lister.calls == []  # no transcript is asked for


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (yta.TranscriptsDisabled(VIDEO_ID), "captions are turned off or there are none"),
        (yta.RequestBlocked(VIDEO_ID), "YouTube is blocking transcript requests"),
        (yta.IpBlocked(VIDEO_ID), "YouTube is blocking transcript requests"),
        (yta.VideoUnavailable(VIDEO_ID), "is unavailable"),
        (yta.AgeRestricted(VIDEO_ID), "is age-restricted"),
        (yta.VideoUnplayable(VIDEO_ID, "Video unavailable", ["Not in your country"]),
         "cannot be played: Video unavailable (Not in your country)"),
        (yta.YouTubeDataUnparsable(VIDEO_ID), "youtube-transcript-api 1."),
        (requests.ConnectionError("no route"), "could not be reached for the transcript"),
    ],
)  # fmt: skip
def test_transcript_list_errors_fail_loudly(key: str, error: Exception, expected: str) -> None:
    fetcher, _, _ = make(lister=FakeLister(error=error))
    assert expected in fail(fetcher)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (yta.PoTokenRequired(VIDEO_ID), "PoTokenRequired"),
        (yta.IpBlocked(VIDEO_ID), "YouTube is blocking transcript requests"),
        (yta.YouTubeRequestFailed(VIDEO_ID, requests.HTTPError("429 Client Error: Too Many Requests "
                                                               "for url: https://www.youtube.com/api")),
         "failed (429 Client Error: Too Many Requests)"),
    ],
)  # fmt: skip
def test_transcript_fetch_errors_fail_loudly(key: str, error: Exception, expected: str) -> None:
    fetcher, _, _ = make(lister=FakeLister(FakeTranscript("en", error=error)))
    message = fail(fetcher)
    assert expected in message and "for url" not in message


def test_no_transcript_at_all(key: str) -> None:
    fetcher, _, _ = make(lister=FakeLister())
    assert "offers no transcript" in fail(fetcher)


def test_no_transcript_in_a_usable_language(key: str) -> None:
    fetcher, _, _ = make(api(resource=video(defaultAudioLanguage="de")), FakeLister(FakeTranscript("fr")))
    assert "does not translate" in fail(fetcher)


def test_short_transcript_fails_loudly(key: str) -> None:
    short = snippets(count=(MIN_WORDS - 1) // 5)  # five words each
    fetcher, _, _ = make(lister=FakeLister(FakeTranscript("en", lines=short)))
    message = fail(fetcher)
    assert f"too little text ({len(short) * 5} words; at least {MIN_WORDS}" in message
