"""The probe: fetch each candidate as Teacher would add it, store nothing, and say whether it is
readable.

`probe(fetcher, url)` fetches with the URL source code (`teacher.url_sources.UrlFetcher.fetch`,
or any object with the same `fetch(url)`), so a candidate is judged on exactly what adding it
would store: a web page's main content as Markdown, a PDF as it is, a YouTube video's
transcript. Nothing is written to disk or to the database.

The verdict (`Probe.verdict`), with a reason in words (`Probe.reason`):

- `readable`: at least `MIN_TEACHING_WORDS` words of text;
- `too_thin`: fewer (the fetcher already refuses fewer than `url_sources.MIN_WORDS`);
- `no_text`: a PDF from which no text could be read (perhaps a scan): Teacher could still send
  it to the model, but the probe cannot judge it;
- `unreadable_pdf`: a PDF that pypdf cannot open;
- `timeout`: the fetch did not finish within its time (also when the whole probe's
  `deadline` passed before it finished);
- otherwise the fetcher's own reason (`url_sources.URL_ERROR_REASONS`): `paywalled` (HTTP
  401/402/403), `fetch_failed`, `http_error`, `too_large`, `unsupported_type`, `blocked`, ...

Only `readable` candidates go to triage. A PDF's text is read from at most `PDF_PAGES` pages;
when it has more, its word count is scaled up from those pages and marked `words_estimated`.
`opening` is the first `OPENING_CHARS` characters of the text, what triage reads (it is not part
of the result's JSON).

`probe_all(fetcher, urls, workers=, deadline=)` probes in parallel, at most `workers` at a
time, and returns one `Probe` per address in order; a fetch still running at the deadline is
reported as `timeout` and not waited for. An exception that is not a `UrlSourceError` is a bug
and is raised.
"""

from __future__ import annotations

import io
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from teacher.url_sources import (
    KIND_PAPER,
    REASON_TIMEOUT,
    UrlSourceError,
    word_count,
)

MIN_TEACHING_WORDS = 400
PDF_PAGES = 30
OPENING_CHARS = 3000
PROBE_WORKERS = 4
PROBE_DEADLINE = 90.0  # seconds for all the fetches together
PROBE_FETCH_TIMEOUT = 20.0  # seconds per request, for the fetcher the finder builds

READABLE = "readable"
TOO_THIN = "too_thin"
NO_TEXT = "no_text"
UNREADABLE_PDF = "unreadable_pdf"
TIMEOUT = REASON_TIMEOUT

_REASONS = {
    "invalid_url": "not an address Teacher can fetch",
    "blocked": "a private or local address, which Teacher does not fetch",
    "fetch_failed": "could not be fetched",
    "timeout": "did not answer in time",
    "paywalled": "needs a login or a subscription",
    "http_error": "the server answered with an error",
    "too_large": "larger than Teacher accepts",
    "empty": "an empty document",
    "unsupported_type": "a kind of file Teacher cannot read",
    "undecodable": "text Teacher cannot decode",
    "too_thin": "too little readable text",
    "unreadable": "could not be read",
}


@dataclass(frozen=True)
class Probe:
    """What fetching a candidate showed (see the module docstring). `final_url` is the address
    after redirects, `media_type` what the server sent, `kind` what Teacher would store it as
    ("paper" or "web_page"); each is None when the fetch failed."""

    verdict: str
    reason: str
    final_url: str | None = None
    media_type: str | None = None
    kind: str | None = None
    words: int | None = None
    words_estimated: bool = False
    pages: int | None = None
    page_title: str | None = None
    opening: str | None = None
    seconds: float | None = None

    @property
    def readable(self) -> bool:
        return self.verdict == READABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "readable": self.readable,
            "reason": self.reason,
            "final_url": self.final_url,
            "media_type": self.media_type,
            "kind": self.kind,
            "words": self.words,
            "words_estimated": self.words_estimated,
            "pages": self.pages,
            "page_title": self.page_title,
            "seconds": round(self.seconds, 2) if self.seconds is not None else None,
        }


def _pdf_text(content: bytes) -> tuple[str, int, bool, int]:
    """(text of the first PDF_PAGES pages, words, whether scaled up, page count)."""
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted and not reader.decrypt(""):
        raise PyPdfError("the PDF is password-protected")
    pages = list(reader.pages)
    read = pages[:PDF_PAGES]
    text = "\n".join(page.extract_text() or "" for page in read)
    words = word_count(text)
    estimated = len(pages) > len(read) and words > 0
    if estimated:
        words = round(words * len(pages) / len(read))
    return text, words, estimated, len(pages)


def probe(fetcher: Any, url: str) -> Probe:
    """Fetch `url` and judge it (see the module docstring)."""
    started = time.monotonic()
    try:
        fetched = fetcher.fetch(url)
    except UrlSourceError as exc:
        label = _REASONS.get(exc.reason, "could not be read")
        return Probe(exc.reason, f"{label}: {exc}", seconds=time.monotonic() - started)
    common = {
        "final_url": fetched.url,
        "media_type": fetched.original_media_type,
        "kind": fetched.kind,
        "page_title": fetched.details.title if fetched.details is not None else None,
    }
    if fetched.kind == KIND_PAPER:
        try:
            text, words, estimated, pages = _pdf_text(fetched.content)
        except (PyPdfError, DependencyError, ValueError, KeyError, TypeError, IndexError) as exc:
            return Probe(
                UNREADABLE_PDF,
                f"the PDF could not be opened ({exc.__class__.__name__}: {exc})",
                seconds=time.monotonic() - started,
                **common,
            )
        common.update(words=words, words_estimated=estimated, pages=pages)
        if words == 0:
            return Probe(
                NO_TEXT,
                f"no text could be read from the PDF's first {min(pages, PDF_PAGES)} page(s); "
                "it may be a scan",
                seconds=time.monotonic() - started,
                **common,
            )
    else:
        text = fetched.content.decode("utf-8")
        words = fetched.words if fetched.words is not None else word_count(text)
        common.update(words=words)
    seconds = time.monotonic() - started
    if words < MIN_TEACHING_WORDS:
        return Probe(
            TOO_THIN,
            f"only {words} words of text (at least {MIN_TEACHING_WORDS} are needed to teach from)",
            seconds=seconds,
            **common,
        )
    if fetched.kind == KIND_PAPER:
        size = f"{'about ' if common['words_estimated'] else ''}{words} words, a PDF of {common['pages']} page(s)"
    else:
        size = f"{words} words of text"
    return Probe(
        READABLE,
        f"readable: {size}",
        opening=" ".join(text.split())[:OPENING_CHARS],
        seconds=seconds,
        **common,
    )


def probe_all(
    fetcher: Any,
    urls: Sequence[str],
    *,
    workers: int = PROBE_WORKERS,
    deadline: float = PROBE_DEADLINE,
) -> list[Probe]:
    """Probe `urls` in parallel (see the module docstring); one Probe per address, in order."""
    if not urls:
        return []
    results: dict[int, Probe] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls))), thread_name_prefix="probe")
    try:
        futures: dict[Future[Probe], int] = {
            pool.submit(probe, fetcher, url): index for index, url in enumerate(urls)
        }
        end = time.monotonic() + deadline
        pending = set(futures)
        while pending:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                results[futures[future]] = future.result()  # a bug in probe() is raised here
        for future in pending:
            future.cancel()
            results[futures[future]] = Probe(
                TIMEOUT, f"did not answer in time: the probe stopped waiting after {deadline:g} seconds"
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [results[index] for index in range(len(urls))]


__all__ = [
    "MIN_TEACHING_WORDS",
    "NO_TEXT",
    "OPENING_CHARS",
    "PDF_PAGES",
    "PROBE_DEADLINE",
    "PROBE_FETCH_TIMEOUT",
    "PROBE_WORKERS",
    "READABLE",
    "TIMEOUT",
    "TOO_THIN",
    "UNREADABLE_PDF",
    "Probe",
    "probe",
    "probe_all",
]
