"""Server-rendered HTML pages: home, the library, adding a source, a source, an artifact
(reader), a lab, the concept graph, the study history, and exploring a topic (`/explore`: the
form and the past explorations; `/explore/{id}`: one exploration to review, a 404 page for
another account's; `/explore/{id}/roadmap`: its roadmap, likewise). `/artifacts/{slug}` of a lab redirects to `/labs/{slug}`, and
`/labs/{slug}` of any other artifact to `/artifacts/{slug}`, so either link reaches the right
page.

The page routes only check that the slug exists, log that the page was opened (a study event:
`library_opened`, `study_page_opened`, `lab_opened`, `source_opened`, `graph_opened`; not for
a redirect or a 404) and render a skeleton; everything else is fetched by the page's JavaScript
from the JSON API (`/api/...`), so no API logic lives here. `/` (home: search, what to continue,
suggestions and a summary of the history), `/add` (the form to add a source; adding it is
logged by the API) and `/history` log nothing: looking at the history is not studying.
`/?q=<words>` opens home with that search. `/explore`, `/explore/{id}` and
`/explore/{id}/roadmap` log nothing either: planning is not studying.

Every template gets `nav`, the header link to mark as the current page (`aria-current`).
`base.html` shows a small "Local SQLite" badge in the header when the app runs on the local
SQLite database (`database_badge(request)`, a template global; nothing on Supabase).
Templates are in `teacher/web/templates/`, static files (css, js, vendored libraries) in
`teacher/web/static/`, served at `/static`.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from teacher.db.providers import create_db_provider
from teacher.discovery.common import DEFAULT_LEVEL, LEVEL_LABELS
from teacher.exploration.common import LIMITS
from teacher.labs import LAB_KIND
from teacher.managers import (
    ArtifactManager,
    ConceptManager,
    ExplorationManager,
    KnowledgeSourceManager,
    NotFoundError,
    StudyHistoryManager,
)
from teacher.managers.study_history import SESSION_IDLE_GAP
from teacher.web.deps import CurrentUserDep, SessionDep, SettingsDep

WEB_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
STATIC_URL = "/static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["static"] = lambda path: f"{STATIC_URL}/{path.lstrip('/')}"


def database_badge(request: Request) -> str | None:
    """What the header's "Local SQLite" badge names: the database's `describe()` when the app
    runs on the local SQLite file, None on Supabase (no badge there; nothing secret either way)."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError("the page templates need app.state.settings (set by create_app)")
    if settings.db != "sqlite":
        return None
    return create_db_provider(settings).describe()


templates.env.globals["database_badge"] = database_badge

router = APIRouter(tags=["pages"], include_in_schema=False)


class _RevalidatedStaticFiles(StaticFiles):
    """Static files that browsers must revalidate on every use.

    Without a Cache-Control header browsers cache scripts heuristically, so an edited module can
    be served stale next to fresh ones and the page fails to load. ETags keep revalidation cheap.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def static_files() -> StaticFiles:
    """The app mounts this at `STATIC_URL`."""
    return _RevalidatedStaticFiles(directory=STATIC_DIR)


def not_found(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "404.html", {"message": message, "nav": None}, status_code=404
    )


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request, q: str | None = None) -> HTMLResponse:
    """Home: the search box (filled with `q`, which the page then searches for), what to
    continue, what to study next and the recent study."""
    return templates.TemplateResponse(request, "home.html", {"nav": "home", "q": q or ""})


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request, session: SessionDep, settings: SettingsDep) -> HTMLResponse:
    """Every source with its study pages and labs."""
    StudyHistoryManager(session, settings).log("library_opened")
    return templates.TemplateResponse(request, "library.html", {"nav": "library"})


@router.get("/add", response_class=HTMLResponse)
def add_page(request: Request) -> HTMLResponse:
    """Add a source: a PDF from this computer or a web address, or one found by "Find
    sources" (`POST /api/sources/discover`); `levels` are the reader levels it offers."""
    return templates.TemplateResponse(
        request,
        "add.html",
        {"nav": "add", "levels": LEVEL_LABELS, "default_level": DEFAULT_LEVEL},
    )


@router.get("/explore", response_class=HTMLResponse)
def explore_page(request: Request) -> HTMLResponse:
    """Explore a topic: the form (with the caps a run keeps to) and the past explorations."""
    return templates.TemplateResponse(
        request,
        "explore.html",
        {
            "nav": "explore",
            "levels": LEVEL_LABELS,
            "default_level": DEFAULT_LEVEL,
            "limits": LIMITS,
        },
    )


@router.get("/explore/{exploration_id}", response_class=HTMLResponse)
def exploration_page(
    exploration_id: int,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> HTMLResponse:
    """One exploration to review (filled in by static/js/exploration.js)."""
    owner = user.id if user is not None else None
    try:
        row = ExplorationManager(session, settings, owner=owner).get(exploration_id)
    except NotFoundError:
        return not_found(request, f"There is no exploration {exploration_id}.")
    return templates.TemplateResponse(
        request,
        "exploration.html",
        {"exploration_id": row.id, "prompt": row.prompt, "nav": "explore", "limits": LIMITS},
    )


@router.get("/explore/{exploration_id}/roadmap", response_class=HTMLResponse)
def roadmap_page(
    exploration_id: int,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> HTMLResponse:
    """One exploration's roadmap (filled in by static/js/roadmap.js; a roadmap of an exploration
    that is not completed is the API's 409, which the page shows)."""
    owner = user.id if user is not None else None
    try:
        row = ExplorationManager(session, settings, owner=owner).get(exploration_id)
    except NotFoundError:
        return not_found(request, f"There is no exploration {exploration_id}.")
    return templates.TemplateResponse(
        request,
        "roadmap.html",
        {"exploration_id": row.id, "prompt": row.prompt, "nav": "explore"},
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    """The study history: sessions, their timelines, time per page."""
    minutes = int(SESSION_IDLE_GAP.total_seconds() // 60)
    return templates.TemplateResponse(
        request, "history.html", {"idle_gap_minutes": minutes, "nav": "history"}
    )


@router.get("/sources/{slug}", response_class=HTMLResponse)
def source_page(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> HTMLResponse:
    try:
        source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    except NotFoundError:
        return not_found(request, f"There is no source called “{slug}”.")
    StudyHistoryManager(session, settings).log("source_opened", source=source)
    return templates.TemplateResponse(
        request, "source.html", {"slug": source.slug, "title": source.title, "nav": None}
    )


@router.get("/artifacts/{slug}", response_class=HTMLResponse)
def artifact_page(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> Response:
    try:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
    except NotFoundError:
        return not_found(request, f"There is no study page called “{slug}”.")
    if artifact.kind == LAB_KIND:
        return RedirectResponse(f"/labs/{artifact.slug}", status_code=307)
    StudyHistoryManager(session, settings).log("study_page_opened", artifact=artifact)
    return templates.TemplateResponse(
        request,
        "artifact.html",
        {"slug": artifact.slug, "title": artifact.title, "status": artifact.status, "nav": None},
    )


@router.get("/labs/{slug}", response_class=HTMLResponse)
def lab_page(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> Response:
    try:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
    except NotFoundError:
        return not_found(request, f"There is no lab called “{slug}”.")
    if artifact.kind != LAB_KIND:
        return RedirectResponse(f"/artifacts/{artifact.slug}", status_code=307)
    StudyHistoryManager(session, settings).log("lab_opened", lab=artifact)
    return templates.TemplateResponse(
        request,
        "lab.html",
        {"slug": artifact.slug, "title": artifact.title, "status": artifact.status, "nav": None},
    )


@router.get("/graph", response_class=HTMLResponse)
def graph_page(
    request: Request, session: SessionDep, settings: SettingsDep, concept: str | None = None
) -> HTMLResponse:
    """The concept graph; `?concept=<slug>` opens it focused on that concept."""
    focus = None
    if concept is not None:
        try:
            focus = ConceptManager(session, settings).get_by_slug(concept)
        except NotFoundError:
            return not_found(request, f"There is no concept called “{concept}”.")
    StudyHistoryManager(session, settings).log("graph_opened", concept=focus)
    return templates.TemplateResponse(
        request,
        "graph.html",
        {
            "concept": focus.slug if focus else None,
            "concept_name": focus.name if focus else None,
            "nav": "graph",
        },
    )


__all__ = ["STATIC_DIR", "STATIC_URL", "TEMPLATES_DIR", "router", "static_files", "templates"]
