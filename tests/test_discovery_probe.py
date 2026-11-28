"""teacher.discovery.probe: the verdict for each kind of page, PDFs (with text, scanned, too
long to read whole, broken), the fetcher's own failures, and the parallel probe's bounds. The
fetcher is a fake or a `UrlFetcher` on a MockTransport: nothing reaches the network."""

from __future__ import annotations

import io
import time

import httpx2
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from teacher.discovery.probe import (
    MIN_TEACHING_WORDS,
    OPENING_CHARS,
    PDF_PAGES,
    probe,
    probe_all,
)
from teacher.url_sources import KIND_PAPER, PDF_TYPE, FetchedSource, UrlFetcher, UrlSourceError
from tests.discovery_fakes import FakeFetcher, web_page

PUBLIC_IP = "93.184.216.34"


def text_pdf(pages: int, words_per_page: int) -> bytes:
    """A real PDF whose every page carries `words_per_page` words of extractable text."""
    writer = PdfWriter()
    for number in range(pages):
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })  # fmt: skip
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        words = b" ".join(b"p%dw%d" % (number, i) for i in range(words_per_page))
        stream.set_data(b"BT /F1 6 Tf 20 700 Td (" + words + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_source(url: str, content: bytes) -> FetchedSource:
    return FetchedSource(url, url, KIND_PAPER, PDF_TYPE, ".pdf", content, PDF_TYPE)


def test_a_long_page_is_readable_with_its_opening() -> None:
    url = "https://example.org/notes"
    result = probe(FakeFetcher({url: web_page(url, 900, title="Notes")}), url)
    assert result.verdict == "readable" and result.readable
    assert result.reason == "readable: 900 words of text"
    assert (result.words, result.kind, result.media_type, result.page_title) == (
        900, "web_page", "text/html", "Notes",
    )  # fmt: skip
    assert result.opening is not None and len(result.opening) == OPENING_CHARS
    assert "opening" not in result.to_dict()  # the text itself is not part of the JSON


def test_a_short_page_is_too_thin() -> None:
    url = "https://example.org/stub"
    result = probe(FakeFetcher({url: web_page(url, MIN_TEACHING_WORDS - 1)}), url)
    assert result.verdict == "too_thin" and not result.readable
    assert f"only {MIN_TEACHING_WORDS - 1} words" in result.reason
    assert result.opening is None


def test_a_pdf_with_text_is_readable() -> None:
    url = "https://example.org/paper.pdf"
    result = probe(FakeFetcher({url: pdf_source(url, text_pdf(3, 200))}), url)
    assert result.verdict == "readable"
    assert (result.words, result.pages, result.words_estimated, result.kind) == (600, 3, False, "paper")
    assert result.reason == "readable: 600 words, a PDF of 3 page(s)"
    assert result.opening.startswith("p0w0 p0w1")


def test_a_long_pdf_is_read_from_its_first_pages_and_estimated() -> None:
    url = "https://example.org/book.pdf"
    result = probe(FakeFetcher({url: pdf_source(url, text_pdf(PDF_PAGES + 10, 20))}), url)
    assert result.words_estimated is True
    assert result.words == round(20 * PDF_PAGES * (PDF_PAGES + 10) / PDF_PAGES)
    assert result.reason.startswith("readable: about ")


def test_a_pdf_without_text_and_a_broken_pdf(make_pdf) -> None:  # noqa: ANN001
    scan, broken = "https://example.org/scan.pdf", "https://example.org/broken.pdf"
    fetcher = FakeFetcher({
        scan: pdf_source(scan, make_pdf(2)),
        broken: pdf_source(broken, b"%PDF-1.7\n" + b"\x8f\x00junk" * 50),
    })  # fmt: skip
    assert probe(fetcher, scan).verdict == "no_text"
    assert "may be a scan" in probe(fetcher, scan).reason
    assert probe(fetcher, broken).verdict == "unreadable_pdf"


@pytest.mark.parametrize(
    ("reason", "verdict"),
    [("paywalled", "paywalled"), ("fetch_failed", "fetch_failed"), ("unsupported_type", "unsupported_type"),
     ("too_thin", "too_thin"), ("timeout", "timeout")],
)  # fmt: skip
def test_the_fetchers_failure_is_the_verdict(reason: str, verdict: str) -> None:
    url = "https://example.org/x"
    result = probe(FakeFetcher({url: UrlSourceError("boom", reason=reason)}), url)
    assert (result.verdict, result.readable) == (verdict, False)
    assert result.reason.endswith(": boom")


def _fetcher(handler) -> UrlFetcher:  # noqa: ANN001
    return UrlFetcher(
        httpx2.Client(transport=httpx2.MockTransport(handler)), resolve=lambda host: [PUBLIC_IP]
    )


def html(words: int) -> bytes:
    body = " ".join(f"word{n}" for n in range(words))
    return f"<html><head><title>T</title></head><body><main><p>{body}</p></main></body></html>".encode()


@pytest.mark.parametrize(
    ("response", "verdict"),
    [
        (httpx2.Response(403), "paywalled"),
        (httpx2.Response(402), "paywalled"),
        (httpx2.Response(404), "http_error"),
        (httpx2.Response(200, content=b"\x89PNG....", headers={"content-type": "image/png"}), "unsupported_type"),
        (httpx2.Response(200, content=html(20), headers={"content-type": "text/html"}), "too_thin"),
        (httpx2.Response(200, content=html(200), headers={"content-type": "text/html"}), "too_thin"),
        (httpx2.Response(200, content=html(MIN_TEACHING_WORDS + 50), headers={"content-type": "text/html"}),
         "readable"),
    ],
)  # fmt: skip
def test_verdicts_with_the_real_url_fetcher(response: httpx2.Response, verdict: str) -> None:
    result = probe(_fetcher(lambda request: response), "https://example.org/page")
    assert result.verdict == verdict


def test_the_url_fetchers_errors_name_their_reason() -> None:
    fetcher = _fetcher(lambda request: httpx2.Response(200))
    with pytest.raises(UrlSourceError) as info:
        fetcher.check_url("ftp://example.org/x")
    assert info.value.reason == "invalid_url"
    private = UrlFetcher(resolve=lambda host: ["10.0.0.1"])
    with pytest.raises(UrlSourceError) as info:
        private.check_url("https://intranet.example/x")
    assert info.value.reason == "blocked"
    with pytest.raises(ValueError, match="unknown UrlSourceError reason"):
        UrlSourceError("x", reason="nonsense")


def test_probe_all_keeps_order_and_bounds_concurrency() -> None:
    urls = [f"https://example.org/{n}" for n in range(6)]
    fetcher = FakeFetcher({u: web_page(u, 500 + n) for n, u in enumerate(urls)}, delay=0.05)
    results = probe_all(fetcher, urls, workers=2, deadline=10)
    assert [r.words for r in results] == [500 + n for n in range(6)]
    assert fetcher.max_active <= 2
    assert probe_all(fetcher, []) == []


def test_probe_all_reports_a_fetch_still_running_at_the_deadline_as_a_timeout() -> None:
    fast, slow = "https://example.org/fast", "https://example.org/slow"

    class Mixed(FakeFetcher):
        def fetch(self, url: str) -> FetchedSource:
            if url == slow:
                time.sleep(1.0)
            return super().fetch(url)

    started = time.monotonic()
    results = probe_all(Mixed({fast: web_page(fast), slow: web_page(slow)}), [slow, fast], deadline=0.3)
    assert time.monotonic() - started < 0.9  # did not wait for the slow one
    assert [r.verdict for r in results] == ["timeout", "readable"]
    assert "stopped waiting after 0.3 seconds" in results[0].reason


def test_a_bug_in_the_fetcher_is_raised_not_hidden() -> None:
    class Broken:
        def fetch(self, url: str) -> FetchedSource:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        probe_all(Broken(), ["https://example.org/x"])
