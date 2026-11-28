"""Explorations (`teacher.exploration`): start one, list them, read one, and the reader's edits.

Every route is under the account gate like every /api route. Explorations are private to the
account that started them (with the gate off, the one anonymous reader's): another account's
exploration is a 404, as for highlights. Every error body is `{"detail": "<message>"}`.
`<exploration>` is `teacher.exploration.view.exploration_detail` (its docstring and the code
name every field); a list item is `exploration_summary`.

GET /api/explorations/limits
    200 `{"limits": {...}, "levels": [{"id", "label"}], "default_level"}`: the caps a run keeps
    to (`teacher.exploration.common.LIMITS`; `price_estimate` is always null: there is none).

POST /api/explorations
    Body `{"topic": "<at most 300 characters>", "level": "intro" | "intermediate" |
    "advanced"}` (level defaults to intermediate; unknown keys 422). Checks that the run can
    work (Claude credentials, the embedder's key, an up-to-date concept embedding index: 503
    otherwise, before anything is spent), creates the exploration and runs it in the
    background (the app's job, like a page generation). 202 `<exploration>` (status
    `running`); poll `GET /api/explorations/{id}` for its progress. 422 a blank or too long
    topic or an unknown level; 503 no explorer in this app.

GET /api/explorations
    200 `[<summary>, ...]`, newest first. Each has `roadmap`: null unless the exploration is
    completed, else `teacher.exploration.roadmap.roadmap_summary` (`url`, `progress`,
    `current_step`, `current_step_title`, `next_action`).

GET /api/explorations/{id}
    200 `<exploration>`; 404 no such exploration of this account.

PATCH /api/explorations/{id}/concepts/{concept_id}
    Body `{"name"?: "<new name>", "dropped"?: true | false}` (at least one). Renames the concept
    or drops it from the plan (or takes it back). 200 `<exploration>`. 409 the exploration is
    not completed, or the concept is grounded; 422 a blank, too long or duplicate name.

PUT /api/explorations/{id}/steps
    Body `{"order": [3, 1, 2]}`: every current step number once, in the new order. 200
    `<exploration>`; 422 not a permutation, or an order that would put a prerequisite in the
    same or a later step than a concept (not dropped) that builds on it; 409 not completed.

PATCH /api/explorations/{id}/candidates/{candidate_id}
    Body `{"decision": "approved" | "proposed" | "rejected"}`: tick (approve) or untick
    (proposed) a candidate source, or reject it. 200 `<exploration>`; 409 not completed, or the
    candidate is already ingested.

The edits are stored and nothing else happens: no source is added and the graph is not touched.
They are refused (409) while an enrichment of the exploration runs.

Enrichment (`teacher.exploration.enrichment`): the approved sources ingested (concept extraction
only, no study page), the outline written to the concept graph, the concepts' grounding checked.

GET /api/explorations/{id}/enrichment/preview
    200 `<preview>`: what an enrichment would do now (`enrichment_preview`): the sources to add
    or link, the concepts to use or look up again (and create if absent), each prerequisite
    link with what is expected of it, `can_start` / `blocked`, and the plan's `digest`. Reads
    only. 404 no such exploration of this account.

POST /api/explorations/{id}/enrichments
    Body `{"digest": "<the preview's digest>"}`: the confirmation. Checks that the enrichment
    can work (Claude credentials, the embedder's key: 503 otherwise, before anything is spent),
    creates it and runs it in the background. 202 `<exploration>` (`enriching` true; poll `GET
    /api/explorations/{id}`: its `enrichment` has the stage, progress, error and, once
    completed, the summary). 409 the plan changed since the preview (look again), the
    exploration is not completed, or an enrichment of it runs already; 503 no enricher in this
    app; 422 no digest.

Roadmap (`teacher.exploration.roadmap`; `<roadmap>` is `roadmap_view`, whose docstring gives the
done rules and the order of the next action). Progress is computed on every read; nothing but
the reader's two choices per concept is stored. The roadmap of a running or failed exploration is
a 409. Like the suggestions, the reads first bring the review queue's items up to date
(`ReviewManager.sync`), so a weak concept's due questions are counted.

GET /api/explorations/{id}/roadmap
    200 `<roadmap>`; 404 no such exploration of this account; 409 not completed.

PATCH /api/explorations/{id}/roadmap/concepts/{concept_id}
    Body `{"primary_source"?: "<source slug>" | null, "marked_known"?: true | false}` (at least
    one). `primary_source`: study the concept from this one of its grounding sources (a library
    source that introduces its graph concept), or null to go back to the recommended one.
    `marked_known`: "I know this" (the concept counts as done), or false to take it back. 200
    `<roadmap>`. 422 a source that does not ground the concept (or is not in the library), or
    not a boolean; 409 not completed, or the concept is dropped; 404 no such concept of this
    exploration. Allowed while an enrichment runs (it changes nothing an enrichment touches).

POST /api/explorations/{id}/roadmap/concepts/{concept_id}/page
    Body `{"model"?: "<model id>"}` (as `POST /api/artifacts`). Generates a study page from the
    concept's primary source exactly as the source page's "Generate study page" does (the same
    code, `teacher.web.routers.artifacts.start_study_page`: the same checks, the same
    background generation, the same `study_page_requested` history event; the page's progress
    is shown on its own page). 202 `{"artifact": <artifact as GET /api/artifacts/{slug}>,
    "roadmap": <roadmap>}`. 409 the source has a study page already (generating or ready: the
    reply names it; open it instead: one page per source, shared by every concept it is the
    primary source of), the concept has no primary source (not grounded: find and enrich
    sources on the review page; or its chosen source no longer grounds it), it is dropped, or
    the exploration is not completed; 422 a model that is not offered. Nothing is ever generated
    without this request.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator
from sqlalchemy.orm import Session

from teacher.exploration import (
    LIMITS,
    ExplorationUnavailableError,
    enrichment_preview,
    exploration_detail,
    exploration_summary,
    roadmap_summary,
    roadmap_view,
    run_enrichment,
    run_exploration,
    start_enrichment,
    start_exploration,
)
from teacher.exploration.common import DEFAULT_LEVEL, LEVEL_LABELS
from teacher.config import Settings
from teacher.db.models import Exploration
from teacher.managers import ExplorationManager, KnowledgeSourceManager, NotFoundError
from teacher.managers.errors import StateConflictError
from teacher.managers.review import ReviewManager
from teacher.web.deps import CurrentUserDep, SessionDep, SettingsDep
from teacher.web.errors import _scrub_paths
from teacher.web.routers.artifacts import start_study_page

router = APIRouter(prefix="/api", tags=["explorations"])

NO_EXPLORER = (
    "Exploring a topic is not available in this app: it was started without an explorer "
    "(it needs a source finder and the concept graph's embedder)."
)
NO_ENRICHER = (
    "Enriching an exploration is not available in this app: it was started without an "
    "enricher (it needs concept extraction and the graph match's resolver)."
)


class ExplorationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str
    level: str = DEFAULT_LEVEL


class ConceptPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    dropped: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> ConceptPatch:
        if self.name is None and self.dropped is None:
            raise ValueError("give a new name, dropped, or both")
        return self


class StepsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: list[int]


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "proposed", "rejected"]


class EnrichIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str


class RoadmapConceptPatch(BaseModel):
    """`primary_source`: a source slug, or null for the recommended source; `marked_known`."""

    model_config = ConfigDict(extra="forbid")

    primary_source: str | None = None
    marked_known: StrictBool | None = None

    @model_validator(mode="after")
    def _something(self) -> RoadmapConceptPatch:
        given = self.model_fields_set & {"primary_source", "marked_known"}
        if not given:
            raise ValueError("give primary_source, marked_known, or both")
        if "marked_known" in given and self.marked_known is None:
            raise ValueError("marked_known must be true or false")
        return self


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None  # the model to generate with; the configured default if omitted


def _owner(user: Any) -> int | None:
    return user.id if user is not None else None


def _detail(request: Request, session: Any, settings: Any, row: Any) -> dict[str, Any]:
    data = exploration_detail(session, settings, row)
    root = settings.root
    if data.get("error"):
        data["error"] = _scrub_paths(data["error"], root)
    for stage in data["stages"]:
        if stage.get("error"):
            stage["error"] = _scrub_paths(stage["error"], root)
    enrichment = data.get("enrichment")
    if enrichment is not None:
        if enrichment.get("error"):
            enrichment["error"] = _scrub_paths(enrichment["error"], root)
        for stage in enrichment["stages"]:
            if stage.get("error"):
                stage["error"] = _scrub_paths(stage["error"], root)
        for item in (enrichment.get("summary") or {}).get("sources", {}).get("items", []):
            if item.get("error"):
                item["error"] = _scrub_paths(item["error"], root)
    for step in data["steps"]:
        for concept in step["concepts"]:
            for candidate in concept["candidates"]:
                if candidate.get("ingest_error"):
                    candidate["ingest_error"] = _scrub_paths(candidate["ingest_error"], root)
    return data


@router.get("/explorations/limits")
def exploration_limits() -> dict[str, Any]:
    """The caps of a run (shown before one starts); there is no price estimate."""
    return {
        "limits": LIMITS,
        "levels": [{"id": key, "label": label} for key, label in LEVEL_LABELS.items()],
        "default_level": DEFAULT_LEVEL,
    }


@router.post("/explorations", status_code=202)
def create_exploration(
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    body: ExplorationIn,
) -> JSONResponse:
    """Start an exploration in the background (see the module docstring)."""
    explorer = request.app.state.explorer
    if explorer is None:
        raise ExplorationUnavailableError(NO_EXPLORER)
    exploration_id = start_exploration(
        settings, explorer, body.topic, body.level, owner=_owner(user)
    )
    background.add_task(run_exploration, settings, exploration_id, explorer)
    row = ExplorationManager(session, settings, owner=_owner(user)).get(exploration_id)
    return JSONResponse(_detail(request, session, settings, row), status_code=202)


def _review_counts(request: Request, session: Session, settings: Settings) -> Any:
    """The review queue's counts by concept, after bringing its items up to date (as the
    suggestions do), and the time they are counted at."""
    now = request.app.state.clock()
    reviews = ReviewManager(session, settings)
    reviews.sync(max_attempts=request.app.state.max_attempts)
    return reviews.concept_counts(now=now), now


def _roadmap(
    request: Request, session: Session, settings: Settings, row: Exploration,
    counts: Any = None,
) -> dict[str, Any]:  # fmt: skip
    if counts is None:
        counts = _review_counts(request, session, settings)
    review_counts, now = counts
    return roadmap_view(
        session, settings, row, max_attempts=request.app.state.max_attempts, now=now,
        review_counts=review_counts,
    )  # fmt: skip


@router.get("/explorations")
def list_explorations(
    request: Request, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> list[dict[str, Any]]:
    manager = ExplorationManager(session, settings, owner=_owner(user))
    rows = manager.list()
    counts = (
        _review_counts(request, session, settings)
        if any(row.status == "completed" for row in rows)
        else None
    )
    out = []
    for row in rows:
        concepts = manager.concepts(row)
        item = exploration_summary(
            row, concepts=len(concepts), candidates=len(manager.candidates(row))
        )
        item["roadmap"] = (
            roadmap_summary(_roadmap(request, session, settings, row, counts))
            if row.status == "completed"
            else None
        )
        out.append(item)
    return out


@router.get("/explorations/{exploration_id}")
def get_exploration(
    request: Request,
    exploration_id: int,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    row = ExplorationManager(session, settings, owner=_owner(user)).get(exploration_id)
    return _detail(request, session, settings, row)


@router.patch("/explorations/{exploration_id}/concepts/{concept_id}")
def edit_concept(
    request: Request,
    exploration_id: int,
    concept_id: int,
    body: ConceptPatch,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    manager = ExplorationManager(session, settings, owner=_owner(user))
    row = manager.get(exploration_id)
    concept = manager.concept(row, concept_id)
    if body.name is not None:
        manager.rename_concept(row, concept, body.name)
    if body.dropped is not None:
        manager.set_dropped(row, concept, body.dropped)
    return _detail(request, session, settings, row)


@router.put("/explorations/{exploration_id}/steps")
def reorder_steps(
    request: Request,
    exploration_id: int,
    body: StepsIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    manager = ExplorationManager(session, settings, owner=_owner(user))
    row = manager.get(exploration_id)
    manager.reorder_steps(row, body.order)
    return _detail(request, session, settings, row)


@router.patch("/explorations/{exploration_id}/candidates/{candidate_id}")
def decide_candidate(
    request: Request,
    exploration_id: int,
    candidate_id: int,
    body: DecisionIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    manager = ExplorationManager(session, settings, owner=_owner(user))
    row = manager.get(exploration_id)
    manager.decide(row, manager.candidate(row, candidate_id), body.decision)
    return _detail(request, session, settings, row)


@router.get("/explorations/{exploration_id}/enrichment/preview")
def preview_enrichment(
    exploration_id: int, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> dict[str, Any]:
    """What an enrichment would do now (see the module docstring). Reads only."""
    row = ExplorationManager(session, settings, owner=_owner(user)).get(exploration_id)
    return enrichment_preview(session, settings, row)


@router.post("/explorations/{exploration_id}/enrichments", status_code=202)
def enrich_exploration(
    request: Request,
    background: BackgroundTasks,
    exploration_id: int,
    body: EnrichIn,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> JSONResponse:
    """Start the confirmed enrichment in the background (see the module docstring)."""
    enricher = request.app.state.enricher
    if enricher is None:
        why = getattr(request.app.state, "enricher_unavailable", None)
        raise ExplorationUnavailableError(NO_ENRICHER + (f" The default one was not built: {why}." if why else ""))
    # In sessions of its own (committed), before this request's session reads anything, so the
    # reply shows the new enrichment. Another account's exploration is a 404 there.
    enrichment_id = start_enrichment(
        settings, enricher, exploration_id, owner=_owner(user), digest=body.digest
    )
    background.add_task(run_enrichment, settings, enrichment_id, enricher)
    row = ExplorationManager(session, settings, owner=_owner(user)).get(exploration_id)
    return JSONResponse(_detail(request, session, settings, row), status_code=202)


# -- the roadmap ----------------------------------------------------------------------------


@router.get("/explorations/{exploration_id}/roadmap")
def get_roadmap(
    request: Request,
    exploration_id: int,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """The roadmap (see the module docstring)."""
    row = ExplorationManager(session, settings, owner=_owner(user)).get(exploration_id)
    return _roadmap(request, session, settings, row)


@router.patch("/explorations/{exploration_id}/roadmap/concepts/{concept_id}")
def edit_roadmap_concept(
    request: Request,
    exploration_id: int,
    concept_id: int,
    body: RoadmapConceptPatch,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """Choose the concept's primary source, or mark it "I know this" (see the module
    docstring)."""
    manager = ExplorationManager(session, settings, owner=_owner(user))
    row = manager.get(exploration_id)
    concept = manager.concept(row, concept_id)
    if "primary_source" in body.model_fields_set:
        source = None
        if body.primary_source is not None:
            try:
                source = KnowledgeSourceManager(session, settings).get_by_slug(body.primary_source)
            except NotFoundError:
                raise ValueError(
                    f"there is no source {body.primary_source!r} in the library; choose one of "
                    f"the sources that ground {concept.name!r}"
                ) from None
        manager.set_primary_source(row, concept, source)
    if body.marked_known is not None:
        manager.set_marked_known(row, concept, body.marked_known)
    return _roadmap(request, session, settings, row)


@router.post("/explorations/{exploration_id}/roadmap/concepts/{concept_id}/page", status_code=202)
def generate_roadmap_page(
    request: Request,
    background: BackgroundTasks,
    exploration_id: int,
    concept_id: int,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUserDep,
    body: Annotated[PageIn | None, Body()] = None,
) -> JSONResponse:
    """Generate a study page from the concept's primary source (see the module docstring)."""
    manager = ExplorationManager(session, settings, owner=_owner(user))
    row = manager.get(exploration_id)
    concept = manager.concept(row, concept_id)
    if concept.status == "dropped":
        raise StateConflictError(
            f"concept {concept.name!r} is dropped: it is not on the roadmap (restore it on the "
            "review page first)"
        )
    roadmap = _roadmap(request, session, settings, row)
    view = next(c for step in roadmap["steps"] for c in step["concepts"] if c["id"] == concept.id)
    if view["page"] is not None:
        page = view["page"]
        raise StateConflictError(
            f"“{view['primary_source']['title']}” has a study page already: “{page['title']}” "
            f"({page['url']}, {page['status']}); open it instead of generating another"
        )
    if view["primary_source"] is None:
        if view["choice_problem"]:
            raise StateConflictError(f"{concept.name!r} has no source to study it from: {view['choice_problem']}")
        raise StateConflictError(
            f"no source in your library introduces {concept.name!r}, so there is nothing to "
            "generate a page from: find and tick sources for it on the review page, then enrich"
        )
    source = KnowledgeSourceManager(session, settings).get_by_slug(view["primary_source"]["slug"])
    artifact = start_study_page(
        request, background, session, settings, [source],
        model=body.model if body is not None else None,
    )  # fmt: skip
    return JSONResponse(
        {"artifact": artifact.model_dump(), "roadmap": _roadmap(request, session, settings, row)},
        status_code=202,
    )


__all__ = [
    "ConceptPatch",
    "DecisionIn",
    "EnrichIn",
    "ExplorationIn",
    "PageIn",
    "RoadmapConceptPatch",
    "StepsIn",
    "router",
]
