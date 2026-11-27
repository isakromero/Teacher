"""HTML pages: home (search and dashboard), the library, adding a source, source, artifact (each
state), lab (each state), concept graph, the header, 404s, static files and vendored assets.

The pages are skeletons filled in by JavaScript from the JSON API, so these tests check the
server side only: status, HTML, the key elements each page's script relies on, and that every
static file a page (or a page script, or a stylesheet) references is actually served.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from sqlalchemy import select

from teacher.db.engine import session_scope
from teacher.db.models import StudyEvent
from teacher.managers import ArtifactManager, ConceptManager, KnowledgeSourceManager
from teacher.web.routers.pages import STATIC_DIR, TEMPLATES_DIR

Json = dict[str, Any]
JS_DIR = STATIC_DIR / "js"
NODE_TEST = Path(__file__).parent / "js" / "math_test.mjs"


# -- helpers ----------------------------------------------------------------------------------


class _Elements(HTMLParser):
    """Collects every start tag as (tag, attrs dict)."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def elements(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Elements()
    parser.feed(html)
    return parser.elements


def by_id(html: str, element_id: str) -> tuple[str, dict[str, str | None]]:
    found = [(t, a) for t, a in elements(html) if a.get("id") == element_id]
    assert len(found) == 1, f"expected one #{element_id}, found {len(found)}"
    return found[0]


def get_html(client: TestClient, path: str, status: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def static_refs(html: str) -> list[str]:
    """`/static/...` URLs in src/href attributes."""
    refs = []
    for _tag, attrs in elements(html):
        for key in ("src", "href"):
            value = attrs.get(key)
            if value and value.startswith("/static/"):
                refs.append(value)
    return refs


_IMPORT = re.compile(r"""(?:^|\n)\s*(?:import|export)\s[^;]*?\bfrom\s*["']([^"']+)["']""")
_CSS_URL = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")


def module_imports(path: Path) -> list[str]:
    return _IMPORT.findall(path.read_text(encoding="utf-8"))


def static_path(url: str) -> Path:
    return STATIC_DIR / url.removeprefix("/static/")


@pytest.fixture
def source(upload_source: Callable[..., Json]) -> Json:
    return upload_source(
        kind="paper",
        title="Attention Is All You Need",
        slug="attention",
        creators=["Ashish Vaswani", "Noam Shazeer"],
        identifiers=["arxiv:1706.03762"],
        originated_on="2017",
        filename="paper.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4\n% tiny test file\n",
    )


def make_artifact(
    settings: Settings, source_slug: str, slug: str, status: str, *, kind: str = "study_page",
    params: dict[str, Any] | None = None,
) -> None:
    """An artifact in `status`, made through the managers (no generation runs)."""
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug(source_slug)
        manager = ArtifactManager(session, settings)
        artifact = manager.create(
            slug=slug, kind=kind, title=f"Study {slug}", generator="test", sources=[source],
            params=params,
        )
        if status == "failed":
            manager.mark_failed(artifact)
        elif status == "ready":
            manager.mark_ready(artifact)


def event_kinds(settings: Settings) -> list[str]:
    with session_scope(settings) as session:
        return [e.kind for e in session.scalars(select(StudyEvent).order_by(StudyEvent.id))]


def nav_of(html: str) -> str:
    return html.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]


# -- home -------------------------------------------------------------------------------------


def test_home_page(client: TestClient, settings: Settings) -> None:
    html = get_html(client, "/")
    assert "<title>Home - Teacher</title>" in html
    _, root = by_id(html, "home-page")
    assert root["data-page"] == "home"
    assert 'class="page page-home"' in html
    # The search box: labelled, described, searching from the address too.
    tag, form = by_id(html, "home-search")
    assert tag == "form" and form["role"] == "search"
    tag, box = by_id(html, "home-search-input")
    assert tag == "input" and box["type"] == "search" and box["name"] == "q" and box["value"] == ""
    assert box["aria-describedby"] == "home-search-hint" and box["aria-controls"] == "home-results"
    assert 'for="home-search-input"' in html
    by_id(html, "home-search-hint")
    _, add = by_id(html, "home-add")
    assert add["href"] == "/add"
    # The results take the place of the sections while a query is in the box.
    _, results = by_id(html, "home-results")
    assert "hidden" in results
    _, status = by_id(html, "home-results-status")
    assert status["role"] == "status" and status["aria-live"] == "polite"
    _, error = by_id(html, "home-results-error")
    assert error["role"] == "alert" and "hidden" in error
    tag, _ = by_id(html, "home-results-list")
    assert tag == "ol"
    by_id(html, "home-dashboard")
    # The three sections, each with a heading and a live region.
    for section, heading in (("home-continue", "Continue studying"), ("home-suggestions", "Suggested next"),
                             ("home-recent", "Recent study")):
        _, region = by_id(html, section)
        assert region["aria-live"] == "polite", section
        _, h2 = by_id(html, f"{section}-heading")
        assert f'id="{section}-heading" class="section-label">{heading}</h2>' in html
    assert 'href="/history">Full history</a>' in html
    assert 'href="/history?suggestions=all#history-suggestions-heading"' in html
    # No forms or lists of the library on home.
    for gone in ("upload-form", "source-list"):
        assert f'id="{gone}"' not in html
    refs = static_refs(html)
    for ref in ("/static/js/home.js", "/static/js/home_search.js", "/static/css/home.css",
                "/static/css/standing.css"):
        assert ref in refs, ref
    assert event_kinds(settings) == []  # looking at home is not studying


def test_home_search_from_the_address(client: TestClient) -> None:
    html = get_html(client, "/?q=attention%20%3Cb%3E")
    _, box = by_id(html, "home-search-input")
    assert box["value"] == "attention <b>"
    assert 'value="attention &lt;b&gt;"' in html  # escaped


def test_home_scripts_search_and_load_the_sections() -> None:
    search = (JS_DIR / "home_search.js").read_text(encoding="utf-8")
    for fragment in ("AbortController", "searchUrl(", "replaceState", '"ArrowDown"', '"Escape"',
                     'event.key !== "/"', 'h("mark"', "checkSegments("):
        assert fragment in search, fragment
    model = (JS_DIR / "search_model.js").read_text(encoding="utf-8")
    assert "/api/search?q=" in model
    home = (JS_DIR / "home.js").read_text(encoding="utf-8")
    for fragment in ("/api/history/continue", "/api/history/suggestions", "/api/history/summary?tz="):
        assert fragment in home, fragment
    for name in ("home.js", "home_search.js", "home_model.js", "search_model.js", "library.js",
                 "library_model.js", "add.js"):
        code = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in code, f"{name}: {sink}"
    assert not (JS_DIR / "library_suggestions.js").exists()


# -- the header -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "current"),
    [("/", "/"), ("/library", "/library"), ("/graph", "/graph"), ("/history", "/history"),
     ("/add", "/add")],
)
def test_header_marks_the_current_page(client: TestClient, path: str, current: str) -> None:
    nav = nav_of(get_html(client, path))
    for href, label in (("/library", "Library"), ("/graph", "Concepts"), ("/history", "History")):
        assert re.search(rf'<a href="{href}"( aria-current="page")?>{label}</a>', nav), (path, href)
    assert re.search(r'<a href="/"( aria-current="page")?>Home</a>', nav)
    assert 'class="button primary compact nav-add" href="/add"' in nav
    marked = re.findall(r'href="([^"]+)"[^>]*aria-current="page"', nav)
    assert marked == [current], (path, marked)  # one link, the page's own


def test_header_marks_nothing_on_other_pages(client: TestClient, source: Json) -> None:
    for path in (f"/sources/{source['slug']}",):
        assert "aria-current" not in nav_of(get_html(client, path))
    assert "aria-current" not in nav_of(get_html(client, "/artifacts/missing", status=404))


def test_header_shows_the_local_sqlite_badge(tmp_path: Path) -> None:
    from teacher.db.migrate import upgrade_to_head
    from teacher.generation import StubGenerator
    from teacher.web.app import create_app

    local = Settings(root=tmp_path / "local")
    upgrade_to_head(local)
    with TestClient(create_app(local, generator=StubGenerator())) as test_client:
        for path, status in (("/", 200), ("/library", 200), ("/artifacts/missing", 404)):
            nav = nav_of(get_html(test_client, path, status=status))
            badge = re.search(r'<span class="db-badge" title="([^"]*)">Local SQLite<', nav)
            assert badge is not None, path
            assert badge.group(1) == f"Database: sqlite:{local.db_path}"
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    rule = css.split(".db-badge {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in rule and "text-overflow: ellipsis" in rule  # shrinks, no scroll


def test_header_shows_no_badge_on_supabase(tmp_path: Path) -> None:
    """Supabase mode: no badge (and the connection string appears nowhere). The page itself
    needs no database, so this runs without one (no lifespan: nothing connects)."""
    from teacher.generation import StubGenerator
    from teacher.web.app import create_app

    password = "pw-not-shown"
    remote = Settings(
        root=tmp_path / "remote",
        db="supabase",
        supabase_db_url=f"postgresql://teacher:{password}@127.0.0.1:9/teacher",
    )
    test_client = TestClient(create_app(remote, generator=StubGenerator()))
    html = get_html(test_client, "/add")
    assert "db-badge" not in html and "Local SQLite" not in html
    assert password not in html


# -- library ----------------------------------------------------------------------------------


def test_library_page(client: TestClient, settings: Settings) -> None:
    html = get_html(client, "/library")
    assert "<title>Library - Teacher</title>" in html
    by_id(html, "library-page")
    _, listing = by_id(html, "source-list")
    assert listing["aria-live"] == "polite"
    tag, box = by_id(html, "library-filter")
    assert tag == "input" and box["type"] == "search"
    assert 'for="library-filter"' in html
    _, status = by_id(html, "library-filter-status")
    assert status["role"] == "status"
    by_id(html, "library-count")
    assert 'href="/add"' in html.split("</nav>", 1)[1]  # an Add button on the page itself
    assert "upload-form" not in html
    assert "/static/js/library.js" in static_refs(html)
    assert event_kinds(settings) == ["library_opened"]


def test_library_lists_nothing_server_side(client: TestClient, source: Json) -> None:
    # The list is fetched by the page script; the page itself only carries the skeleton.
    html = get_html(client, "/library")
    assert source["title"] not in html


# -- add a source -----------------------------------------------------------------------------


def test_add_page(client: TestClient, settings: Settings) -> None:
    html = get_html(client, "/add")
    assert "<title>Add a source - Teacher</title>" in html
    by_id(html, "add-page")
    tag, form = by_id(html, "upload-form")
    assert tag == "form"
    _, file_input = by_id(html, "upload-file")
    assert file_input["type"] == "file" and "pdf" in (file_input["accept"] or "")
    assert "required" in file_input
    _, title = by_id(html, "upload-title")
    assert "required" in title
    for field in ("upload-authors", "upload-arxiv", "upload-year", "upload-error"):
        by_id(html, field)
    assert 'id="source-list"' not in html  # the list is on /library
    _, generate = by_id(html, "upload-generate")
    assert generate["type"] == "checkbox" and "checked" in generate
    tag, _ = by_id(html, "upload-model")
    assert tag == "select"
    _, model_field = by_id(html, "upload-model-field")
    assert "hidden" in model_field  # shown by add.js once the choices are loaded
    assert "/static/js/add.js" in static_refs(html)
    assert event_kinds(settings) == []  # adding is logged when it happens (source_added)


def test_add_page_detection_elements(client: TestClient) -> None:
    """The elements add.js uses for automatic detection of a paper's details."""
    html = get_html(client, "/add")
    _, status = by_id(html, "upload-detect-status")
    assert status["aria-live"] == "polite"
    _, detect_error = by_id(html, "upload-detect-error")
    assert detect_error["role"] == "alert" and "hidden" in detect_error
    _, duplicate = by_id(html, "upload-duplicate")
    assert "hidden" in duplicate
    # The detail fields are hidden until a PDF has been read.
    _, details = by_id(html, "upload-details")
    assert "hidden" in details
    by_id(html, "upload-detected-note")
    tag, doi = by_id(html, "upload-doi")
    assert tag == "input" and doi["name"] == "doi"
    tag, summary = by_id(html, "upload-summary")
    assert tag == "textarea" and summary["name"] == "summary"
    for field in ("upload-doi", "upload-summary", "upload-title", "upload-year"):
        assert f'for="{field}"' in html  # every field has a label
    tag, submit = by_id(html, "upload-submit")
    assert tag == "button" and "disabled" in submit
    assert "Add to library" in html
    # The two ways to add a source: a PDF from this computer, or a web address.
    for field in ("upload-mode-file", "upload-mode-url", "upload-file-field", "upload-url"):
        by_id(html, field)
    _, url_field = by_id(html, "upload-url-field")
    assert "hidden" in url_field
    tag, _ = by_id(html, "upload-url-fetch")
    assert tag == "button"
    # The page script posts to the inspect endpoint.
    add = (JS_DIR / "add.js").read_text(encoding="utf-8")
    for fragment in ("/api/sources/inspect", "/api/sources/inspect-url", "/api/sources/from-url",
                     '"/api/artifacts"'):
        assert fragment in add, fragment


# -- source -----------------------------------------------------------------------------------


def test_source_page(client: TestClient, source: Json) -> None:
    html = get_html(client, f"/sources/{source['slug']}")
    _, root = by_id(html, "source-page")
    assert root["data-slug"] == source["slug"]
    assert "<title>Attention Is All You Need - Teacher</title>" in html
    _, link = by_id(html, "source-file-link")
    assert link["href"] == f"/api/sources/{source['slug']}/file"
    assert client.get(link["href"]).status_code == 200
    for element_id in (
        "source-meta",
        "generate-button",
        "generate-model",
        "generate-model-field",
        "artifact-list",
        "concept-list",
        "concepts-heading",
        "delete-button",
    ):
        by_id(html, element_id)
    _, confirm = by_id(html, "delete-confirm")
    assert "hidden" in confirm  # the inline confirmation starts closed
    by_id(html, "delete-confirm-button")
    by_id(html, "delete-cancel-button")
    by_id(html, "delete-error")
    assert "/static/js/source.js" in static_refs(html)


def test_source_title_is_escaped(client: TestClient, upload_source: Callable[..., Json]) -> None:
    created = upload_source(title="<script>alert(1)</script> & co", slug="xss")
    html = get_html(client, f"/sources/{created['slug']}")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html


def test_unknown_source_is_html_404(client: TestClient) -> None:
    html = get_html(client, "/sources/no-such-source", status=404)
    assert "Not found" in html and "no-such-source" in html
    by_id(html, "main")


# -- artifact ---------------------------------------------------------------------------------


PANELS = {"generating": "state-generating", "failed": "state-failed", "ready": "state-ready"}


@pytest.mark.parametrize("status", ["generating", "failed", "ready"])
def test_artifact_page_states(
    client: TestClient, settings: Settings, source: Json, status: str
) -> None:
    slug = f"study-{status}"
    make_artifact(settings, source["slug"], slug, status)
    html = get_html(client, f"/artifacts/{slug}")
    _, root = by_id(html, "artifact-page")
    assert root["data-slug"] == slug
    assert root["data-status"] == status
    for state, panel_id in PANELS.items():
        _, panel = by_id(html, panel_id)
        assert ("hidden" in panel) == (state != status), panel_id
    for element_id in (
        "artifact-title",
        "reader-progress",
        "generating-stages",
        "failed-summary",
        "regenerate-button",
        "artifact-actions",
        "regenerate-study-button",
        "regenerate-confirm",
        "regenerate-study-confirm",
        "regenerate-study-cancel",
        "regenerate-study-error",
        "regenerate-model",
        "regenerate-model-field",
        "regenerate-study-model",
        "regenerate-study-model-field",
        "blocks",
        "reader-footer",
        "artifact-error",
    ):
        by_id(html, element_id)
    refs = static_refs(html)
    assert "/static/js/artifact.js" in refs
    assert "/static/vendor/katex-0.16.22/katex.min.css" in refs


def test_artifact_page_tutor_chat(client: TestClient, settings: Settings, source: Json) -> None:
    """The tutor side chat skeleton that chat.js fills in (shown only in the `ready` state)."""
    make_artifact(settings, source["slug"], "study-chat", "ready")
    html = get_html(client, "/artifacts/study-chat")
    _, main = by_id(html, "main")
    assert "page-artifact" in (main["class"] or "").split()
    by_id(html, "artifact-layout")
    tag, panel = by_id(html, "tutor")
    assert tag == "aside" and "hidden" in panel  # chat.js shows it once the reader is mounted
    assert panel["aria-labelledby"] == "tutor-heading"
    assert by_id(html, "tutor-heading")[0] == "h2"
    tag, messages = by_id(html, "tutor-messages")
    assert messages["role"] == "log"
    by_id(html, "tutor-empty")
    _, status = by_id(html, "tutor-status")
    assert status["aria-live"] == "polite"
    tag, form = by_id(html, "tutor-composer")
    assert tag == "form"
    tag, textarea = by_id(html, "tutor-input")
    assert tag == "textarea"
    assert 'for="tutor-input"' in html  # labelled
    tag, refs = by_id(html, "tutor-refs")
    assert tag == "ul" and "hidden" in refs
    _, hint = by_id(html, "tutor-hint")
    assert "hidden" in hint
    tag, send = by_id(html, "tutor-send")
    assert tag == "button" and send["type"] == "submit"
    for button_id in ("tutor-check", "tutor-new", "tutor-toggle", "tutor-open"):
        tag, button = by_id(html, button_id)
        assert tag == "button" and button["type"] == "button", button_id
    assert by_id(html, "tutor-open")[1]["aria-controls"] == "tutor"
    for text in ("Check my understanding", "New conversation", "Ask the tutor", "Send"):
        assert text in html, text
    assert "Ask about this" not in html  # the highlight popover asks the tutor (highlights.js)
    # The page script loads the chat, which loads the SSE parser and the reference helpers.
    seen = _crawl(client, static_refs(html))
    for expected in ("/static/js/chat.js", "/static/js/sse.js", "/static/js/references.js"):
        assert expected in seen, expected
    chat = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    for fragment in ("/chat", "/threads", "/messages", "getReader", "data-block-key"):
        assert fragment in chat, fragment
    # Blocks carry the key the chat attributes a selection to.
    assert "blockKey: block.key" in (JS_DIR / "blocks" / "registry.js").read_text(encoding="utf-8")


def test_artifact_page_glossary(client: TestClient, settings: Settings, source: Json) -> None:
    """The glossary panel glossary.js fills in, and the scripts that mark terms on the page."""
    make_artifact(settings, source["slug"], "study-glossary", "ready")
    html = get_html(client, "/artifacts/study-glossary")
    tag, panel = by_id(html, "glossary")
    assert tag == "details" and "hidden" in panel  # shown once the glossary has loaded
    assert by_id(html, "glossary-count")[0] == "span"
    tag, build = by_id(html, "glossary-build")
    assert "hidden" in build
    tag, button = by_id(html, "glossary-build-button")
    assert tag == "button" and button["type"] == "button"
    assert by_id(html, "glossary-build-status")[1]["aria-live"] == "polite"
    _, error = by_id(html, "glossary-error")
    assert error["role"] == "alert" and "hidden" in error
    assert by_id(html, "glossary-list")[0] == "dl"
    by_id(html, "glossary-empty")
    for text in ("Glossary", "No glossary has been built for this page", "Build glossary"):
        assert text in html, text
    # The glossary sits in the reader, before the blocks (not inside them, so its text is never
    # part of a selection in a block).
    assert html.index('id="glossary"') < html.index('id="blocks"')
    seen = _crawl(client, static_refs(html))
    for expected in ("/static/js/reader.js", "/static/js/glossary.js", "/static/js/glossary_match.js"):
        assert expected in seen, expected
    script = (JS_DIR / "glossary.js").read_text(encoding="utf-8")
    for fragment in ('role: "tooltip"', "aria-describedby", "/glossary", "MutationObserver",
                     "Escape", "Could not build the glossary", "Could not load the glossary"):
        assert fragment in script, fragment
    assert "title:" not in script and ".title =" not in script  # no native tooltips
    assert "glossary.refresh()" in (JS_DIR / "reader.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".gloss-term", ".gloss-popover", ".glossary-list"):
        assert selector in css, selector


def test_artifact_page_highlights(client: TestClient, settings: Settings, source: Json) -> None:
    """The "Highlights and notes" panel highlights.js fills in, and the script's hooks: the
    popover follows the selection (selectionchange), asks the tutor through the chat's
    controller, and every failure is shown."""
    make_artifact(settings, source["slug"], "study-highlights", "ready")
    html = get_html(client, "/artifacts/study-highlights")
    tag, panel = by_id(html, "highlights")
    assert tag == "details" and "hidden" in panel  # shown once the highlights have loaded
    assert by_id(html, "highlights-count")[0] == "span"
    _, error = by_id(html, "highlights-error")
    assert error["role"] == "alert" and "hidden" in error
    assert by_id(html, "highlights-list")[0] == "ol"
    assert by_id(html, "highlights-status")[1]["aria-live"] == "polite"
    by_id(html, "highlights-empty")
    assert "Highlights and notes" in html
    # Outside the blocks, so its text is never part of a selection in a block.
    assert html.index('id="glossary"') < html.index('id="highlights"') < html.index('id="blocks"')
    seen = _crawl(client, static_refs(html))
    for expected in ("/static/js/highlights.js", "/static/js/highlight_model.js"):
        assert expected in seen, expected
    script = (JS_DIR / "highlights.js").read_text(encoding="utf-8")
    for fragment in ('"selectionchange"', "MutationObserver", "/highlights", "Ask the tutor",
                     "Add note", "Edit note", "Remove", "Not found on the page",
                     "Could not save the highlight", "Could not load your highlights",
                     "teacher:highlights", "teacher:highlight-popover", "window.teacherHighlights"):
        assert fragment in script, fragment
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "contextmenu"):
        assert sink not in script, sink
    artifact = (JS_DIR / "artifact.js").read_text(encoding="utf-8")
    assert "selectionButton: false" in artifact and "chat.attachReference(ref)" in artifact
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for selector in ("mark.hl", ".hl-atom", ".hl-popover", ".highlights-list", ".hl-entry.is-missing"):
        assert selector in css, selector


def test_artifact_page_labs_section(client: TestClient, settings: Settings, source: Json) -> None:
    """The ready study page's "Hands-on lab" section, after the reader footer: the labs made
    from the page and "Create a lab" with the model picker (study_labs.js fills it in)."""
    make_artifact(settings, source["slug"], "study-labs", "ready")
    html = get_html(client, "/artifacts/study-labs")
    tag, section = by_id(html, "study-labs")
    assert tag == "section" and "hidden" in section
    assert section["aria-labelledby"] == "study-labs-heading"
    assert "Hands-on lab" in html and "takes a few minutes" in html
    # In the ready panel, after the footer, and outside the blocks (no "Ask about this" on it).
    ready = html.index('id="state-ready"')
    assert ready < html.index('id="reader-footer"') < html.index('id="study-labs"') < html.index('id="tutor"')
    by_id(html, "study-lab-list")
    tag, button = by_id(html, "create-lab-button")
    assert tag == "button" and button["type"] == "button"
    assert by_id(html, "create-lab-model")[0] == "select"
    assert "hidden" in by_id(html, "create-lab-model-field")[1]
    _, error = by_id(html, "create-lab-error")
    assert error["role"] == "alert" and "hidden" in error
    seen = _crawl(client, static_refs(html))
    for expected in ("/static/js/study_labs.js", "/static/js/artifact_list.js", "/static/js/generation_view.js"):
        assert expected in seen, expected
    script = (JS_DIR / "study_labs.js").read_text(encoding="utf-8")
    for fragment in ("`${base}/labs`", "chosenModel(MODEL_ID)", "Could not create a lab", "artifactUrl(lab)"):
        assert fragment in script, fragment


def test_artifact_lists_tell_labs_from_study_pages() -> None:
    """The library, the source page and the study page list labs with their own link, tag and
    progress wording (artifact_list.js)."""
    shared = (JS_DIR / "artifact_list.js").read_text(encoding="utf-8")
    for fragment in ("`/labs/${seg(artifact.slug)}`", "`/artifacts/${seg(artifact.slug)}`",
                     '"checked step"', '"checkpoint"', '"Lab"', "params.study_page"):
        assert fragment in shared, fragment
    for name in ("library.js", "source.js"):
        code = (JS_DIR / name).read_text(encoding="utf-8")
        assert "artifactListNodes(" in code, name
        # No list of its own that would show a lab as a study page.
        assert "checkpoint" not in code and "statusBadge" not in code, name


def test_artifact_page_after_generation(client: TestClient, source: Json) -> None:
    # TestClient runs the background generation before returning, so the stub page is ready.
    created = client.post("/api/artifacts", json={"sources": [source["slug"]]}).json()
    html = get_html(client, f"/artifacts/{created['slug']}")
    assert by_id(html, "artifact-page")[1]["data-status"] == "ready"
    assert client.get(f"/api/artifacts/{created['slug']}/blocks").status_code == 200


def test_unknown_artifact_is_html_404(client: TestClient) -> None:
    html = get_html(client, "/artifacts/no-such-page", status=404)
    assert "Not found" in html and "no-such-page" in html


def test_api_404s_stay_json(client: TestClient) -> None:
    response = client.get("/api/artifacts/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# -- lab --------------------------------------------------------------------------------------


@pytest.fixture
def study_page(client: TestClient, source: Json) -> Json:
    """A ready study page generated by the stub generator."""
    created = client.post("/api/artifacts", json={"sources": [source["slug"]]})
    assert created.status_code == 202, created.text
    return client.get(f"/api/artifacts/{created.json()['slug']}").json()


def make_lab_page(client: TestClient, settings: Settings, source: Json, study_page: Json,
                  status: str) -> str:
    """A lab in `status`: generated by the stub when ready, else made through the managers."""
    if status == "ready":
        created = client.post(f"/api/artifacts/{study_page['slug']}/labs", json={})
        assert created.status_code == 202, created.text
        return created.json()["slug"]
    slug = f"lab-{status}"
    make_artifact(settings, source["slug"], slug, status, kind="lab",
                  params={"study_page": study_page["slug"]})
    return slug


@pytest.mark.parametrize("status", ["generating", "failed", "ready"])
def test_lab_page_states(
    client: TestClient, settings: Settings, source: Json, study_page: Json, status: str
) -> None:
    slug = make_lab_page(client, settings, source, study_page, status)
    html = get_html(client, f"/labs/{slug}")
    _, root = by_id(html, "lab-page")
    assert root["data-page"] == "lab" and root["data-slug"] == slug and root["data-status"] == status
    assert "page-lab" in (by_id(html, "main")[1]["class"] or "").split()
    for state, panel_id in PANELS.items():
        _, panel = by_id(html, panel_id)
        assert ("hidden" in panel) == (state != status), panel_id
    # The generating and failed states use the elements generation_view.js expects.
    for element_id in ("lab-title", "artifact-meta", "artifact-notices", "generating-stages",
                       "generating-error", "failed-summary", "failed-stages", "regenerate-button",
                       "regenerate-error", "regenerate-model", "regenerate-model-field",
                       "artifact-error", "lab-crumb-page", "lab-crumb-link"):
        by_id(html, element_id)
    assert "hidden" in by_id(html, "lab-crumb-page")[1]  # lab.js shows it with the study page
    assert by_id(html, "lab-export")[1]["href"] == f"/api/labs/{slug}/export"
    refs = static_refs(html)
    assert "/static/js/lab.js" in refs
    assert "/static/vendor/katex-0.16.22/katex.min.css" in refs


def test_lab_page_ready_elements(
    client: TestClient, settings: Settings, source: Json, study_page: Json
) -> None:
    """The skeleton lab.js fills in: brief, steps, the editor, Run, the output, the tools with
    their in-page confirmations, the runtime notice and the tutor (without references)."""
    slug = make_lab_page(client, settings, source, study_page, "ready")
    html = get_html(client, f"/labs/{slug}")
    for element_id in ("lab-brief", "lab-progress", "lab-editors", "lab-cursor", "lab-run-status",
                       "lab-output", "lab-sandbox", "lab-tools-error", "lab-solution-files"):
        by_id(html, element_id)
    assert by_id(html, "lab-steps")[0] == "ol"
    _, tabs = by_id(html, "lab-tabs")
    assert tabs["role"] == "tablist" and tabs["aria-label"] == "Files"
    keys = by_id(html, "lab-editor-keys")
    assert "Escape, then Tab" in html and "Ctrl+Enter" in html and keys[0] == "p"
    tag, run = by_id(html, "lab-run")
    assert tag == "button" and run["type"] == "button"
    # Check next to Run (lab.js shows it for a step with a check), Indent and Outdent for touch
    # keyboards, and the narrow layout's Instructions / Code switch.
    tag, check = by_id(html, "lab-check")
    assert tag == "button" and check["type"] == "button" and "hidden" in check
    for button_id in ("lab-indent", "lab-outdent"):
        assert by_id(html, button_id)[0] == "button", button_id
    for view, panel_id in (("text", "lab-text"), ("code", "lab-workspace")):
        assert f'data-view="{view}" aria-controls="{panel_id}"' in html
        by_id(html, panel_id)
    assert by_id(html, "lab-current-step")[0] == "select" and 'for="lab-current-step"' in html
    _, save = by_id(html, "lab-save-status")
    assert save["role"] == "status" and save["aria-live"] == "polite"
    _, save_error = by_id(html, "lab-save-error")
    assert save_error["role"] == "alert" and "hidden" in save_error
    _, runtime = by_id(html, "lab-runtime")
    assert "hidden" in runtime
    # Reset and Show solution ask in the page first (no browser dialogs).
    for button_id, panel_id in (("lab-reset", "lab-reset-confirm"),
                                ("lab-solution-button", "lab-solution-confirm")):
        _, button = by_id(html, button_id)
        assert button["aria-controls"] == panel_id and button["aria-expanded"] == "false"
        assert "hidden" in by_id(html, panel_id)[1]
    for button_id in ("lab-reset-yes", "lab-reset-cancel", "lab-solution-yes", "lab-solution-cancel",
                      "lab-solution-hide"):
        tag, button = by_id(html, button_id)
        assert tag == "button" and button["type"] == "button", button_id
    assert "hidden" in by_id(html, "lab-solution")[1]
    tag, export = by_id(html, "lab-export")
    assert tag == "a" and "download" in export
    assert client.get(export["href"]).headers["content-type"] == "application/zip"
    for text in ("Reset to starter code", "Show solution", "Download as a folder", "Run"):
        assert text in html, text
    # The tutor: the same skeleton as on a study page, with text about the lab.
    _, panel = by_id(html, "tutor")
    assert "hidden" in panel
    for element_id in ("lab-layout", "tutor-messages", "tutor-empty", "tutor-composer", "tutor-input",
                       "tutor-refs", "tutor-hint", "tutor-send", "tutor-check", "tutor-new",
                       "tutor-toggle", "tutor-open"):
        by_id(html, element_id)
    assert "Ask about the code, an error, or what to try next" in html
    assert "Ask about this" not in html
    seen = _crawl(client, static_refs(html))
    for expected in ("/static/js/lab.js", "/static/js/lab_model.js", "/static/js/lab_editor.js",
                     "/static/js/lab_sandbox.js", "/static/js/lab_sandbox_frame.js",
                     "/static/js/chat.js", "/static/js/generation_view.js", "/static/js/markdown.js"):
        assert expected in seen, expected


def test_lab_page_scripts() -> None:
    """lab.js uses the lab endpoints, turns references off in the chat and saves before every
    message; no script of the lab inserts HTML (run output is text)."""
    code = (JS_DIR / "lab.js").read_text(encoding="utf-8")
    for fragment in ("`${labBase}/workspace`", "`${labBase}/runs`", "`${labBase}/solution`",
                     '"/api/labs/runtime"', "blocksRoot: null", "beforeSend", "saver.flush()",
                     "createSandbox(", "sandbox.ready.then(", "filesToObject(files)", "runReport(",
                     "visibilitychange", "keepalive", "info.detail", "pollGeneration(artifactBase"):
        assert fragment in code, fragment
    chat = (JS_DIR / "chat.js").read_text(encoding="utf-8")
    assert "if (beforeSend) await beforeSend();" in chat
    assert "if (blocksRoot && selectionButton) setupSelectionButton(blocksRoot, attach);" in chat
    for name in ("lab.js", "lab_editor.js", "lab_model.js", "generation_view.js", "artifact_list.js",
                 "study_labs.js"):
        text = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in text, f"{name}: {sink}"
    editor = (JS_DIR / "lab_editor.js").read_text(encoding="utf-8")
    for fragment in ('spellcheck: "false"', 'wrap: "off"', 'role: "tab"', '"insertText"', "Escape"):
        assert fragment in editor, fragment
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".lab-grid", ".lab-code", ".lab-step", ".lab-output", ".study-labs", ".tag-lab"):
        assert selector in css, selector
    template = (TEMPLATES_DIR / "lab.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", template)


# -- concept graph ----------------------------------------------------------------------------

D3_SCRIPTS = [
    "/static/vendor/d3-dispatch-3.0.2/d3-dispatch.js",
    "/static/vendor/d3-quadtree-3.0.2/d3-quadtree.js",
    "/static/vendor/d3-timer-3.0.2/d3-timer.js",
    "/static/vendor/d3-force-3.0.0/d3-force.js",
]
GRAPH_JS = ["graph.js", "graph_view.js", "graph_inspector.js", "graph_model.js", "d3_force.js"]


def test_nav_links_to_the_concept_graph(client: TestClient) -> None:
    assert '<a href="/graph">Concepts</a>' in nav_of(get_html(client, "/"))
    assert '<a href="/graph" aria-current="page">Concepts</a>' in nav_of(get_html(client, "/graph"))


def test_graph_page(client: TestClient) -> None:
    html = get_html(client, "/graph")
    assert "<title>Concepts - Teacher</title>" in html
    _, page = by_id(html, "graph-page")
    assert page["data-page"] == "graph" and page["data-concept"] == ""
    assert by_id(html, "main")[1]["class"] == "page page-graph"
    tag, svg = by_id(html, "graph-svg")
    assert tag == "svg" and svg["role"] == "img" and svg["aria-labelledby"] == "graph-svg-label"
    by_id(html, "graph-svg-label")
    tag, inspector = by_id(html, "graph-inspector")
    assert tag == "aside" and inspector["aria-label"] == "Inspector"
    # Search: a labelled input, a real submit button, a status line, a results list.
    tag, form = by_id(html, "graph-search")
    assert tag == "form" and form["role"] == "search"
    tag, search = by_id(html, "graph-search-input")
    assert tag == "input" and search["type"] == "search"
    assert 'for="graph-search-input"' in html
    tag, submit = by_id(html, "graph-search-submit")
    assert tag == "button" and submit["type"] == "submit"
    assert by_id(html, "graph-search-status")[1]["aria-live"] == "polite"
    assert "hidden" in by_id(html, "graph-search-results")[1]
    # The notice saying whether meaning was searched starts hidden; every search shows it.
    assert "hidden" in by_id(html, "graph-search-notice")[1]
    # Zoom controls are buttons with labels.
    for button in ("graph-zoom-in", "graph-zoom-out", "graph-fit"):
        tag, attrs = by_id(html, button)
        assert tag == "button" and attrs["type"] == "button" and attrs["aria-label"], button
    # The empty state, the drawing and the error start hidden; the script picks one.
    for hidden in ("graph-empty", "graph-view", "graph-error", "graph-note"):
        assert "hidden" in by_id(html, hidden)[1], hidden
    assert by_id(html, "graph-error")[1]["role"] == "alert"
    assert "No concepts yet" in html
    by_id(html, "graph-loading")
    by_id(html, "graph-status")
    by_id(html, "graph-origin-legend")
    # Summaries are rendered with markdown.js, whose maths needs the KaTeX stylesheet.
    assert "/static/vendor/katex-0.16.22/katex.min.css" in static_refs(html)


def test_graph_page_loads_d3_before_its_module(client: TestClient) -> None:
    html = get_html(client, "/graph")
    scripts = [(a.get("src"), a.get("type")) for tag, a in elements(html) if tag == "script"]
    srcs = [src for src, _type in scripts]
    assert srcs[-5:] == [*D3_SCRIPTS, "/static/js/graph.js"]
    # The d3 files are classic scripts (they define window.d3), the page script a module.
    assert all(t is None for src, t in scripts if src in D3_SCRIPTS)
    assert ("/static/js/graph.js", "module") in scripts
    for src in D3_SCRIPTS:
        response = client.get(src)
        assert response.status_code == 200 and "javascript" in response.headers["content-type"]
        assert (static_path(src).parent / "LICENSE").is_file(), src
    # Only the wrapper reads the global.
    for name in GRAPH_JS:
        code = (JS_DIR / name).read_text(encoding="utf-8")
        assert ("globalThis.d3" in code) == (name == "d3_force.js"), name
        assert "window.d3" not in code.replace("`window.d3`", ""), name


def test_graph_page_scripts_insert_no_html(client: TestClient) -> None:
    """No innerHTML/outerHTML/insertAdjacentHTML in the graph scripts (markdown.js sanitises the
    one HTML they show), and no inline event-handler attributes in the template."""
    for name in GRAPH_JS:
        code = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in code, f"{name}: {sink}"
    template = (TEMPLATES_DIR / "graph.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", template)


def test_graph_page_focused_on_a_concept(client: TestClient, settings: Settings) -> None:
    with session_scope(settings) as session:
        ConceptManager(session, settings).create(
            "Attention <i>", "About attention.", "paper", slug="attention"
        )
    html = get_html(client, "/graph?concept=attention")
    assert by_id(html, "graph-page")[1]["data-concept"] == "attention"
    assert "<title>Attention &lt;i&gt; - Concepts - Teacher</title>" in html


@pytest.mark.parametrize("query", ["?concept=no-such-concept", "?concept="])
def test_graph_page_unknown_concept_is_html_404(client: TestClient, query: str) -> None:
    html = get_html(client, f"/graph{query}", status=404)
    assert "Not found" in html and "There is no concept called" in html


# -- static files -----------------------------------------------------------------------------


def test_static_files_are_served(client: TestClient) -> None:
    css = client.get("/static/css/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    # Browsers refuse ES modules not served with a JavaScript MIME type (.mjs included).
    for path in sorted([*JS_DIR.rglob("*.js"), *(STATIC_DIR / "vendor").rglob("*.mjs")]):
        url = "/static/" + path.relative_to(STATIC_DIR).as_posix()
        response = client.get(url)
        assert response.status_code == 200, url
        assert "javascript" in response.headers["content-type"], url
    assert client.get("/static/nope.js").status_code == 404


def _page_htmls(client: TestClient, settings: Settings, source: Json) -> list[str]:
    make_artifact(settings, source["slug"], "refs-page", "ready")
    make_artifact(settings, source["slug"], "refs-lab", "failed", kind="lab",
                  params={"study_page": "refs-page"})
    return [
        get_html(client, "/"),
        get_html(client, "/library"),
        get_html(client, "/add"),
        get_html(client, "/history"),
        get_html(client, f"/sources/{source['slug']}"),
        get_html(client, "/artifacts/refs-page"),
        get_html(client, "/labs/refs-lab"),
        get_html(client, "/graph"),
        get_html(client, "/artifacts/missing", status=404),
    ]


def _crawl(client: TestClient, pending: list[str]) -> set[str]:
    """Follow static refs -> module imports -> CSS url()s; every file must exist and be served."""
    seen: set[str] = set()
    while pending:
        url = pending.pop()
        if url in seen:
            continue
        seen.add(url)
        path = static_path(url)
        assert path.is_file(), f"{url} is referenced but {path} does not exist"
        assert client.get(url).status_code == 200, url
        if path.suffix in (".js", ".mjs"):
            pending += [urljoin(url, spec) for spec in module_imports(path)]
        elif path.suffix == ".css":
            text = path.read_text(encoding="utf-8")
            pending += [
                urljoin(url, ref) for ref in _CSS_URL.findall(text) if not ref.startswith("data:")
            ]
    return seen


def test_every_referenced_static_file_exists(
    client: TestClient, settings: Settings, source: Json
) -> None:
    """Templates -> scripts/stylesheets -> module imports -> CSS url()s, all present and served."""
    pending = [ref for html in _page_htmls(client, settings, source) for ref in static_refs(html)]
    assert pending
    seen = _crawl(client, pending)
    # The crawl must have reached the vendored libraries and the KaTeX fonts.
    for expected in (
        "/static/vendor/marked-15.0.12/marked.esm.js",
        "/static/vendor/dompurify-3.2.6/purify.es.mjs",
        "/static/vendor/katex-0.16.22/katex.mjs",
        "/static/js/blocks/registry.js",
        "/static/js/blocks/blurb.js",
        "/static/js/chat.js",
        "/static/js/sse.js",
        "/static/js/references.js",
        "/static/js/graph_view.js",
        "/static/js/d3_force.js",
        "/static/vendor/d3-force-3.0.0/d3-force.js",
        "/static/vendor/d3-dispatch-3.0.2/d3-dispatch.js",
        "/static/vendor/d3-quadtree-3.0.2/d3-quadtree.js",
        "/static/vendor/d3-timer-3.0.2/d3-timer.js",
    ):
        assert expected in seen, expected
    assert any(u.startswith("/static/vendor/katex-0.16.22/fonts/") for u in seen)


def test_templates_reference_only_static_or_api(client: TestClient) -> None:
    """No CDN or other external script/stylesheet: the app must work offline."""
    for template in TEMPLATES_DIR.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"""(src|href)=["']https?://""", text), template.name


def test_no_browser_dialogs_in_page_scripts() -> None:
    # A bare call such as `confirm(...)`; method definitions like `prompt(block, id) {` are fine.
    dialog = re.compile(
        r"(?<![.\w$])(alert|confirm|prompt)\s*\((?![^)]*\)\s*\{)|window\.(alert|confirm|prompt)\b"
    )
    comments = re.compile(r"/\*.*?\*/|^\s*//.*?$", re.S | re.M)
    for path in JS_DIR.rglob("*.js"):
        code = comments.sub("", path.read_text(encoding="utf-8"))
        match = dialog.search(code)
        assert match is None, f"{path.name}: {match and match.group(0)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_markdown_keeps_latex_intact() -> None:
    """protectMath + marked leave TeX with underscores/backslashes untouched (runs under node)."""
    result = subprocess.run(
        ["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "math ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("sse_test.mjs", "sse ok"),
        ("references_test.mjs", "references ok"),
        ("glossary_match_test.mjs", "glossary ok"),
        ("interactable_test.mjs", "interactable ok"),
        ("chart_test.mjs", "chart ok"),
        ("graph_model_test.mjs", "graph ok"),
        ("graph_relate_test.mjs", "graph relate ok"),
        ("graph_search_test.mjs", "graph search ok"),
        ("lab_test.mjs", "lab ok"),
        ("home_test.mjs", "home ok"),
        ("highlight_test.mjs", "highlight ok"),
    ],
)
def test_tutor_chat_helpers(script: str, marker: str) -> None:
    """The chat's SSE parser and reference helpers (selected text, limits), and the glossary
    matcher (which must leave the selected text unchanged), under node."""
    result = subprocess.run(
        ["node", str(NODE_TEST.parent / script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert marker in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_scripts_parse() -> None:
    for path in sorted(JS_DIR.rglob("*.js")):
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_static_files_must_be_revalidated(client) -> None:
    response = client.get("/static/js/dom.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_source_page_script_lists_concepts_by_role() -> None:
    code = (JS_DIR / "source.js").read_text(encoding="utf-8")
    assert "`${base}/concepts`" in code
    assert "No concepts have been extracted yet." in code
    assert "/graph?concept=${encodeURIComponent(concept.slug)}" in code
    for role, label in (("introduces", "Introduced here"), ("uses", "Used"),
                        ("assumes", "Assumed background")):
        assert f'["{role}", "{label}"]' in code
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in code, sink


def test_graph_inspector_shows_where_a_concept_appears() -> None:
    code = (JS_DIR / "graph_inspector.js").read_text(encoding="utf-8")
    assert "appearsIn(c.sources)" in code and "Appears in (" in code
    assert "`/sources/${encodeURIComponent(source.slug)}`" in code
    assert "No source is linked to this concept yet." in code


def test_graph_scripts_regenerate_inline_and_build_dom_safely() -> None:
    """The inspector's "Regenerate relations" asks inline (no browser dialog: see
    test_no_browser_dialogs_in_page_scripts) and every graph script builds DOM with dom.js."""
    inspector = (JS_DIR / "graph_inspector.js").read_text(encoding="utf-8")
    page = (JS_DIR / "graph.js").read_text(encoding="utf-8")
    assert '"Regenerate relations"' in inspector and "regenerateConfirm(c, ctx)" in inspector
    assert '"aria-expanded"' in inspector and 'event.key === "Escape"' in inspector
    assert "RELATE_MODELS_URL" in page and "relateBody(regenerate)" in page
    assert "showSearchNotice(data.semantic)" in page
    for path in [*JS_DIR.glob("graph*.js"), JS_DIR / "model_choice.js"]:
        code = path.read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in code, f"{path.name}: {sink}"
