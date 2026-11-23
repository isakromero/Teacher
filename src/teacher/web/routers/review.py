"""Review queue endpoints (`teacher.managers.review` has the rules, `teacher.review_bank` the
variants).

- `GET /api/review/queue?tz=<IANA zone>&cap=20&limit=1&concept=<slug>` -> the queue now:
  `{now, tz, day_start, cap, done_today, remaining_today, due, items_total, next_due_at,
  concept, items}`. Reading it first derives the items of newly cleared questions. `due` counts
  every due item; `items` are the first `limit` (0 to 50) of them in the order they are asked,
  but no more than `remaining_today` (`cap` minus the reviews done since the start of the
  reader's day in `tz`; a higher `cap` extends the day). Each item carries its presentation:
  `block` (shaped like an entry of GET /api/artifacts/{slug}/blocks, public content only, the
  options already in the order shown, `state` "open"), `presentation` (the token to answer
  with), `form` ("original" or "variant") and `variant` (its number). `concept` narrows the
  queue to questions tagged `main` with that concept. `tz` is required (422 if unknown).
- `POST /api/review/items/{id}/answers` body `{presentation, response, confidence}` -> 201,
  the graded review: `{verdict, passed, feedback, missing, confidence, details, state
  ("passed" or "revealed": one attempt per review), attempt (1), revealed (the answer, in the
  order shown), interval_days, due_at, reviews, form, variant}`. `response` is `{"choice":
  <position shown>}` or `{"text": ...}`; `confidence` ("low", "medium", "high") is required.
  409 when the item was not served with that token (answered, or shown again since), its
  variant was rebuilt, or its page left the queue; 422 for a malformed answer (nothing is
  recorded); 503 when grading is unavailable.
- `GET /api/review/banks` -> `{can_build, cannot_build_reason, pages}`: the review bank of every
  page with review items (`teacher.review_bank.bank_status`, with `artifact`).
- `GET /api/artifacts/{slug}/review-bank` -> one page's bank status (with `can_build`).
- `POST /api/artifacts/{slug}/review-bank` -> builds (or rebuilds) the page's variants now, one
  model call with the artifact's generation model, recorded as a `review_bank` run; returns the
  new status. 503 when the server has no bank builder (an offline generator) or the model is
  unavailable, 502 for an unusable reply (it breaks the rules: the message lists how), 409 while
  a build is running, 422 for a lab, a page that is not ready, or one without questions. A
  failed build leaves the page and its existing variants unchanged.

Time comes from `app.state.clock` (naive UTC), so the queue can be checked with time moved on.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import generation_model
from teacher.grading import Confidence, Verdict
from teacher.labs import LAB_KIND
from teacher.managers import ArtifactManager, BlockConceptManager, ConceptManager, NotFoundError
from teacher.managers.review import (
    DEFAULT_DAILY_CAP,
    MAX_DAILY_CAP,
    ItemView,
    ReviewConflict,
    ReviewManager,
)
from teacher.review_bank import (
    ReviewBankBusyError,
    ReviewBankError,
    ReviewBankInputError,
    ReviewBankOutputError,
    ReviewBankUnavailableError,
    bank_status,
    build_review_bank,
)
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.errors import _scrub_paths
from teacher.web.routers.reading import GradersDep, MaxAttemptsDep

router = APIRouter(prefix="/api", tags=["review"])

NO_BUILDER_REASON = (
    "This server has no review bank builder configured (it runs with an offline generator), "
    "so variants cannot be built here."
)


def get_clock(request: Request) -> Callable[[], datetime]:
    return request.app.state.clock


ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]


def get_bank_builder(request: Request) -> Any:
    return request.app.state.review_bank_builder


BankBuilderDep = Annotated[Any, Depends(get_bank_builder)]


# -- models ---------------------------------------------------------------------------------


class RefOut(BaseModel):
    slug: str
    title: str


class ConceptRefOut(BaseModel):
    slug: str
    name: str


class ReviewBlockOut(BaseModel):
    key: str
    type: str
    is_gate: bool
    content: dict[str, Any]
    state: Literal["open"]
    attempts: int
    revealed: None
    segments: list[Any]
    concepts: list[Any]


class ReviewItemOut(BaseModel):
    id: int
    presentation: str
    form: Literal["original", "variant"]
    variant: int | None
    variants_available: int
    block: ReviewBlockOut
    artifact: RefOut
    question_key: str
    due_at: str
    overdue_days: float
    confident_error: bool
    reviews: int
    interval_days: float
    last_verdict: Verdict
    last_confidence: Confidence | None
    concepts: list[ConceptRefOut]


class QueueOut(BaseModel):
    now: str
    tz: str
    day_start: str
    cap: int
    done_today: int
    remaining_today: int
    due: int
    items_total: int
    next_due_at: str | None
    concept: ConceptRefOut | None
    items: list[ReviewItemOut]


class ReviewAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presentation: str
    response: dict[str, Any]
    confidence: Confidence


class ReviewAnswerOut(BaseModel):
    attempt: int
    passed: bool
    verdict: Verdict
    feedback: str | None
    missing: list[str]
    confidence: Confidence
    details: dict[str, Any] | None
    state: Literal["passed", "revealed"]
    revealed: dict[str, Any] | None
    interval_days: float
    due_at: str
    reviews: int
    form: Literal["original", "variant"]
    variant: int | None


class BankRunOut(BaseModel):
    id: int
    status: str
    model: str
    started_at: str
    finished_at: str | None
    error: str | None


class BankStatusOut(BaseModel):
    artifact: RefOut
    status: Literal["built", "building", "failed", "interrupted", "not_built", "no_questions"]
    questions: int
    variants: int
    questions_with_variants: int
    built_at: str | None
    run: BankRunOut | None
    reason: str
    can_build: bool
    cannot_build_reason: str | None


class BanksOut(BaseModel):
    can_build: bool
    cannot_build_reason: str | None
    pages: list[BankStatusOut]


# -- helpers --------------------------------------------------------------------------------


def day_start(now: datetime, tz: str) -> datetime:
    """The start of the reader's day containing `now` (naive UTC), as naive UTC; ValueError
    (422) for an unknown zone."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {tz!r}") from exc
    local = now.replace(tzinfo=UTC).astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


@contextmanager
def _conflicts_as_409() -> Iterator[None]:
    try:
        yield
    except ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _item_out(
    session: Session, settings: Settings, view: ItemView, rng: random.Random, now: datetime
) -> ReviewItemOut:
    manager = ReviewManager(session, settings)
    shown = manager.present(view, rng, now=now)
    concepts = [
        ConceptRefOut(slug=c.slug, name=c.name)
        for tag, c in BlockConceptManager(session, settings).for_blocks([view.block.id]).get(
            view.block.id, []
        )
        if tag.role == "main"
    ]
    item = view.item
    return ReviewItemOut(
        id=item.id,
        presentation=shown.token,
        form=shown.form,  # type: ignore[arg-type]
        variant=shown.variant_position,
        variants_available=len(manager.variants(view.block)),
        block=ReviewBlockOut(
            key=f"review-{item.id}-{shown.token[:8]}",
            type=view.block.type,
            is_gate=True,
            content=shown.shown,
            state="open",
            attempts=0,
            revealed=None,
            segments=[],
            concepts=[],
        ),
        artifact=RefOut(slug=view.artifact.slug, title=view.artifact.title),
        question_key=view.block.key,
        due_at=to_iso(item.due_at) or "",
        overdue_days=max((now - item.due_at).total_seconds() / 86400, 0.0),
        confident_error=view.confident_error,
        reviews=item.reviews,
        interval_days=item.interval_days,
        last_verdict=item.last_verdict,  # type: ignore[arg-type]
        last_confidence=item.last_confidence,  # type: ignore[arg-type]
        concepts=concepts,
    )


def _status_out(
    session: Session, settings: Settings, artifact: Any, builder: Any, now: datetime
) -> BankStatusOut:
    status = bank_status(session, settings, artifact, now=now)
    run = status["run"]
    return BankStatusOut(
        artifact=RefOut(slug=artifact.slug, title=artifact.title),
        status=status["status"],
        questions=status["questions"],
        variants=status["variants"],
        questions_with_variants=status["questions_with_variants"],
        built_at=to_iso(status["built_at"]),
        run=None if run is None else BankRunOut(
            id=run["id"],
            status=run["status"],
            model=run["model"],
            started_at=to_iso(run["started_at"]) or "",
            finished_at=to_iso(run["finished_at"]),
            error=_scrub_paths(run["error"], settings.root) if run["error"] else None,
        ),
        reason=_scrub_paths(status["reason"], settings.root),
        can_build=builder is not None,
        cannot_build_reason=None if builder is not None else NO_BUILDER_REASON,
    )


# -- endpoints ------------------------------------------------------------------------------


@router.get("/review/queue")
def get_queue(
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    clock: ClockDep,
    tz: Annotated[str, Query(min_length=1)],
    cap: Annotated[int, Query(ge=1, le=MAX_DAILY_CAP)] = DEFAULT_DAILY_CAP,
    limit: Annotated[int, Query(ge=0, le=50)] = 1,
    concept: Annotated[str | None, Query()] = None,
) -> QueueOut:
    """The due questions, in the order they are asked (see the module docstring)."""
    now = clock()
    start = day_start(now, tz)
    focus = ConceptManager(session, settings).get_by_slug(concept) if concept else None
    manager = ReviewManager(session, settings)
    manager.sync(max_attempts=max_attempts)
    queue = manager.queue(now=now, day_start=start, cap=cap, concept=focus)
    rng = random.SystemRandom()
    return QueueOut(
        now=to_iso(now) or "",
        tz=tz,
        day_start=to_iso(start) or "",
        cap=cap,
        done_today=queue.done_today,
        remaining_today=queue.remaining_today,
        due=queue.due,
        items_total=queue.items_total,
        next_due_at=to_iso(queue.next_due_at),
        concept=ConceptRefOut(slug=focus.slug, name=focus.name) if focus else None,
        items=[_item_out(session, settings, v, rng, now) for v in queue.served(limit)],
    )


@router.post("/review/items/{item_id}/answers", status_code=201)
def answer_item(
    item_id: int,
    body: ReviewAnswerIn,
    session: SessionDep,
    settings: SettingsDep,
    graders: GradersDep,
    clock: ClockDep,
) -> ReviewAnswerOut:
    """Grade one review answer and reschedule the question."""
    with _conflicts_as_409():
        outcome = ReviewManager(session, settings).answer(
            item_id,
            presentation=body.presentation,
            response=body.response,
            confidence=body.confidence,
            graders=graders,
            now=clock(),
        )
    attempt = outcome.attempt
    item = outcome.view.item
    return ReviewAnswerOut(
        attempt=1,
        passed=outcome.result.passed,
        verdict=outcome.result.verdict,
        feedback=attempt.feedback,
        missing=list(attempt.missing),
        confidence=attempt.confidence,  # type: ignore[arg-type]
        details=attempt.details,
        state="passed" if outcome.result.passed else "revealed",
        revealed=outcome.revealed,
        interval_days=item.interval_days,
        due_at=to_iso(item.due_at) or "",
        reviews=item.reviews,
        form=attempt.form,  # type: ignore[arg-type]
        variant=attempt.variant_position,
    )


@router.get("/review/banks")
def list_banks(
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    clock: ClockDep,
    builder: BankBuilderDep,
) -> BanksOut:
    """The review bank of every page that has review items, newest page first."""
    manager = ReviewManager(session, settings)
    manager.sync(max_attempts=max_attempts)
    now = clock()
    return BanksOut(
        can_build=builder is not None,
        cannot_build_reason=None if builder is not None else NO_BUILDER_REASON,
        pages=[
            _status_out(session, settings, artifact, builder, now)
            for artifact, _count in manager.pages_with_items()
        ],
    )


@router.get("/artifacts/{slug}/review-bank")
def get_bank(
    slug: str, session: SessionDep, settings: SettingsDep, clock: ClockDep, builder: BankBuilderDep
) -> BankStatusOut:
    """One page's review bank (422 for a lab, which has none)."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    if artifact.kind == LAB_KIND:
        raise HTTPException(status_code=422, detail=f"{slug!r} is a lab, which has no review bank")
    return _status_out(session, settings, artifact, builder, clock())


@router.post("/artifacts/{slug}/review-bank")
def rebuild_bank(
    slug: str,
    request: Request,
    settings: SettingsDep,
    clock: ClockDep,
    builder: BankBuilderDep,
) -> BankStatusOut:
    """Build or rebuild the page's variants now (one model call; no session open during it)."""
    if builder is None:
        raise HTTPException(status_code=503, detail=NO_BUILDER_REASON)
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        model = generation_model(request.app.state.generator, dict(artifact.params or {}))
    try:
        build_review_bank(settings, slug, builder, model=model, trigger="on_demand")
    except ReviewBankError as exc:
        status = {
            ReviewBankInputError: 422,
            ReviewBankBusyError: 409,
            ReviewBankUnavailableError: 503,
            ReviewBankOutputError: 502,
        }.get(type(exc), 500)
        raise HTTPException(
            status_code=status, detail=_scrub_paths(str(exc), settings.root)
        ) from exc
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        return _status_out(session, settings, artifact, builder, clock())


# -- the page -------------------------------------------------------------------------------

page_router = APIRouter(tags=["pages"], include_in_schema=False)


@page_router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request, session: SessionDep, settings: SettingsDep, concept: str | None = None
) -> HTMLResponse:
    """The review queue, one question at a time; `?concept=<slug>` reviews only that concept.
    Logs nothing (the answers are the record)."""
    from teacher.web.routers.pages import not_found, templates

    focus = None
    if concept is not None:
        try:
            focus = ConceptManager(session, settings).get_by_slug(concept)
        except NotFoundError:
            return not_found(request, f"There is no concept called “{concept}”.")
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "nav": "review",
            "concept": focus.slug if focus else None,
            "concept_name": focus.name if focus else None,
        },
    )


__all__ = ["BankStatusOut", "QueueOut", "ReviewAnswerIn", "ReviewAnswerOut", "day_start", "page_router", "router"]
