"""teacher.url_sources: HTML to Markdown, page details, and the rules of fetching a URL."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from teacher.url_sources import (
    MAX_REDIRECTS,
    MAX_TEXT_BYTES,
    MIN_WORDS,
    UrlFetcher,
    UrlSourceError,
    html_to_markdown,
    main_content,
    page_details,
    parse_html,
)

PUBLIC_IP = "93.184.216.34"
WORDS = " ".join(f"word{n}" for n in range(MIN_WORDS))


def page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def markdown(body: str, head: str = "") -> str:
    return html_to_markdown(parse_html(page(body, head)))


# -- conversion -----------------------------------------------------------------------------


def test_markdown_keeps_structure() -> None:
    text = markdown(
        "<main><h1>Title</h1><p>One <b>bold</b> and <em>soft</em> &amp; <a href='/x'>a link</a>."
        "<h2>Part</h2><ul><li>first<li>second <code>x = 1</code></ul>"
        "<ol><li>a</li><li>b</li></ol>"
        "<pre>def f():\n    return 1</pre>"
        "<blockquote><p>Quoted</p></blockquote>"
        "<table><caption>Scores</caption><tr><th>Model<th>BLEU<tr><td>Base<td>27.3</table>"
        "<p>Line one<br>line two</p><img src='a.png' alt='A diagram'></main>"
    )
    assert text == (
        "# Title\n\nOne **bold** and *soft* & a link.\n\n## Part\n\n"
        "- first\n- second `x = 1`\n\n1. a\n2. b\n\n"
        "```\ndef f():\n    return 1\n```\n\n> Quoted\n\n"
        "Scores\n\n| Model | BLEU |\n| --- | --- |\n| Base | 27.3 |\n\n"
        "Line one\nline two\n\n[image: A diagram]"
    )


def test_markdown_drops_what_is_not_content() -> None:
    text = markdown(
        "<header>SITE-NAME<nav>MENU</nav></header>"
        "<article><header><h1>Post</h1></header><p>Body text.</p>"
        "<script>var SCRIPT = 1</script><style>.STYLE{}</style><aside>SIDEBAR</aside>"
        "<form>FORM<button>BUTTON</button></form><p hidden>HIDDEN</p>"
        "<span aria-hidden='true'>ARIA</span></article><footer>FOOTER</footer>",
        "<title>HEAD-TITLE</title>",
    )
    assert text == "# Post\n\nBody text."


def test_markdown_takes_latex_from_mathml() -> None:
    math = (
        "<math{attrs}><semantics><mi>x</mi>"
        "<annotation encoding='application/x-tex'>x^2</annotation></semantics></math>"
    )
    text = markdown(
        f"<p>Inline {math.format(attrs='')} here.</p>{math.format(attrs=' display=\"block\"')}"
        "<p><math alttext='a+b'><mi>a</mi></math> and <math><mi>y</mi></math></p>"
    )
    assert text == "Inline $x^2$ here.\n\n$$x^2$$\n\n$a+b$ and y"


def test_main_content_prefers_main_then_a_dominant_article() -> None:
    root = parse_html(page("<p>Outside</p><main><p>Inside</p></main>"))
    assert main_content(root).tag == "main"
    root = parse_html(page(f"<p>short</p><article><p>{WORDS}</p></article>"))
    assert main_content(root).tag == "article"
    # A list of teasers: no article holds half of the page, so the body is the content.
    teasers = "".join(f"<article><p>teaser {n} text</p></article>" for n in range(4))
    assert main_content(parse_html(page(teasers))).tag == "body"


def test_page_details() -> None:
    root = parse_html(
        page(
            "<main><h1>Heading</h1><time datetime='2020-01-02'>then</time></main>",
            "<title>Tab title</title>"
            "<meta property='og:title' content='  The   Title '>"
            "<meta name='citation_author' content='Ada L'>"
            "<meta name='citation_author' content='Bob M'>"
            "<meta name='author' content='Ignored'>"
            "<meta property='article:published_time' content='2024-03-05T10:00:00Z'>"
            "<meta name='description' content='What it is about.'>"
            "<meta property='og:site_name' content='The Site'>"
            "<meta name='citation_arxiv_id' content='arXiv:1706.03762v7'>"
            "<meta name='citation_doi' content='https://doi.org/10.1145/3295222'>",
        )
    )
    details = page_details(root)
    assert details.title == "The Title"
    assert details.authors == ("Ada L", "Bob M")
    assert (details.published, details.year) == ("2024-03-05", 2024)
    assert details.summary == "What it is about."
    assert details.site_name == "The Site"
    assert (details.arxiv_id, details.doi) == ("1706.03762", "10.1145/3295222")


def test_page_details_fallbacks_and_missing() -> None:
    details = page_details(
        parse_html(
            page(
                "<h1>Heading</h1><time datetime='2020-01'>x</time>",
                "<title>Tab</title><meta property='article:author' content='https://x.test/ada'>"
                "<meta name='date' content='not a date'>",
            )
        )
    )
    assert (details.title, details.authors, details.published) == ("Heading", (), "2020-01")
    empty = page_details(parse_html(page("<p>text</p>")))
    assert (empty.title, empty.summary, empty.year) == (None, None, None)
    assert page_details(parse_html(page("<p>x</p>", "<title> Tab </title>"))).title == "Tab"


# -- fetching -------------------------------------------------------------------------------


Handler = Callable[[httpx2.Request], httpx2.Response]


def fetcher(handler: Handler, hosts: dict[str, list[str]] | None = None) -> UrlFetcher:
    """A fetcher on a mock transport; every host resolves to a public address unless `hosts`
    says otherwise (an empty list: the host does not resolve)."""

    def resolve(host: str) -> list[str]:
        addresses = (hosts or {}).get(host, [PUBLIC_IP])
        if not addresses:
            raise OSError("no such host")
        return addresses

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return UrlFetcher(client, resolve=resolve)


def html_response(body: str, head: str = "", **kwargs: Any) -> httpx2.Response:
    return httpx2.Response(
        200, content=page(body, head).encode(), headers={"content-type": "text/html; charset=utf-8"}, **kwargs
    )


def test_fetch_html_page() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return html_response(f"<nav>MENU</nav><main><h1>Post</h1><p>{WORDS}</p></main>", "<title>T</title>")

    fetched = fetcher(handler).fetch("  https://example.test/post  ")
    assert (fetched.requested_url, fetched.url) == ("https://example.test/post",) * 2
    assert (fetched.kind, fetched.media_type, fetched.suffix) == ("web_page", "text/markdown", ".md")
    assert fetched.original_media_type == "text/html"
    assert fetched.content.decode() == f"# Post\n\n{WORDS}\n"
    assert fetched.words == MIN_WORDS + 2
    assert fetched.details is not None and fetched.details.title == "Post"
    assert seen[0].headers["user-agent"].startswith("Teacher/")


def test_fetch_pdf_by_type_or_signature(make_pdf) -> None:
    pdf = make_pdf(2)
    for content_type in ("application/pdf", "application/octet-stream"):
        fetched = fetcher(
            lambda request: httpx2.Response(200, content=pdf, headers={"content-type": content_type})
        ).fetch("https://example.test/paper")
        assert (fetched.kind, fetched.media_type, fetched.suffix) == ("paper", "application/pdf", ".pdf")
        assert fetched.content == pdf and fetched.details is None and fetched.words is None


def test_fetch_plain_text_and_markdown() -> None:
    for content_type, suffix, stored in (
        ("text/plain; charset=utf-8", ".txt", "text/plain"),
        ("text/markdown", ".md", "text/markdown"),
    ):
        fetched = fetcher(
            lambda request: httpx2.Response(200, content=WORDS.encode(), headers={"content-type": content_type})
        ).fetch("https://example.test/notes")
        assert (fetched.kind, fetched.media_type, fetched.suffix) == ("web_page", stored, suffix)
        assert fetched.content == WORDS.encode()


def test_fetch_follows_redirects_and_checks_every_hop() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/old":
            return httpx2.Response(302, headers={"location": "/new"})
        return html_response(f"<p>{WORDS}</p>")

    fetched = fetcher(handler).fetch("https://example.test/old")
    assert (fetched.requested_url, fetched.url) == ("https://example.test/old", "https://example.test/new")

    def to_private(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(301, headers={"location": "http://intranet.test/secret"})

    with pytest.raises(UrlSourceError, match="private or local address"):
        fetcher(to_private, {"intranet.test": ["10.0.0.5"]}).fetch("https://example.test/")

    def loop(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "/again"})

    with pytest.raises(UrlSourceError, match=f"more than {MAX_REDIRECTS} times"):
        fetcher(loop).fetch("https://example.test/")
    with pytest.raises(UrlSourceError, match="redirect .* to nowhere"):
        fetcher(lambda request: httpx2.Response(302)).fetch("https://example.test/")


@pytest.mark.parametrize(
    ("url", "hosts", "message"),
    [
        ("ftp://example.test/x", None, "must start with http"),
        ("example.test/x", None, "must start with http"),
        ("file:///etc/passwd", None, "must start with http"),
        ("https:///path", None, "no host name"),
        ("https://user:pw@example.test/", None, "user name or password"),
        ("https://gone.test/", {"gone.test": []}, "could not be found"),
        ("http://localhost:8000/", {"localhost": ["127.0.0.1"]}, "private or local"),
        ("http://router.test/", {"router.test": ["192.168.1.1"]}, "private or local"),
        ("http://mixed.test/", {"mixed.test": [PUBLIC_IP, "::1"]}, "private or local"),
        ("http://meta.test/", {"meta.test": ["169.254.169.254"]}, "private or local"),
    ],
)
def test_fetch_refuses_urls(url: str, hosts: dict[str, list[str]] | None, message: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request may be made")

    with pytest.raises(UrlSourceError, match=message):
        fetcher(handler, hosts).fetch(url)


def respond(status: int = 200, content: bytes = b"", content_type: str | None = "text/html") -> Handler:
    headers = {"content-type": content_type} if content_type else {}
    return lambda request: httpx2.Response(status, content=content, headers=headers)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (respond(404), "HTTP 404"),
        (respond(403), r"HTTP 403\. The page may need a login"),
        (respond(500), "HTTP 500"),
        (respond(200, b""), "empty document"),
        (respond(200, page("<p>Just a few words.</p>").encode()), "too little readable text"),
        (respond(200, page("<div id='app'></div><script>render()</script>").encode()), "JavaScript or a login"),
        (respond(200, b"short", "text/plain"), "too little text"),
        (respond(200, b"\x89PNG....", "image/png"), "image/png, which Teacher cannot read"),
        (respond(200, b"data", None), "undeclared type"),
        (respond(200, b"<html>not a pdf</html>", "application/pdf"), "says it is a PDF"),
        (respond(200, b"x" * (MAX_TEXT_BYTES + 1), "text/html"), "larger than Teacher accepts"),
        (respond(200, (WORDS + " ").encode() + b"\xff" * 400, "text/plain"), "could not be decoded"),
        (respond(200, WORDS.encode(), "text/plain; charset=klingon"), "encoding 'klingon'"),
    ],
)
def test_fetch_fails_loudly(handler: Handler, message: str) -> None:
    with pytest.raises(UrlSourceError, match=message):
        fetcher(handler).fetch("https://example.test/x")


def test_fetch_reports_network_errors() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(UrlSourceError, match="did not answer within"):
        fetcher(timeout).fetch("https://example.test/")
    with pytest.raises(UrlSourceError, match="could not be fetched .ConnectError"):
        fetcher(refused).fetch("https://example.test/")


def test_fetch_decodes_declared_charsets() -> None:
    body = page(f"<p>café {WORDS}</p>", "<meta charset='iso-8859-1'>").encode("iso-8859-1")
    meta = fetcher(respond(200, body, "text/html")).fetch("https://example.test/")
    assert meta.content.decode().startswith("café ")
    header = fetcher(
        respond(200, f"café {WORDS}".encode("iso-8859-1"), "text/plain; charset=ISO-8859-1")
    ).fetch("https://example.test/")
    assert header.content.decode().startswith("café ")


def test_markdown_drops_page_furniture_and_source_line_breaks() -> None:
    text = markdown(
        "<div role='navigation'>ROLE-NAV</div><div class='navbox wide'>NAVBOX</div>"
        "<h2>Part<a class='headerlink' href='#p'>¶</a><span class='mw-editsection'>[edit]</span></h2>"
        "<p>A sentence that the\n    source wraps<br>and one real break.</p>"
        "<table><tr><td><div>Machine learning</div><div>and</div><div>data mining</div></td></tr></table>"
    )
    assert text == (
        "## Part\n\nA sentence that the source wraps\nand one real break.\n\n"
        "| Machine learning and data mining |\n| --- |"
    )
