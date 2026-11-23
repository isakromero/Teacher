"""Citations: what each knowledge source cites, and the reading frontier.

- `GET /api/sources/{slug}/references`: the state of the source's reference extraction, its
  references (each with the library source it matched, or the best address to add it from),
  and `cited_by`, the library sources whose references matched this source.
- `POST /api/sources/{slug}/references/extract` (202): claim a run and extract in the
  background; 409 while a run is going, 503 when the app has no reference extractor.
- `GET /api/references/frontier`: the cited works that are not in the library, grouped across
  sources and ranked by how many library sources cite them.

`after_source_added` and `rematch_library` are called by the sources router: every change to
the library re-runs matching for all stored references (`teacher.managers.source_references`),
and a new source's references are extracted in the background (`teacher.reference_extraction`).
A failed extraction is recorded and shown; it never undoes the source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import KnowledgeSource, SourceReference, utcnow
from teacher.managers import KnowledgeSourceManager
from teacher.managers.source_references import (
    ExtractionBusyError,
    FrontierItem,
    RematchResult,
    SourceReferenceManager,
    add_url,
    extraction_state,
)
from teacher.reference_extraction import claim_extraction, run_claimed_quietly
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["references"])

NO_EXTRACTOR = (
    "References cannot be extracted on this server: it was started without a reference "
    "extractor (an app built with an offline generator has none unless one is passed)."
)
FRONTIER_LIMIT = 500


# -- response models ----------------------------------------------------------------------


class ExtractionOut(BaseModel):
    """The source's reference extraction. `status`: never_run (no run yet), running,
    interrupted (a run that never finished: the server stopped during it), done (the latest run
    succeeded), failed (`error` says why). `succeeded_at`, `found_in` (reference_list |
    inline_citations | none) and `reference_count` describe the stored references, from the
    latest run that succeeded (null until one has); after a failed run they may still describe
    an earlier one. `available`: whether this server can run an extraction at all."""

    status: Literal["never_run", "running", "interrupted", "done", "failed"]
    available: bool
    unavailable_reason: str | None
    run_number: int | None
    trigger: str | None
    extractor: str | None
    model: str | None
    started_at: str | None
    finished_at: str | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    succeeded_at: str | None
    found_in: str | None
    reference_count: int | None


class MatchOut(BaseModel):
    """The library source a reference matched, and how (doi | arxiv | title)."""

    slug: str
    title: str
    method: str


class ReferenceOut(BaseModel):
    """One reference as extracted. `match` is null when the work is not in the library; then
    `add_url` is the best address to add it from (`add_url_kind`: arxiv (the arXiv PDF), doi
    (doi.org) or url (printed in the reference)), null when none is known."""

    position: int
    raw_text: str
    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    url: str | None
    match: MatchOut | None
    add_url: str | None
    add_url_kind: str | None


class CitingOut(BaseModel):
    """A library source citing this one; `positions` are its references that matched."""

    slug: str
    title: str
    positions: list[int]


class SourceReferencesOut(BaseModel):
    slug: str
    title: str
    extraction: ExtractionOut
    references: list[ReferenceOut]
    cited_by: list[CitingOut]


class SourceLinkOut(BaseModel):
    slug: str
    title: str


class FrontierItemOut(BaseModel):
    """One cited work not in the library. The details are those of the reference that says the
    most about it (each missing one filled from the others). `citing_count` library sources
    cite it, in `reference_count` references."""

    key: str
    title: str | None
    raw_text: str
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    arxiv_id: str | None
    url: str | None
    add_url: str | None
    add_url_kind: str | None
    citing_count: int
    reference_count: int
    cited_by: list[SourceLinkOut]


class CoverageOut(BaseModel):
    """How many library sources are in each extraction state: the frontier only knows what the
    `done` (and earlier succeeded) extractions found."""

    sources: int
    never_run: int
    running: int
    interrupted: int
    done: int
    failed: int


class FrontierOut(BaseModel):
    items: list[FrontierItemOut]
    total: int
    coverage: CoverageOut


# -- helpers --------------------------------------------------------------------------------


def _extractor(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.reference_extractor


def extraction_out(
    manager: SourceReferenceManager, source: KnowledgeSource, *, available: bool, now: datetime
) -> ExtractionOut:
    row = manager.extraction(source)
    values: dict[str, object] = {
        "status": extraction_state(row, now),
        "available": available,
        "unavailable_reason": None if available else NO_EXTRACTOR,
    }
    names = ("run_number", "trigger", "extractor", "model", "error", "input_tokens",
             "output_tokens", "found_in", "reference_count")  # fmt: skip
    for name in names:
        values[name] = getattr(row, name) if row is not None else None
    for name in ("started_at", "finished_at", "succeeded_at"):
        values[name] = to_iso(getattr(row, name)) if row is not None else None
    return ExtractionOut(**values)


def _add(ref: SourceReference) -> tuple[str | None, str | None]:
    found = add_url(doi=ref.doi, arxiv_id=ref.arxiv_id, url=ref.url)
    return found if found is not None else (None, None)


def references_out(
    session: Session, settings: Settings, source: KnowledgeSource, *, available: bool
) -> SourceReferencesOut:
    manager = SourceReferenceManager(session, settings)
    refs = manager.references(source)
    matched = manager.sources_by_id(r.matched_source_id for r in refs if r.matched_source_id)
    out = []
    for ref in refs:
        target = matched.get(ref.matched_source_id) if ref.matched_source_id else None
        match = None
        if target is not None:
            if ref.match_method is None:
                raise RuntimeError(f"reference {ref.id} is matched but has no match method")
            match = MatchOut(slug=target.slug, title=target.title, method=ref.match_method)
        link, kind = (None, None) if match is not None else _add(ref)
        out.append(
            ReferenceOut(
                position=ref.position, raw_text=ref.raw_text, title=ref.title,
                authors=list(ref.authors or []), year=ref.year, venue=ref.venue, doi=ref.doi,
                arxiv_id=ref.arxiv_id, url=ref.url, match=match, add_url=link,
                add_url_kind=kind,
            )  # fmt: skip
        )
    return SourceReferencesOut(
        slug=source.slug,
        title=source.title,
        extraction=extraction_out(manager, source, available=available, now=utcnow()),
        references=out,
        cited_by=[
            CitingOut(slug=s.slug, title=s.title, positions=positions)
            for s, positions in manager.cited_by(source)
        ],
    )


def frontier_item_out(item: FrontierItem) -> FrontierItemOut:
    best = item.best
    doi = item.first_value("doi")
    arxiv_id = item.first_value("arxiv_id")
    url = item.first_value("url")
    found = add_url(doi=doi, arxiv_id=arxiv_id, url=url)  # type: ignore[arg-type]
    link, kind = found if found is not None else (None, None)
    return FrontierItemOut(
        key=item.key,
        title=item.first_value("title"),  # type: ignore[arg-type]
        raw_text=best.raw_text,
        authors=list(item.first_value("authors") or []),  # type: ignore[call-overload]
        year=item.first_value("year"),  # type: ignore[arg-type]
        venue=item.first_value("venue"),  # type: ignore[arg-type]
        doi=doi,  # type: ignore[arg-type]
        arxiv_id=arxiv_id,  # type: ignore[arg-type]
        url=url,  # type: ignore[arg-type]
        add_url=link,
        add_url_kind=kind,
        citing_count=len(item.citing),
        reference_count=len(item.references),
        cited_by=[SourceLinkOut(slug=s.slug, title=s.title) for s in item.citing],
    )


def rematch_library(session: Session, settings: Settings) -> RematchResult:
    """Re-run matching for every stored reference (after any change to the library)."""
    return SourceReferenceManager(session, settings).rematch_all()


def after_source_added(
    request: Request,
    background: BackgroundTasks,
    session: Session,
    settings: Settings,
    source: KnowledgeSource,
) -> None:
    """For a source just added in `session`: re-match every stored reference (old references
    may cite it), then, when the app has a reference extractor, claim a run in the same
    transaction and extract in the background after the response (the request's session is
    committed by then). Without an extractor the source's extraction stays "never_run"."""
    rematch_library(session, settings)
    extractor = _extractor(request)
    if extractor is None:
        return
    claim = claim_extraction(session, settings, source, extractor, trigger="after_create")
    background.add_task(run_claimed_quietly, settings, claim, extractor)


# -- endpoints ----------------------------------------------------------------------------


@router.get("/sources/{slug}/references")
def get_source_references(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> SourceReferencesOut:
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    return references_out(
        session, settings, source, available=_extractor(request) is not None
    )


@router.post("/sources/{slug}/references/extract", status_code=202)
def extract_source_references(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
) -> SourceReferencesOut:
    """Start extracting the source's references in the background; the reply shows the run as
    `running`. Poll `GET /api/sources/{slug}/references` for the outcome."""
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    extractor = _extractor(request)
    if extractor is None:
        raise HTTPException(503, NO_EXTRACTOR)
    try:
        claim = claim_extraction(session, settings, source, extractor, trigger="on_demand")
    except ExtractionBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    out = references_out(session, settings, source, available=True)
    session.commit()  # the background job reads the claim through its own sessions
    background.add_task(run_claimed_quietly, settings, claim, extractor)
    return out


@router.get("/references/frontier")
def get_frontier(
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=FRONTIER_LIMIT)] = 50,
) -> FrontierOut:
    """The reading frontier: cited works not in the library, most-cited first (`limit` of
    `total`), with how many sources have had their references extracted."""
    manager = SourceReferenceManager(session, settings)
    items = manager.frontier()
    counts = manager.counts()
    return FrontierOut(
        items=[frontier_item_out(item) for item in items[:limit]],
        total=len(items),
        coverage=CoverageOut(**{name: counts[name] for name in CoverageOut.model_fields}),
    )


__all__ = [
    "NO_EXTRACTOR",
    "after_source_added",
    "rematch_library",
    "router",
]
