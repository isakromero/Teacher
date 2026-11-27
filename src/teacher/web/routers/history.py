"""Study history endpoints: the sessions and their timelines, per-artifact totals, the pages'
heartbeats and the events only a page sees, and the export (see
`teacher.managers.study_history` for the rules).

- `GET /api/history/sessions?limit=20&offset=0&artifact=<slug>&source=<slug>&since=&until=`
  -> `{total, limit, offset, sessions}`, newest first. `artifact` keeps the sessions with
  activity on that study page (or its labs) or lab, `source` those on that paper's pages, labs
  or source page; `since`/`until` are ISO 8601 datetimes (no offset means UTC) and keep the
  sessions overlapping [since, until).
- `GET /api/history/sessions/{id}` -> the session with its `timeline` (events, answers, chat
  answers, chat messages, lab runs and attention spans, in time order). Blocks are named by key
  and `block_label` (heading or opening words); an answer has its raw `response` and `answer`,
  what the reader answered in words (the chosen option's text, the written text, or for a
  shape the history does not know the raw JSON with `form: "raw"` and a `note`).
- `GET /api/history/artifacts` -> the totals of every artifact; `.../artifacts/{slug}` one.
- `POST /api/history/heartbeat` body `{"surface", "slug", "block", "resumed"}` -> the attention
  span it extended or opened. `slug` names the study page, lab or source (none for the graph);
  `block` is the key of the study-page block in view (optional); `resumed` (default false) says
  the reader is back after a pause the page saw, so the time since the last heartbeat is not
  credited.
- `POST /api/history/events` body `{"kind", "artifact", "lab", "block", "concept", "source",
  "payload"}` (references by slug, `block` by key on `artifact`) -> 201, the stored event.
  Only the kinds a page reports itself are accepted (`CLIENT_EVENT_KINDS`); the rest are
  logged by the server and are a 422 here.
- `GET /api/history/export?format=json|markdown` -> the whole history as a download.
- `GET /api/history/concepts` -> `{thresholds, concepts}`: every concept's standing, computed
  from the answers to and the time on the blocks tagged with it
  (`teacher.managers.concept_standing`: status, first-attempt pass rate, attempts, time, last
  studied, the pages that cover it), by slug.
- `GET /api/history/concepts/{slug}` -> one concept's standing with `questions`: every question
  tagged with it (main first), with its page, attempts and verdicts. The question's text is
  given only once the question is visible to the reader, and the tagger's rationale (which can
  hint at the answer) only once it is cleared; locked questions are named by page and key only.
- `GET /api/history/suggestions?limit=` -> `{rules, total, suggestions}`: what to study next,
  ranked (`teacher.suggestions`: fill the gap, review, ready next), each with its reason, its
  evidence and an action (study pages, sources, the review queue for a review suggestion
  whose concept has questions there, or nothing in the library).
- `GET /api/history/summary?tz=<IANA zone>&sessions=5` -> `{tz, now, today, week,
  total_sessions, recent_sessions}` for the home page: `today` and `week` (from the start of
  the reader's day, and of their week, which starts on Monday, in `tz`) are `{since,
  active_seconds, sessions, answers, questions, first_attempts, first_attempts_passed,
  first_attempt_pass_rate}` (`StudyHistoryManager.period_totals`); `recent_sessions` are the
  newest `sessions` sessions, as in the list. `tz` is required: an unknown zone is a 422.
- `GET /api/history/continue?limit=3` -> `{items}`: the study pages and labs studied most
  recently, newest first, each `{artifact (as GET /api/artifacts/{slug} gives it, with its
  progress), url, last_studied_at, active_seconds}` (`StudyHistoryManager.recently_studied`).

Unknown slugs, keys and sessions are 404s; an unknown kind or surface, a reference a kind does
not take, or a payload with missing or extra keys is a 422. Nothing is dropped quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from teacher.db.models import ATTENTION_SURFACES, utcnow
from teacher.history_export import history_markdown
from teacher.labs import LAB_KIND, LabKindError, require_lab
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    ConceptManager,
    ConceptStandingManager,
    KnowledgeSourceManager,
    StudyHistoryManager,
)
from teacher.managers.concept_standing import thresholds as standing_thresholds
from teacher.managers.review import ReviewManager
from teacher.managers.study_history import MAX_SESSION_PAGE
from teacher.reading import reading_state
from teacher.suggestions import rules as suggestion_rules
from teacher.suggestions import suggestions as rank_suggestions
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.artifacts import ArtifactOut, artifact_out
from teacher.web.routers.reading import MaxAttemptsDep

router = APIRouter(prefix="/api/history", tags=["history"])

Surface = Literal["study_page", "lab", "source", "graph"]
assert set(Surface.__args__) == set(ATTENTION_SURFACES)  # type: ignore[attr-defined]


# -- models -----------------------------------------------------------------------------------


class ArtifactRefOut(BaseModel):
    slug: str
    title: str
    kind: str


class SourceRefOut(BaseModel):
    slug: str
    title: str


class ConceptRefOut(BaseModel):
    slug: str
    name: str


class SessionOut(BaseModel):
    id: int
    started_at: str
    last_activity_at: str
    backfilled: bool  # derived from older activity by the migration, not recorded live
    active_seconds: float
    events: int
    answers: int
    answers_passed: int
    chat_answers: int
    chat_answers_passed: int
    user_messages: int
    tutor_messages: int
    lab_runs: int
    lab_checks: int
    lab_checks_passed: int
    artifacts: list[ArtifactRefOut]
    sources: list[SourceRefOut]


class SessionsOut(BaseModel):
    total: int
    limit: int
    offset: int
    sessions: list[SessionOut]


class EventEntryOut(BaseModel):
    type: Literal["event"]
    at: str
    id: int
    kind: str
    artifact: ArtifactRefOut | None
    lab: ArtifactRefOut | None
    block: str | None
    block_label: str | None  # the block's heading or opening words (teacher.blocks.block_label)
    concept: ConceptRefOut | None
    source: SourceRefOut | None
    payload: dict[str, Any]


class ReaderAnswerOut(BaseModel):
    """What the reader answered, in words (`teacher.managers.study_history.reader_answer`)."""

    form: Literal["choice", "text", "raw"]  # raw: a shape the history does not know, as JSON
    choice: int | None  # the chosen option's index (choice only)
    text: str  # the chosen option, the written answer, or the raw response as JSON
    note: str | None  # why it is raw


class AnswerEntryOut(BaseModel):
    type: Literal["answer"]
    at: str
    id: int
    artifact: ArtifactRefOut | None
    block: str
    block_label: str
    block_type: str
    question: str | None
    attempt: int
    response: Any
    answer: ReaderAnswerOut
    passed: bool
    is_question: bool  # false for a prediction: a guess, not graded
    verdict: str  # "passed" | "partial" | "failed"
    missing: list[str]  # a partial answer's unmet required criteria
    confidence: str | None
    feedback: str | None
    grader: str


class ChatAnswerEntryOut(BaseModel):
    type: Literal["chat_answer"]
    at: str
    id: int
    artifact: ArtifactRefOut | None
    thread_id: int
    block_id: int
    block_type: str
    question: str | None
    attempt: int
    response: Any
    answer: ReaderAnswerOut
    passed: bool
    feedback: str | None
    grader: str


class ChatMessageEntryOut(BaseModel):
    type: Literal["chat_message"]
    at: str
    id: int
    artifact: ArtifactRefOut | None
    thread_id: int
    role: str
    kind: str
    text: str
    references: list[dict[str, Any]]
    model: str | None
    input_tokens: int | None
    output_tokens: int | None


class LabRunEntryOut(BaseModel):
    type: Literal["lab_run"]
    at: str
    id: int
    lab: ArtifactRefOut | None
    artifact: ArtifactRefOut | None  # the lab's study page
    kind: str
    step: int | None
    passed: bool
    output: str
    error: str | None
    file_paths: list[str]


class SpanEntryOut(BaseModel):
    type: Literal["span"]
    at: str
    id: int
    surface: str
    artifact: ArtifactRefOut | None
    lab: ArtifactRefOut | None
    source: SourceRefOut | None
    block: str | None
    block_label: str | None
    started_at: str
    last_seen_at: str
    active_seconds: float
    heartbeats: int


TimelineEntryOut = Annotated[
    EventEntryOut
    | AnswerEntryOut
    | ChatAnswerEntryOut
    | ChatMessageEntryOut
    | LabRunEntryOut
    | SpanEntryOut,
    Field(discriminator="type"),
]


class SessionDetailOut(SessionOut):
    timeline: list[TimelineEntryOut]


class BlockSecondsOut(BaseModel):
    block: str
    label: str  # the block's heading or opening words
    seconds: float


class ArtifactTotalsOut(BaseModel):
    artifact: ArtifactRefOut
    active_seconds: float  # on the page itself (for a lab: on the lab)
    lab_seconds: float  # on its labs (for a lab: the same as active_seconds)
    block_seconds: list[BlockSecondsOut]  # per block of a study page, in reading order
    sessions: int
    first_activity_at: str | None
    last_activity_at: str | None
    answers: int
    answers_passed: int
    pass_rate: float | None  # answers_passed / answers; null without answers
    gates_attempted: int
    user_messages: int
    chat_answers: int
    chat_answers_passed: int
    lab_runs: int
    lab_checks: int
    lab_checks_passed: int


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: Surface
    slug: str | None = None
    block: str | None = None
    resumed: bool = False  # back after a pause: the time since the last one is not credited


class SpanOut(BaseModel):
    id: int
    session_id: int
    surface: str
    block: str | None
    started_at: str
    last_seen_at: str
    active_seconds: float
    heartbeats: int


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    artifact: str | None = None
    lab: str | None = None
    block: str | None = None  # a block key of `artifact`
    concept: str | None = None
    source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: int
    session_id: int
    kind: str
    occurred_at: str


class StandingConceptOut(BaseModel):
    id: int
    slug: str
    name: str


class StandingPageOut(BaseModel):
    slug: str
    title: str
    kind: str
    role: Literal["main", "incidental"]  # main when any of its blocks is about the concept
    main_blocks: int
    main_questions: int
    incidental_blocks: int


class ConceptStandingOut(BaseModel):
    concept: StandingConceptOut
    status: Literal["unstudied", "weak", "developing", "strong"]
    seen: bool  # any answer or time on a block tagged with it, main or incidental
    questions: int  # main-tagged questions in the library
    questions_answered: int
    first_attempt_passed: int
    first_attempt_partial: int  # first attempts that were partly right (count half)
    reviews: int  # answers in the review queue (each one more first try)
    reviews_passed: int
    reviews_partial: int
    first_tries: int  # questions_answered + reviews
    first_attempt_pass_rate: float | None
    attempts: int
    eventually_passed: int
    unresolved: int  # answered but never passed
    incidental_questions: int
    incidental_questions_answered: int
    incidental_first_attempt_passed: int
    blocks: int  # main-tagged blocks
    incidental_blocks: int
    active_seconds: float  # on main-tagged blocks
    incidental_seconds: float
    first_studied_at: str | None
    last_studied_at: str | None
    last_answered_at: str | None
    pages: list[StandingPageOut]


class ThresholdsOut(BaseModel):
    weak_below: float
    strong_from: float
    strong_min_questions: int
    statuses: list[str]
    good_standing: list[str]


class StandingsOut(BaseModel):
    thresholds: ThresholdsOut
    concepts: list[ConceptStandingOut]


class ConceptQuestionOut(BaseModel):
    artifact: ArtifactRefOut
    block: str
    block_type: str
    role: Literal["main", "incidental"]
    visible: bool  # the reader can see the question now (it is not behind an uncleared gate)
    question: str | None  # only when visible
    rationale: str | None  # why it is tagged; only once the question is cleared
    attempts: int
    first_attempt_passed: bool | None  # null: not answered
    eventually_passed: bool
    last_answered_at: str | None


class ConceptStandingDetailOut(ConceptStandingOut):
    thresholds: ThresholdsOut
    tested_by: list[ConceptQuestionOut]


class SuggestionActionPageOut(BaseModel):
    slug: str
    title: str
    role: Literal["main", "incidental"]
    questions: int
    blocks: int
    superseded: bool


class SuggestionActionSourceOut(BaseModel):
    slug: str
    title: str
    role: str  # how the source relates to the concept (introduces | uses | assumes)
    state: Literal["no_study_page", "page_not_tagged", "page_does_not_cover"]
    pages: list[dict[str, str]]


class SuggestionActionReviewOut(BaseModel):
    """A `review_queue` action's queue (`teacher.suggestions.review_action`)."""

    items: int
    due: int
    next_due_at: str | None
    url: str


class SuggestionActionOut(BaseModel):
    kind: Literal["study_page", "source", "none", "review_queue"]
    text: str
    pages: list[SuggestionActionPageOut]
    sources: list[SuggestionActionSourceOut]
    review: SuggestionActionReviewOut | None = None  # only for "review_queue"


class SuggestionAlsoOut(BaseModel):
    kind: str
    reason: str


class SuggestionOut(BaseModel):
    rank: int
    kind: Literal["fill_gap", "review", "ready_next"]
    concept: StandingConceptOut
    status: str
    reason: str
    evidence: dict[str, Any]
    also: list[SuggestionAlsoOut]
    action: SuggestionActionOut


class SuggestionRulesOut(BaseModel):
    kinds: list[str]
    review_after_days: float
    good_standing: list[str]


class SuggestionsOut(BaseModel):
    rules: SuggestionRulesOut
    total: int
    suggestions: list[SuggestionOut]


class PeriodOut(BaseModel):
    since: str  # the start of the period, as an instant
    active_seconds: float
    sessions: int
    answers: int
    questions: int
    first_attempts: int
    first_attempts_passed: int
    first_attempt_pass_rate: float | None


class SummaryOut(BaseModel):
    tz: str
    now: str
    today: PeriodOut
    week: PeriodOut
    total_sessions: int
    recent_sessions: list[SessionOut]


class ContinueItemOut(BaseModel):
    artifact: ArtifactOut
    url: str
    last_studied_at: str
    active_seconds: float


class ContinueOut(BaseModel):
    items: list[ContinueItemOut]


# -- helpers ----------------------------------------------------------------------------------


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _span_out(session: Any, settings: Any, span: Any) -> SpanOut:
    block = ArtifactBlockManager(session, settings).get(span.block_id) if span.block_id else None
    return SpanOut(
        id=span.id,
        session_id=span.session_id,
        surface=span.surface,
        block=block.key if block is not None else None,
        started_at=to_iso(span.started_at) or "",
        last_seen_at=to_iso(span.last_seen_at) or "",
        active_seconds=span.active_seconds,
        heartbeats=span.heartbeats,
    )


# -- endpoints --------------------------------------------------------------------------------


@router.get("/sessions")
def list_sessions(
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_SESSION_PAGE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    artifact: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> SessionsOut:
    """Study sessions, newest first, with their totals (filters: see the module docstring)."""
    history = StudyHistoryManager(session, settings)
    page = history.list_sessions(
        limit=limit,
        offset=offset,
        artifact=ArtifactManager(session, settings).get_by_slug(artifact) if artifact else None,
        source=KnowledgeSourceManager(session, settings).get_by_slug(source) if source else None,
        since=_naive_utc(since),
        until=_naive_utc(until),
    )
    return SessionsOut(
        total=page.total,
        limit=limit,
        offset=offset,
        sessions=[SessionOut(**history.session_record(s)) for s in page.sessions],
    )


@router.get("/sessions/{session_id}")
def get_session(session_id: int, session: SessionDep, settings: SettingsDep) -> SessionDetailOut:
    """One session with its totals and its whole timeline."""
    history = StudyHistoryManager(session, settings)
    found = history.get_session(session_id)
    (summary,) = history.summaries([found])
    return SessionDetailOut.model_validate(
        {**history.session_record(summary), "timeline": history.timeline(found)}
    )


@router.get("/artifacts")
def list_artifact_totals(session: SessionDep, settings: SettingsDep) -> list[ArtifactTotalsOut]:
    """The totals of every artifact, in creation order."""
    history = StudyHistoryManager(session, settings)
    return [
        ArtifactTotalsOut.model_validate(history.artifact_totals(a))
        for a in ArtifactManager(session, settings).list()
    ]


@router.get("/artifacts/{slug}")
def get_artifact_totals(slug: str, session: SessionDep, settings: SettingsDep) -> ArtifactTotalsOut:
    """One artifact's time, sessions, attempts and pass rate."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    return ArtifactTotalsOut.model_validate(
        StudyHistoryManager(session, settings).artifact_totals(artifact)
    )


@router.post("/heartbeat")
def heartbeat(body: HeartbeatIn, session: SessionDep, settings: SettingsDep) -> SpanOut:
    """The page `surface` is visible and the reader active: extend or open an attention span."""
    if body.surface == "graph":
        if body.slug is not None or body.block is not None:
            raise ValueError("a graph heartbeat takes no slug or block")
    elif body.slug is None:
        raise ValueError(f"a {body.surface} heartbeat needs the slug of the page")
    if body.block is not None and body.surface != "study_page":
        raise ValueError(f"a {body.surface} heartbeat takes no block")
    artifacts = ArtifactManager(session, settings)
    refs: dict[str, Any] = {}
    if body.surface == "study_page":
        page = artifacts.get_by_slug(body.slug)  # type: ignore[arg-type]
        if page.kind == LAB_KIND:
            raise LabKindError(f"{page.slug!r} is a lab: send a lab heartbeat for it")
        refs["artifact"] = page
        if body.block is not None:
            refs["block"] = ArtifactBlockManager(session, settings).get_by_key(page, body.block)
    elif body.surface == "lab":
        lab = artifacts.get_by_slug(body.slug)  # type: ignore[arg-type]
        require_lab(lab)
        refs["lab"] = lab
    elif body.surface == "source":
        sources = KnowledgeSourceManager(session, settings)
        refs["source"] = sources.get_by_slug(body.slug)  # type: ignore[arg-type]
    span = StudyHistoryManager(session, settings).heartbeat(
        body.surface, resumed=body.resumed, **refs
    )
    return _span_out(session, settings, span)


@router.post("/events", status_code=201)
def create_event(body: EventIn, session: SessionDep, settings: SettingsDep) -> EventOut:
    """Log an action only the page sees (`CLIENT_EVENT_KINDS`)."""
    artifacts = ArtifactManager(session, settings)
    artifact = artifacts.get_by_slug(body.artifact) if body.artifact is not None else None
    lab = artifacts.get_by_slug(body.lab) if body.lab is not None else None
    if body.block is not None and artifact is None:
        raise ValueError("block is a block key of the artifact: give the artifact too")
    event = StudyHistoryManager(session, settings).log_client(
        body.kind,
        artifact=artifact,
        lab=lab,
        block=(
            ArtifactBlockManager(session, settings).get_by_key(artifact, body.block)
            if artifact is not None and body.block is not None
            else None
        ),
        concept=(
            ConceptManager(session, settings).get_by_slug(body.concept)
            if body.concept is not None
            else None
        ),
        source=(
            KnowledgeSourceManager(session, settings).get_by_slug(body.source)
            if body.source is not None
            else None
        ),
        payload=body.payload,
    )
    return EventOut(
        id=event.id,
        session_id=event.session_id,
        kind=event.kind,
        occurred_at=to_iso(event.occurred_at) or "",
    )


@router.get("/export")
def export_history(
    session: SessionDep,
    settings: SettingsDep,
    format: Literal["json", "markdown"] = "json",
) -> Response:
    """The whole history as a download: JSON (complete, lab files included) or Markdown."""
    data = StudyHistoryManager(session, settings).export()
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    if format == "json":
        return JSONResponse(
            data,
            headers={"Content-Disposition": f'attachment; filename="study-history-{stamp}.json"'},
        )
    return Response(
        content=history_markdown(data),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="study-history-{stamp}.md"'},
    )


@router.get("/concepts")
def list_concept_standing(session: SessionDep, settings: SettingsDep) -> StandingsOut:
    """Every concept's standing, by slug (see `teacher.managers.concept_standing`)."""
    standings = ConceptStandingManager(session, settings).standings()
    return StandingsOut.model_validate(
        {
            "thresholds": standing_thresholds(),
            "concepts": [s.record() for s in standings.values()],
        }
    )


@router.get("/concepts/{slug}")
def get_concept_standing(
    slug: str, session: SessionDep, settings: SettingsDep, max_attempts: MaxAttemptsDep
) -> ConceptStandingDetailOut:
    """One concept's standing and the questions that test it (text only once visible)."""
    concept = ConceptManager(session, settings).get_by_slug(slug)
    standing = ConceptStandingManager(session, settings)
    blocks = ArtifactBlockManager(session, settings)
    states: dict[int, Any] = {}
    tested_by = []
    for role, rationale, evidence, page in standing.tagged_questions(concept):
        if page.id not in states:
            states[page.id] = reading_state(session, settings, page, max_attempts=max_attempts)
        block_state = states[page.id].get(evidence.key)
        content = blocks.read_content(block_state.block) if block_state.visible else None
        tested_by.append(
            {
                "artifact": {"slug": page.slug, "title": page.title, "kind": page.kind},
                "block": evidence.key,
                "block_type": evidence.type,
                "role": role,
                "visible": block_state.visible,
                "question": _question_text(content) if content is not None else None,
                "rationale": rationale if block_state.cleared else None,
                "attempts": evidence.attempts,
                "first_attempt_passed": evidence.first_passed,
                "eventually_passed": evidence.eventually_passed,
                "last_answered_at": to_iso(evidence.last_answered_at),
            }
        )
    return ConceptStandingDetailOut.model_validate(
        {
            **standing.standing(concept).record(),
            "thresholds": standing_thresholds(),
            "tested_by": tested_by,
        }
    )


def _question_text(content: dict[str, Any]) -> str | None:
    for key in ("question", "prompt"):
        if isinstance(content.get(key), str):
            return content[key]
    return None


@router.get("/suggestions")
def list_suggestions(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> SuggestionsOut:
    """What to study next, ranked (see `teacher.suggestions`); the first `limit` (default all).
    The review queue's items are derived first, so a review suggestion can point there."""
    ReviewManager(session, settings).sync(max_attempts=max_attempts)
    ranked = rank_suggestions(session, settings, now=request.app.state.clock())
    return SuggestionsOut.model_validate(
        {
            "rules": suggestion_rules(),
            "total": len(ranked),
            "suggestions": ranked if limit is None else ranked[:limit],
        }
    )


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"unknown time zone {name!r}: give an IANA name such as Europe/Stockholm") from err


@router.get("/summary")
def history_summary(
    session: SessionDep,
    settings: SettingsDep,
    tz: Annotated[str, Query(min_length=1, description="the reader's IANA time zone")],
    sessions: Annotated[int, Query(ge=1, le=MAX_SESSION_PAGE)] = 5,
) -> SummaryOut:
    """Time, sessions and answers today and this week, and the newest sessions."""
    zone = _zone(tz)
    now = utcnow()  # naive UTC
    local_now = now.replace(tzinfo=UTC).astimezone(zone)
    day_start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    week_start = datetime.combine(
        local_now.date() - timedelta(days=local_now.weekday()), time.min, tzinfo=zone
    )
    history = StudyHistoryManager(session, settings)

    def period(start: datetime) -> PeriodOut:
        since = start.astimezone(UTC).replace(tzinfo=None)
        return PeriodOut(since=to_iso(since) or "", **history.period_totals(since))

    page = history.list_sessions(limit=sessions)
    return SummaryOut(
        tz=tz,
        now=to_iso(now) or "",
        today=period(day_start),
        week=period(week_start),
        total_sessions=page.total,
        recent_sessions=[SessionOut(**history.session_record(s)) for s in page.sessions],
    )


@router.get("/continue")
def continue_studying(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=20)] = 3,
) -> ContinueOut:
    """The study pages and labs studied most recently, with their progress."""
    return ContinueOut(
        items=continue_items(session, settings, request.app.state.max_attempts, limit)
    )


def continue_items(
    session: Any, settings: Any, max_attempts: int, limit: int
) -> list[ContinueItemOut]:
    """The items of `GET /api/history/continue?limit=<limit>` (also the phone summary's)."""
    items = []
    for artifact, last, seconds in StudyHistoryManager(session, settings).recently_studied(limit):
        out = artifact_out(session, settings, artifact, max_attempts)
        url = f"/labs/{artifact.slug}" if artifact.kind == LAB_KIND else f"/artifacts/{artifact.slug}"
        items.append(
            ContinueItemOut(
                artifact=out, url=url, last_studied_at=to_iso(last) or "", active_seconds=seconds
            )
        )
    return items


__all__ = [
    "ArtifactTotalsOut",
    "ContinueItemOut",
    "ContinueOut",
    "continue_items",
    "SummaryOut",
    "ConceptStandingDetailOut",
    "ConceptStandingOut",
    "StandingsOut",
    "SuggestionsOut",
    "EventIn",
    "EventOut",
    "HeartbeatIn",
    "SessionDetailOut",
    "SessionOut",
    "SessionsOut",
    "SpanOut",
    "router",
]
