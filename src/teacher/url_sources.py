"""Knowledge sources from a URL: fetch a web address and turn it into a file Teacher can teach.

`UrlFetcher.fetch(url)` returns a `FetchedSource`, the bytes to store and what is known about
them:

- a PDF (by content type, or by its `%PDF` signature) is kept as it is (`kind` "paper");
- an HTML page is converted to Markdown by `html_to_markdown` (`kind` "web_page"): the main
  content (`<main>`, else the dominant `<article>`, else `<body>`) without scripts, styles,
  navigation, footers, sidebars and forms; headings, lists, tables, code, quotes and
  mathematics (the LaTeX annotation of MathML, where a page carries one) are kept. Images are
  not: only their alt text is. Its `details` (title, authors, date, summary, site name, arXiv
  id, DOI) are read from the page's own `<meta>` tags by `page_details`, without a model call;
- plain text and Markdown are kept as they are (`kind` "web_page");
- a YouTube video address is not fetched here: `teacher.youtube_sources` recognises it (any
  other address on a YouTube host, such as a playlist or a channel, is an error, never a page
  to read) and `YouTubeFetcher` returns the video's transcript as Markdown (`kind` "web_page",
  `details` from the YouTube Data API, `extra_metadata` naming the video and the transcript).

The stored file is a snapshot: generation and the tutor read it, never the live page.

Fail loudly: everything that keeps a URL from becoming a usable source raises `UrlSourceError`
(a `ValueError`, HTTP 422) with the reason: a URL that is not http(s), carries credentials or
points at a private or local address (also after a redirect); DNS, connection and timeout
errors; a status other than 200; too many redirects; a body over the size limit; a content type
Teacher cannot read; text that cannot be decoded; and a page with fewer than `MIN_WORDS` words
of readable text (typically one that needs JavaScript or a login). Nothing is truncated and
there is no fallback to a partial page. A YouTube video's own failures (no API key, no
transcript, ...) are listed in `teacher.youtube_sources`.

No model is called here and nothing is stored; `teacher.web.routers.sources` does both.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

USER_AGENT = "Teacher/0.1 (self-study reader)"
TIMEOUT = 30.0  # seconds, per request
MAX_REDIRECTS = 5
MAX_PDF_BYTES = 24_000_000  # a PDF is sent to the model as base64 within a 32 MB request
MAX_TEXT_BYTES = 5_000_000  # an HTML page or text file
MIN_WORDS = 150  # fewer readable words than this is not a page worth teaching
MAX_BAD_CHARS = 0.001  # share of undecodable characters tolerated in a page

KIND_PAPER = "paper"
KIND_WEB_PAGE = "web_page"
PDF_TYPE = "application/pdf"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = {"text/plain": ".txt", "text/markdown": ".md", "text/x-markdown": ".md"}


#: Why a URL could not be used (`UrlSourceError.reason`): what source discovery
#: (`teacher.discovery.probe`) reports as a candidate's verdict. Every raise in this module
#: names one; an error raised elsewhere (a YouTube video's, `teacher.youtube_sources`) is
#: `UNREADABLE` unless it says otherwise.
REASON_INVALID_URL = "invalid_url"  # not http(s), no host, credentials in it
REASON_BLOCKED = "blocked"  # a private or local address
REASON_FETCH_FAILED = "fetch_failed"  # DNS, connection, redirects, other HTTP errors
REASON_TIMEOUT = "timeout"
REASON_PAYWALLED = "paywalled"  # HTTP 401, 402 or 403: a login or a paywall
REASON_HTTP_ERROR = "http_error"  # any other status than 200
REASON_TOO_LARGE = "too_large"
REASON_EMPTY = "empty"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_UNDECODABLE = "undecodable"
REASON_TOO_THIN = "too_thin"  # fewer than MIN_WORDS words of readable text
REASON_UNREADABLE = "unreadable"  # anything else
URL_ERROR_REASONS = (
    REASON_INVALID_URL, REASON_BLOCKED, REASON_FETCH_FAILED, REASON_TIMEOUT, REASON_PAYWALLED,
    REASON_HTTP_ERROR, REASON_TOO_LARGE, REASON_EMPTY, REASON_UNSUPPORTED_TYPE,
    REASON_UNDECODABLE, REASON_TOO_THIN, REASON_UNREADABLE,
)  # fmt: skip


class UrlSourceError(ValueError):
    """The URL cannot be used as a knowledge source; the message says why, and `reason` (one of
    `URL_ERROR_REASONS`) what kind of failure it is."""

    def __init__(self, message: str, *, reason: str = REASON_UNREADABLE) -> None:
        if reason not in URL_ERROR_REASONS:
            raise ValueError(f"unknown UrlSourceError reason {reason!r}")
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PageDetails:
    """What an HTML page says about itself. Anything it does not say is None (authors: empty).
    `published` is YYYY, YYYY-MM or YYYY-MM-DD."""

    title: str | None = None
    authors: tuple[str, ...] = ()
    published: str | None = None
    summary: str | None = None
    site_name: str | None = None
    arxiv_id: str | None = None
    doi: str | None = None

    @property
    def year(self) -> int | None:
        return int(self.published[:4]) if self.published else None


@dataclass(frozen=True)
class FetchedSource:
    """A fetched URL, ready to store. `url` is the address after redirects; `media_type` and
    `suffix` describe `content` as stored (a web page is stored as Markdown), and
    `original_media_type` what the server sent. `details` is set for HTML pages and YouTube
    videos only, `words` for everything but PDFs. `extra_metadata` is added to the stored
    source's metadata as it is (a YouTube video's id, channel, duration and transcript)."""

    requested_url: str
    url: str
    kind: str
    media_type: str
    suffix: str
    content: bytes
    original_media_type: str
    details: PageDetails | None = None
    words: int | None = None
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)


# -- HTML: a small tree -------------------------------------------------------------------

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
# Never part of what a page teaches.
SKIP_TAGS = frozenset(
    "script style noscript template svg canvas iframe object embed nav footer aside form "
    "button select dialog menu audio video picture".split()
)
# ARIA landmarks that are the site around the content, not the content.
SKIP_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary", "search"})
# Page furniture that common publishing tools mark only with a class: Wikipedia's edit links,
# navigation boxes and sidebars, and the permalink marks of Sphinx and MkDocs headings.
SKIP_CLASSES = frozenset(
    {"mw-editsection", "navbox", "sidebar", "noprint", "headerlink", "toc", "breadcrumbs"}
)
BREAK = "\x00"  # an explicit line break (<br>) inside inline text, until `_line` renders it
BLOCK_TAGS = frozenset(
    "p div section article main header body html figure figcaption details summary dl dt dd "
    "address center caption".split()
)
HEADINGS = {f"h{level}": level for level in range(1, 7)}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def walk(self) -> Iterable[Node]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        """All text below this node, skipping what a reader never sees."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif not _hidden(child):
                parts.append(child.text())
        return "".join(parts)


# tag -> (open tags it closes, open tags that stop the search): HTML's optional end tags.
_CELL_STOPS = ("table",)
_IMPLIED_END: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "li": (("li",), ("ul", "ol")),
    "dt": (("dt", "dd"), ("dl",)),
    "dd": (("dt", "dd"), ("dl",)),
    "tr": (("tr",), _CELL_STOPS),
    "td": (("td", "th"), ("tr", "table")),
    "th": (("td", "th"), ("tr", "table")),
    **{
        tag: (("p",), ("li", "td", "th", "blockquote", "div", "section", "article", "main", "body"))
        for tag in ("p", "ul", "ol", "pre", "table", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6")
    },
}


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def _close_implied(self, tag: str) -> None:
        """Close the open element that a new `tag` ends without an end tag of its own (an
        `<li>` ends the previous `<li>` of the same list, and so on)."""
        closes, stops = _IMPLIED_END.get(tag, ((), ()))
        for index in range(len(self.stack) - 1, 0, -1):
            open_tag = self.stack[index].tag
            if open_tag in stops:
                return
            if open_tag in closes:
                del self.stack[index:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        node = Node(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # An end tag that closes nothing open is ignored, as browsers do.

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _hidden(node: Node) -> bool:
    return (
        node.tag in SKIP_TAGS
        or node.tag == "head"
        or "hidden" in node.attrs
        or node.attrs.get("aria-hidden") == "true"
        or node.attrs.get("role") in SKIP_ROLES
        or not SKIP_CLASSES.isdisjoint(node.attrs.get("class", "").split())
    )


def _squash(text: str) -> str:
    return " ".join(text.replace(BREAK, " ").split())


def word_count(text: str) -> int:
    return len(text.split())


def main_content(root: Node) -> Node:
    """The node holding the page's content: `<main>` (or `role="main"`), else an `<article>`
    that holds at least half of the body's words, else `<body>`, else the whole document."""
    body = next((n for n in root.walk() if n.tag == "body"), root)
    mains = [n for n in body.walk() if n.tag == "main" or n.attrs.get("role") == "main"]
    if mains:
        return max(mains, key=lambda n: word_count(n.text()))
    articles = [n for n in body.walk() if n.tag == "article"]
    if articles:
        best = max(articles, key=lambda n: word_count(n.text()))
        if word_count(best.text()) * 2 >= word_count(body.text()):
            return best
    return body


# -- HTML to Markdown ---------------------------------------------------------------------


def _math(node: Node) -> str:
    """A `<math>` element as LaTeX where the page gives it (a TeX annotation or `alttext`)."""
    for inner in node.walk():
        if inner.tag == "annotation" and "tex" in inner.attrs.get("encoding", "").lower():
            tex = _squash(inner.text())
            if tex:
                return _wrap_math(tex, node)
    alt = _squash(node.attrs.get("alttext", ""))
    if alt:
        return _wrap_math(alt, node)
    return _squash(
        "".join(
            child if isinstance(child, str) else child.text()
            for child in node.children
            if isinstance(child, str) or child.tag != "annotation"
        )
    )


def _wrap_math(tex: str, node: Node) -> str:
    return f"$${tex}$$" if node.attrs.get("display") == "block" else f"${tex}$"


def _inline(node: Node) -> str:
    """The inline Markdown of `node`'s children (whitespace not yet collapsed)."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
            continue
        if _hidden(child):
            continue
        tag = child.tag
        if tag == "br":
            parts.append(BREAK)
        elif tag == "math":
            parts.append(_math(child))
        elif tag == "img":
            alt = _squash(child.attrs.get("alt", ""))
            if alt:
                parts.append(f"[image: {alt}]")
        elif tag in ("code", "kbd", "samp", "tt"):
            code = _squash(child.text())
            if code:
                parts.append(f"`{code}`")
        elif tag in ("strong", "b"):
            parts.append(_marked("**", _inline(child)))
        elif tag in ("em", "i"):
            parts.append(_marked("*", _inline(child)))
        elif tag == "sup":
            parts.append(_marked("^", _inline(child), close=""))
        elif tag in BLOCK_TAGS or tag in HEADINGS or tag in _FLOW_TAGS:
            # A block inside inline content (a paragraph in a table cell, say): its text is
            # kept, set apart from its neighbours.
            parts.append(f" {_inline(child)} ")
        else:
            parts.append(_inline(child))
    return "".join(parts)


_FLOW_TAGS = frozenset({"ul", "ol", "li", "table", "tr", "td", "th", "pre", "blockquote", "hr"})


def _marked(mark: str, text: str, close: str | None = None) -> str:
    """`text` wrapped in `mark`, keeping its outer whitespace outside the marks."""
    inner = text.strip()
    if not inner:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()) :]
    return f"{lead}{mark}{inner}{mark if close is None else close}{tail}"


def _line(text: str) -> str:
    """Inline text as one paragraph: whitespace (the source's own line breaks included)
    collapsed, explicit line breaks kept."""
    lines = [_squash(line) for line in text.split(BREAK)]
    return "\n".join(line for line in lines if line)


def _has_block_child(node: Node) -> bool:
    return any(
        isinstance(child, Node)
        and (
            child.tag in BLOCK_TAGS
            or child.tag in HEADINGS
            or child.tag in ("ul", "ol", "pre", "table", "blockquote", "hr")
        )
        for child in node.children
    )


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.split("\n"))


def _list(node: Node) -> str:
    ordered = node.tag == "ol"
    items: list[str] = []
    for child in node.children:
        if not isinstance(child, Node) or child.tag != "li" or _hidden(child):
            continue
        body = "\n\n".join(_blocks(child)) if _has_block_child(child) else _line(_inline(child))
        if not body:
            continue
        marker = f"{len(items) + 1}. " if ordered else "- "
        first, _, rest = body.partition("\n")
        item = marker + first
        if rest:
            item += "\n" + _indent(rest, " " * len(marker))
        items.append(item)
    return "\n".join(items)


def _table(node: Node) -> str:
    rows: list[list[str]] = []
    for row in node.walk():
        if row.tag != "tr":
            continue
        cells = [
            _line(_inline(cell)).replace("\n", " ").replace("|", "\\|")
            for cell in row.children
            if isinstance(cell, Node) and cell.tag in ("td", "th")
        ]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    caption = next((n for n in node.walk() if n.tag == "caption"), None)
    title = _line(_inline(caption)) if caption is not None else ""
    return (title + "\n\n" if title else "") + "\n".join(lines)


def _blocks(node: Node) -> list[str]:
    """The Markdown blocks of `node`'s children. Inline content between block elements becomes
    a paragraph of its own."""
    blocks: list[str] = []
    run = Node("#run")

    def flush() -> None:
        text = _line(_inline(run))
        if text:
            blocks.append(text)
        run.children = []

    for child in node.children:
        if isinstance(child, str):
            run.children.append(child)
            continue
        if _hidden(child):
            continue
        tag = child.tag
        if tag in HEADINGS:
            flush()
            title = _line(_inline(child)).replace("\n", " ")
            if title:
                blocks.append("#" * HEADINGS[tag] + " " + title)
        elif tag in ("ul", "ol"):
            flush()
            blocks.append(_list(child))
        elif tag == "pre":
            flush()
            code = child.text().strip("\n")
            if code.strip():
                blocks.append(f"```\n{code}\n```")
        elif tag == "table":
            flush()
            blocks.append(_table(child))
        elif tag == "blockquote":
            flush()
            quoted = "\n\n".join(_blocks(child))
            if quoted:
                blocks.append(_indent(quoted, "> "))
        elif tag == "hr":
            flush()
            blocks.append("---")
        elif tag in BLOCK_TAGS or tag in ("li", "tbody", "thead", "tr", "td", "th"):
            flush()
            blocks.extend(_blocks(child))
        elif tag == "math" and child.attrs.get("display") == "block":
            flush()
            blocks.append(_math(child))
        else:
            run.children.append(child)
    flush()
    return [block for block in blocks if block]


def _drop_site_headers(node: Node) -> None:
    """Remove `<header>` elements that are not part of an article: a page-level header holds
    the site's name and menu, an article's own header its title and byline."""
    kept: list[Node | str] = []
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == "header":
                continue
            if child.tag != "article":
                _drop_site_headers(child)
        kept.append(child)
    node.children = kept


def html_to_markdown(root: Node) -> str:
    """The main content of a parsed page as Markdown ('' if it has none)."""
    content = main_content(root)
    if content.tag != "article":
        _drop_site_headers(content)
    return "\n\n".join(_blocks(content))


# -- HTML: what the page says about itself ------------------------------------------------

_DATE = re.compile(r"^(\d{4})(?:[-/](\d{2})(?:[-/](\d{2}))?)?")
_ARXIV = re.compile(r"^(?:arxiv:)?\s*(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)
_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?\s*(10\.\d{4,9}/\S+)$", re.I)


def _partial_date(value: str) -> str | None:
    match = _DATE.match(value.strip())
    if match is None:
        return None
    year, month, day = match.groups()
    if not 1500 <= int(year) <= 2200:
        return None
    if month is None or not 1 <= int(month) <= 12:
        return year
    if day is None or not 1 <= int(day) <= 31:
        return f"{year}-{month}"
    return f"{year}-{month}-{day}"


def page_details(root: Node) -> PageDetails:
    """Title, authors, date, summary, site name, arXiv id and DOI from the page's `<meta>`
    tags, `<title>`, first `<h1>` and first `<time datetime>`."""
    metas: dict[str, list[str]] = {}
    for node in root.walk():
        if node.tag != "meta":
            continue
        key = (node.attrs.get("property") or node.attrs.get("name") or "").strip().lower()
        value = _squash(node.attrs.get("content", ""))
        if key and value:
            metas.setdefault(key, []).append(value)

    def first(*keys: str) -> str | None:
        return next((metas[key][0] for key in keys if key in metas), None)

    def element_text(tag: str, within: Node) -> str | None:
        node = next((n for n in within.walk() if n.tag == tag), None)
        text = _squash(node.text()) if node is not None else ""
        return text or None

    title = (
        first("citation_title", "og:title", "twitter:title")
        or element_text("h1", main_content(root))
        or element_text("title", root)
    )
    authors = metas.get("citation_author") or metas.get("author") or []
    if not authors:
        # `article:author` is often a profile URL, which is not a name.
        authors = [a for a in metas.get("article:author", []) if "://" not in a]
    published = None
    for key in (
        "citation_publication_date",
        "citation_online_date",
        "citation_date",
        "article:published_time",
        "dc.date",
        "date",
    ):
        published = next(filter(None, map(_partial_date, metas.get(key, []))), None)
        if published:
            break
    if published is None:
        stamp = next((n for n in root.walk() if n.tag == "time" and n.attrs.get("datetime")), None)
        published = _partial_date(stamp.attrs["datetime"]) if stamp is not None else None
    arxiv = _ARXIV.match(first("citation_arxiv_id") or "")
    doi = _DOI.match(first("citation_doi", "dc.identifier") or "")
    return PageDetails(
        title=title,
        authors=tuple(dict.fromkeys(authors)),
        published=published,
        summary=first("og:description", "description", "twitter:description"),
        site_name=first("og:site_name", "application-name"),
        arxiv_id=arxiv.group(1) if arxiv else None,
        doi=doi.group(1) if doi else None,
    )


# -- fetching ------------------------------------------------------------------------------


def resolve_host(host: str) -> list[str]:
    """Every IP address `host` resolves to (socket.gaierror if it does not resolve)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def _base_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""", re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


class UrlFetcher:
    """Fetches a URL as a `FetchedSource`.

    `client` may be any `httpx2.Client`-compatible object (tests pass one with a
    `MockTransport`); by default a client is created per fetch. `resolve(host)` returns the
    host's IP addresses and is how private and local addresses are refused. `youtube` fetches
    a YouTube video address (`fetch(requested_url, video_id)`); it defaults to
    `teacher.youtube_sources.YouTubeFetcher()`.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        resolve: Callable[[str], list[str]] = resolve_host,
        timeout: float = TIMEOUT,
        youtube: Any = None,
    ) -> None:
        self._client = client
        self.resolve = resolve
        self.timeout = timeout
        if youtube is None:
            # Imported here: teacher.youtube_sources imports this module.
            from teacher.youtube_sources import YouTubeFetcher

            youtube = YouTubeFetcher()
        self.youtube = youtube

    # -- the URL --------------------------------------------------------------------------

    def check_url(self, url: str) -> str:
        """`url` if Teacher may fetch it; UrlSourceError otherwise."""
        url = url.strip()
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError as exc:
            raise UrlSourceError(
                f"{url!r} is not a valid web address ({exc})", reason=REASON_INVALID_URL
            ) from exc
        if parts.scheme not in ("http", "https"):
            raise UrlSourceError(
                f"{url!r} is not a web address Teacher can fetch: it must start with http:// "
                "or https://",
                reason=REASON_INVALID_URL,
            )
        if not host:
            raise UrlSourceError(f"{url!r} has no host name", reason=REASON_INVALID_URL)
        if parts.username or parts.password:
            raise UrlSourceError(
                "the address contains a user name or password; remove it",
                reason=REASON_INVALID_URL,
            )
        try:
            addresses = self.resolve(host)
        except OSError as exc:
            raise UrlSourceError(
                f"the host {host!r} could not be found ({exc})", reason=REASON_FETCH_FAILED
            ) from exc
        if not addresses:
            raise UrlSourceError(
                f"the host {host!r} could not be found", reason=REASON_FETCH_FAILED
            )
        for address in addresses:
            if not ipaddress.ip_address(address.split("%")[0]).is_global:
                raise UrlSourceError(
                    f"the host {host!r} is a private or local address ({address}), which "
                    "Teacher does not fetch",
                    reason=REASON_BLOCKED,
                )
        return url

    # -- the request ----------------------------------------------------------------------

    def _get(self, client: Any, url: str) -> tuple[str, str, bytes]:
        """Follow redirects by hand (each hop is checked); return (final url, Content-Type
        header, body)."""
        for _hop in range(MAX_REDIRECTS + 1):
            url = self.check_url(url)
            try:
                with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
                    },
                    follow_redirects=False,
                    timeout=self.timeout,
                ) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise UrlSourceError(
                                f"{url} answered with a redirect (HTTP {status}) to nowhere",
                                reason=REASON_FETCH_FAILED,
                            )
                        url = urljoin(url, location)
                        continue
                    if status != 200:
                        login = status in (401, 402, 403)
                        hint = " The page may need a login." if login else ""
                        raise UrlSourceError(
                            f"{url} answered with HTTP {status}.{hint}",
                            reason=REASON_PAYWALLED if login else REASON_HTTP_ERROR,
                        )
                    content_type = response.headers.get("content-type")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        limit = MAX_PDF_BYTES if body.startswith(b"%PDF") else MAX_TEXT_BYTES
                        if len(body) > limit:
                            raise UrlSourceError(
                                f"{url} is larger than Teacher accepts "
                                f"({MAX_PDF_BYTES // 1_000_000} MB for a PDF, "
                                f"{MAX_TEXT_BYTES // 1_000_000} MB for a page)",
                                reason=REASON_TOO_LARGE,
                            )
                    return url, content_type or "", bytes(body)
            except httpx2.TimeoutException as exc:
                raise UrlSourceError(
                    f"{url} did not answer within {self.timeout:g} seconds",
                    reason=REASON_TIMEOUT,
                ) from exc
            except httpx2.HTTPError as exc:
                raise UrlSourceError(
                    f"{url} could not be fetched ({exc.__class__.__name__}: {exc})",
                    reason=REASON_FETCH_FAILED,
                ) from exc
        raise UrlSourceError(
            f"the address redirects more than {MAX_REDIRECTS} times", reason=REASON_FETCH_FAILED
        )

    # -- the result -----------------------------------------------------------------------

    def fetch(self, url: str) -> FetchedSource:
        from teacher.youtube_sources import youtube_video_id  # see __init__

        requested = url.strip()
        video_id = youtube_video_id(requested)
        if video_id is not None:
            return self.youtube.fetch(requested, video_id)
        if self._client is not None:
            final, content_type, body = self._get(self._client, requested)
        else:
            with httpx2.Client() as client:
                final, content_type, body = self._get(client, requested)
        media_type = _base_type(content_type)
        header_charset = _HEADER_CHARSET.search(content_type)
        charset = header_charset.group(1) if header_charset else None
        if not body:
            raise UrlSourceError(f"{final} returned an empty document", reason=REASON_EMPTY)

        if body.startswith(b"%PDF"):
            return FetchedSource(
                requested, final, KIND_PAPER, PDF_TYPE, ".pdf", body, media_type or PDF_TYPE
            )
        if media_type == PDF_TYPE:
            raise UrlSourceError(
                f"{final} says it is a PDF, but its content is not one",
                reason=REASON_UNSUPPORTED_TYPE,
            )
        if media_type in HTML_TYPES:
            root = parse_html(_decode(body, final, charset))
            markdown = html_to_markdown(root)
            words = word_count(markdown)
            if words < MIN_WORDS:
                raise UrlSourceError(
                    f"{final} has too little readable text ({words} words; at least "
                    f"{MIN_WORDS} are needed). The page may need JavaScript or a login to "
                    "show its content.",
                    reason=REASON_TOO_THIN,
                )
            return FetchedSource(
                requested,
                final,
                KIND_WEB_PAGE,
                "text/markdown",
                ".md",
                (markdown + "\n").encode("utf-8"),
                media_type,
                page_details(root),
                words,
            )
        if media_type in TEXT_TYPES:
            text = _decode(body, final, charset)
            words = word_count(text)
            if words < MIN_WORDS:
                raise UrlSourceError(
                    f"{final} has too little text ({words} words; at least {MIN_WORDS} are "
                    "needed)",
                    reason=REASON_TOO_THIN,
                )
            stored = "text/markdown" if TEXT_TYPES[media_type] == ".md" else "text/plain"
            return FetchedSource(
                requested,
                final,
                KIND_WEB_PAGE,
                stored,
                TEXT_TYPES[media_type],
                text.encode("utf-8"),
                media_type,
                None,
                words,
            )
        raise UrlSourceError(
            f"{final} is {media_type or 'of an undeclared type'}, which Teacher cannot read; "
            "supported are web pages (HTML), PDFs, plain text and Markdown",
            reason=REASON_UNSUPPORTED_TYPE,
        )


def _decode(body: bytes, url: str, charset: str | None) -> str:
    """The body as text, in the encoding the server's Content-Type header names, else the one
    a `<meta charset>` in its first bytes names, else UTF-8. An encoding Python does not know
    and more than a trace of undecodable bytes are errors, not guesses."""
    if charset is None:
        declared = _META_CHARSET.search(body[:4096])
        charset = declared.group(1).decode("ascii") if declared else "utf-8"
    encoding = charset
    try:
        text = body.decode(encoding, errors="replace")
    except LookupError as exc:
        raise UrlSourceError(
            f"{url} is written in the encoding {encoding!r}, which Teacher cannot read",
            reason=REASON_UNDECODABLE,
        ) from exc
    bad = text.count("�")
    if bad > MAX_BAD_CHARS * max(len(text), 1):
        raise UrlSourceError(
            f"the text of {url} could not be decoded ({bad} unreadable characters as {encoding})",
            reason=REASON_UNDECODABLE,
        )
    return text


__all__ = [
    "KIND_PAPER",
    "KIND_WEB_PAGE",
    "MAX_PDF_BYTES",
    "MAX_REDIRECTS",
    "MAX_TEXT_BYTES",
    "MIN_WORDS",
    "REASON_BLOCKED",
    "REASON_EMPTY",
    "REASON_FETCH_FAILED",
    "REASON_HTTP_ERROR",
    "REASON_INVALID_URL",
    "REASON_PAYWALLED",
    "REASON_TIMEOUT",
    "REASON_TOO_LARGE",
    "REASON_TOO_THIN",
    "REASON_UNDECODABLE",
    "REASON_UNREADABLE",
    "REASON_UNSUPPORTED_TYPE",
    "URL_ERROR_REASONS",
    "FetchedSource",
    "PageDetails",
    "UrlFetcher",
    "UrlSourceError",
    "html_to_markdown",
    "main_content",
    "page_details",
    "parse_html",
    "word_count",
]
