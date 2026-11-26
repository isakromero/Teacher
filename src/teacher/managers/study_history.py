"""StudyHistoryManager: study sessions, the event log, time on page, and the read model over
everything the reader did.

Sessions. Every logged activity joins the open session or starts a new one: an event
(`log`), a heartbeat (`heartbeat`), and every new row of block_responses,
chat_block_responses, chat_messages and lab_runs (their managers call `activity_session()`
and store its id). The open session is the newest live session whose last activity is at most
`SESSION_IDLE_GAP` before the new activity; joining moves its `last_activity_at` forward.
Sessions backfilled by migration 0010 are never joined.

Events. `log(kind, ...)` appends a `StudyEvent`. Each kind has a spec (`EVENT_SPECS`): the
references it requires and allows and the exact keys of its payload; anything else is a
ValueError (an unknown kind too), never a dropped or trimmed event. A lab reference also sets
the event's `artifact` to the lab's study page (NULL once that page is gone), so a page's
events include its labs'; callers never pass both. Where each kind is logged:

| kind                        | logged by                                   | references        |
|-----------------------------|---------------------------------------------|-------------------|
| library_opened              | GET /library                                | -                 |
| study_page_opened           | GET /artifacts/{slug} (a study page)        | artifact          |
| lab_opened                  | GET /labs/{slug}                            | lab               |
| source_opened               | GET /sources/{slug}                         | source            |
| source_file_opened          | the page ("Open original file")             | source            |
| graph_opened                | GET /graph[?concept=]                       | concept?          |
| gate_cleared                | POST .../blocks/{key}/responses             | artifact, block   |
|                             | POST /api/offline/artifacts/{slug}/attempts |                   |
| glossary_opened             | the page (POST /api/history/events)         | artifact          |
| glossary_term_viewed        | the page                                    | artifact          |
| glossary_build_requested    | POST /api/artifacts/{slug}/glossary         | artifact          |
| chat_thread_started         | POST .../chat/threads                       | artifact or lab   |
| lab_workspace_saved         | PUT /api/labs/{slug}/workspace              | lab               |
| lab_hint_viewed             | the page                                    | lab               |
| lab_solution_viewed         | GET /api/labs/{slug}/solution               | lab               |
| lab_exported                | GET /api/labs/{slug}/export                 | lab               |
| concept_inspected           | the page                                    | concept           |
| graph_searched              | GET /api/graph/search                       | -                 |
| relations_requested         | POST /api/graph/concepts/{slug}/relate      | concept           |
| source_added                | POST /api/sources, /api/sources/from-url    | source            |
| source_updated              | PATCH /api/sources/{slug}                   | source            |
| source_deleted              | DELETE /api/sources/{slug}                  | source            |
| study_page_requested        | POST /api/artifacts                         | artifact          |
| lab_requested               | POST /api/artifacts/{slug}/labs             | lab               |
| regeneration_requested      | POST /api/artifacts/{slug}/regenerate       | artifact or lab   |
| artifact_deleted            | DELETE /api/artifacts/{slug}                | artifact or lab   |

Only `CLIENT_EVENT_KINDS` may be posted by a page (`log_client`, which also checks the payload
against the data: the glossary term exists, the lab step has a hint). Events of a deletion keep
the slug and title in the payload, since their references become NULL.

Time on page. A page sends a heartbeat about every `HEARTBEAT_INTERVAL` while its tab is
visible and the reader was active recently, and also once the block in view has changed and
settled, and when its tab is hidden or the page closed (attention.js). `heartbeat(surface, ...)`
credits the time since the session's previous heartbeat, if that was at most
`SPAN_CONTINUE_GAP` ago, to the span of that previous heartbeat: what was on screen until now.
The heartbeat's own subject (surface, page, block in view) then continues that span when it is
unchanged, else opens a new span starting now with nothing credited yet. After a longer gap (the
reader was idle or away), or when the page says the reader is back after a pause it saw
(`resumed`: the tab was hidden, or there was no input for a while), a new span starts with
nothing credited. So active time never exceeds wall-clock time, and time before a change of
block goes to the block read before it.

Read model. `list_sessions` (newest first, paged, filtered by page, paper and date) gives
`SessionSummary` rows; `session_record` turns one into JSON. `timeline` merges one session's
events, answers, chat answers, chat messages, lab runs and attention spans in time order, as
JSON-ready records (see `TIMELINE_TYPES`): a block is named by its key and its `block_label`
(`teacher.blocks.block_label`), and an answer has its raw `response` and `answer`, what the
reader answered in words (`reader_answer`), its `verdict` ("passed", "partial", "failed"; null
only for a row written outside the managers), the `missing` criteria of a partial answer and
the reader's `confidence`. `artifact_totals` sums an artifact's time, attempts
and pass rate. For the home page, `last_studied` and `seconds_on_artifacts` give every
artifact's latest activity and time, `recently_studied` the newest few, and `period_totals`
what was done since a moment (today, this week). `export()` is the whole history as one JSON-ready dict, with every concept's
standing and every block concept tag (`teacher.history_export` renders it as Markdown). Grader `details` (rubric verdicts) are not part of the read model;
the attempt's feedback is.

Predictions. A `prediction` block is a gate (the reader commits to a guess before reading on,
and any guess clears it) but not a question (`teacher.blocks.is_question_type`). Its responses
are therefore left out of every count of answers, passes and questions (`summaries`,
`period_totals`, `artifact_totals`, `NOT_QUESTIONS`); the timeline still lists each one, as an
`answer` record with `is_question` false, so the history shows what the reader guessed without
calling it passed.

Like every manager it flushes and never commits.
"""

from __future__ import annotations

import builtins
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, Select, cast, func, or_, select, union
from sqlalchemy.orm import Session

from teacher.blocks import BLOCK_TYPES, block_label, public_content
from teacher.config import Settings
from teacher.db.models import (
    ATTENTION_SURFACES,
    STUDY_EVENT_KINDS,
    Artifact,
    ArtifactBlock,
    ArtifactSource,
    AttentionSpan,
    BlockResponse,
    ChatBlock,
    ChatBlockResponse,
    ChatMessage,
    ChatThread,
    Concept,
    GlossaryTerm,
    KnowledgeSource,
    LabRunRecord,
    StudyEvent,
    StudySession,
    utcnow,
)
from teacher.labs import LAB_KIND, STUDY_PAGE_PARAM, read_lab
from teacher.managers.artifact_blocks import ArtifactBlockManager
from teacher.managers.artifacts import ArtifactManager, _require_persistent
from teacher.managers.block_concepts import BlockConceptManager
from teacher.managers.concept_standing import ConceptStandingManager
from teacher.managers.concept_standing import thresholds as standing_thresholds
from teacher.managers.errors import IntegrityCheckError, NotFoundError

SESSION_IDLE_GAP = timedelta(minutes=30)
HEARTBEAT_INTERVAL = timedelta(seconds=15)  # how often a page sends one (attention.js)
SPAN_CONTINUE_GAP = 3 * HEARTBEAT_INTERVAL
MAX_SESSION_PAGE = 200
EXPORT_FORMAT = "teacher-study-history"
# 2: adds standing_thresholds, concepts (standing) and block_concepts; 3: adds each answer's
# `answer` (what the reader answered, for people) and the blocks' labels (`block_label`, and
# `label` in block_seconds); 4: adds each answer's `is_question`, `verdict`, `missing` and
# `confidence`, and leaves predictions out of the answer counts
EXPORT_VERSION = 4

REFERENCES: tuple[str, ...] = ("artifact", "lab", "block", "concept", "source")

# The gate types that are not questions (a prediction): responses to them are never counted as
# answers, passes or questions answered.
NOT_QUESTIONS: tuple[str, ...] = tuple(
    sorted(name for name, entry in BLOCK_TYPES.items() if entry.is_gate and not entry.is_question)
)


def _a_question(type_column: Any) -> Any:
    """SQL: the block whose `type_column` this is is a question (not one of `NOT_QUESTIONS`)."""
    return type_column.not_in(NOT_QUESTIONS)


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What an event kind takes: `required` references (all of them), `one_of` (exactly one of
    these, when not empty), `optional` references, and the exact `payload` keys."""

    required: frozenset[str] = frozenset()
    one_of: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    payload: frozenset[str] = frozenset()


def _spec(
    required: Iterable[str] = (),
    *,
    one_of: Iterable[str] = (),
    optional: Iterable[str] = (),
    payload: Iterable[str] = (),
) -> EventSpec:
    return EventSpec(
        frozenset(required), frozenset(one_of), frozenset(optional), frozenset(payload)
    )


EVENT_SPECS: dict[str, EventSpec] = {
    "library_opened": _spec(),
    "study_page_opened": _spec(["artifact"]),
    "lab_opened": _spec(["lab"]),
    "source_opened": _spec(["source"]),
    "source_file_opened": _spec(["source"]),
    "graph_opened": _spec(optional=["concept"]),
    "gate_cleared": _spec(["artifact", "block"], payload=["state", "attempt", "unlocked"]),
    "glossary_opened": _spec(["artifact"]),
    "glossary_term_viewed": _spec(["artifact"], payload=["term"]),
    "glossary_build_requested": _spec(["artifact"]),
    "chat_thread_started": _spec(one_of=["artifact", "lab"], payload=["thread_id"]),
    "lab_workspace_saved": _spec(["lab"], payload=["files", "chars"]),
    "lab_hint_viewed": _spec(["lab"], payload=["step"]),
    "lab_solution_viewed": _spec(["lab"], payload=["first_time"]),
    "lab_exported": _spec(["lab"]),
    "concept_inspected": _spec(["concept"]),
    "graph_searched": _spec(payload=["query", "results", "semantic"]),
    "relations_requested": _spec(["concept"], payload=["regenerate", "model"]),
    "source_added": _spec(["source"], payload=["via"]),
    "source_updated": _spec(["source"], payload=["fields"]),
    "source_deleted": _spec(["source"], payload=["slug", "title"]),
    "study_page_requested": _spec(["artifact"], payload=["sources", "model"]),
    "lab_requested": _spec(["lab"], payload=["model"]),
    "regeneration_requested": _spec(one_of=["artifact", "lab"], payload=["supersedes", "model"]),
    "artifact_deleted": _spec(one_of=["artifact", "lab"], payload=["slug", "title", "kind"]),
}
assert set(EVENT_SPECS) == set(STUDY_EVENT_KINDS), "every event kind needs a spec"

# The kinds a page may post itself (POST /api/history/events): actions that never reach the
# server otherwise.
CLIENT_EVENT_KINDS: tuple[str, ...] = (
    "source_file_opened",
    "glossary_opened",
    "glossary_term_viewed",
    "lab_hint_viewed",
    "concept_inspected",
)

TIMELINE_TYPES: tuple[str, ...] = (
    "event",
    "answer",
    "chat_answer",
    "chat_message",
    "lab_run",
    "span",
)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One session with its totals. `artifacts` are the study pages and labs it touched (by
    id), `sources` the source pages it opened."""

    session: StudySession
    active_seconds: float = 0.0
    events: int = 0
    answers: int = 0
    answers_passed: int = 0
    chat_answers: int = 0
    chat_answers_passed: int = 0
    user_messages: int = 0
    tutor_messages: int = 0
    lab_runs: int = 0
    lab_checks: int = 0
    lab_checks_passed: int = 0
    artifacts: tuple[Artifact, ...] = ()
    sources: tuple[KnowledgeSource, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionPage:
    """A page of `list_sessions`: `total` sessions match the filters."""

    total: int
    sessions: list[SessionSummary] = field(default_factory=list)


# -- small helpers ----------------------------------------------------------------------------


def iso(value: datetime | None) -> str | None:
    """A stored naive-UTC timestamp as ISO 8601 with an explicit +00:00 offset."""
    if value is None:
        return None
    return value.isoformat() + "+00:00" if value.tzinfo is None else value.isoformat()


def artifact_ref(artifact: Artifact | None) -> dict[str, str] | None:
    if artifact is None:
        return None
    return {"slug": artifact.slug, "title": artifact.title, "kind": artifact.kind}


def source_ref(source: KnowledgeSource | None) -> dict[str, str] | None:
    if source is None:
        return None
    return {"slug": source.slug, "title": source.title}


def concept_ref(concept: Concept | None) -> dict[str, str] | None:
    if concept is None:
        return None
    return {"slug": concept.slug, "name": concept.name}


def _question(content: Mapping[str, Any]) -> str | None:
    """The question a gate block asks (a multiple choice's `question`, a blurb's or a
    prediction's `prompt`)."""
    for key in ("question", "prompt"):
        value = content.get(key)
        if isinstance(value, str):
            return value
    return None


ANSWER_FORMS: tuple[str, ...] = ("choice", "text", "raw")


def reader_answer(block_type: str, content: Mapping[str, Any], response: Any) -> dict[str, Any]:
    """What the reader answered, for people, from a stored response (`teacher.grading` defines
    the shapes): `{"form": "choice", "choice": <index>, "text": <the chosen option>}` for a
    multiple choice or a prediction (the guess), `{"form": "text", "text": <what they wrote>}`
    for a blurb. Anything else
    (an unknown block type, or a response not in its type's shape) is `{"form": "raw", "text":
    <the response as JSON>, "note": <why>}`, shown as it is and labelled, never hidden. Only
    the question's public part is read, so nothing the reader has not seen (the correct option,
    a model answer) is ever included."""

    def raw(note: str) -> dict[str, Any]:
        return {
            "form": "raw",
            "choice": None,
            "text": json.dumps(response, ensure_ascii=False, sort_keys=True),
            "note": note,
        }

    public = public_content(dict(content))
    keys = set(response) if isinstance(response, dict) else None
    if block_type in ("multiple_choice", "prediction"):
        if keys != {"choice"}:
            what = block_type.replace("_", "-")
            return raw(f'a {what} answer should be {{"choice": <option index>}}')
        choice = response["choice"]
        options = public.get("options")
        if isinstance(choice, bool) or not isinstance(choice, int):
            return raw("the choice is not an option index")
        if not isinstance(options, list) or not 0 <= choice < len(options):
            count = len(options) if isinstance(options, list) else 0
            return raw(f"option {choice} is not one of the question's {count} options")
        return {"form": "choice", "choice": choice, "text": str(options[choice]), "note": None}
    if block_type == "blurb":
        if keys != {"text"} or not isinstance(response["text"], str):
            return raw('a written answer should be {"text": <the answer>}')
        return {"form": "text", "choice": None, "text": response["text"], "note": None}
    return raw(f"a {block_type} block takes no answers this history knows how to show")


def _passed(column: Any) -> Any:
    """SUM of a boolean column as an integer (a plain SUM would come back as a bool)."""
    return func.coalesce(func.sum(cast(column, Integer)), 0)


def _check_payload(
    kind: str, payload: Mapping[str, Any], expected: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{kind}: the payload must be an object")
    keys = set(payload)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected {', '.join(extra)}")
        wanted = ", ".join(sorted(expected)) or "no keys"
        raise ValueError(f"{kind}: invalid payload ({'; '.join(parts)}); it takes {wanted}")
    try:
        json.dumps(dict(payload), allow_nan=False)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{kind}: the payload is not JSON-serialisable: {err}") from err
    return dict(payload)


# -- the manager ------------------------------------------------------------------------------


class StudyHistoryManager:
    """Assign sessions, log events and heartbeats, and read the history back."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- sessions ---------------------------------------------------------------------------

    def activity_session(self, at: datetime | None = None) -> StudySession:
        """The open live session at `at` (default now), moved forward to `at`; or a new one if
        no live session had activity within `SESSION_IDLE_GAP` before it."""
        at = utcnow() if at is None else at
        current = self.session.scalar(
            select(StudySession)
            .where(
                StudySession.backfilled.is_(False),
                StudySession.last_activity_at >= at - SESSION_IDLE_GAP,
                StudySession.started_at <= at + SESSION_IDLE_GAP,
            )
            .order_by(StudySession.last_activity_at.desc(), StudySession.id.desc())
            .limit(1)
        )
        if current is None:
            current = StudySession(started_at=at, last_activity_at=at, backfilled=False)
            self.session.add(current)
        else:
            if at > current.last_activity_at:
                current.last_activity_at = at
            if at < current.started_at:
                current.started_at = at
        self.session.flush()
        return current

    def get_session(self, session_id: int) -> StudySession:
        """The session with `session_id`; NotFoundError if there is none."""
        found = self.session.get(StudySession, session_id)
        if found is None:
            raise NotFoundError(f"no study session with id {session_id}")
        return found

    # -- references -------------------------------------------------------------------------

    def study_page_of(self, lab: Artifact) -> Artifact | None:
        """The study page a lab was made from, or None once it is gone (or the lab names none)."""
        slug = (lab.params or {}).get(STUDY_PAGE_PARAM)
        if not isinstance(slug, str):
            return None
        page = self.session.scalar(select(Artifact).where(Artifact.slug == slug))
        if page is not None and page.kind == LAB_KIND:
            raise IntegrityCheckError(
                f"lab {lab.slug!r} names {slug!r} as its study page, but that is a lab"
            )
        return page

    def _refs(
        self,
        what: str,
        *,
        artifact: Artifact | None,
        lab: Artifact | None,
        block: ArtifactBlock | None,
        concept: Concept | None,
        source: KnowledgeSource | None,
    ) -> dict[str, Any]:
        """Check the references' kinds and consistency; the column values to store."""
        for name, obj in (("artifact", artifact), ("lab", lab), ("block", block),
                          ("concept", concept), ("source", source)):
            if obj is not None:
                _require_persistent(obj, name)
        if artifact is not None and artifact.kind == LAB_KIND:
            raise ValueError(f"{what}: {artifact.slug!r} is a lab; pass it as the lab")
        if lab is not None and lab.kind != LAB_KIND:
            raise ValueError(f"{what}: {lab.slug!r} is a {lab.kind}, not a lab")
        if lab is not None and artifact is not None:
            raise ValueError(f"{what}: pass the lab only; its study page is found from it")
        if block is not None and (artifact is None or block.artifact_id != artifact.id):
            raise ValueError(f"{what}: block {block.key!r} is not a block of the artifact given")
        page = self.study_page_of(lab) if lab is not None else artifact
        return {
            "artifact_id": page.id if page is not None else None,
            "lab_id": lab.id if lab is not None else None,
            "block_id": block.id if block is not None else None,
            "concept_id": concept.id if concept is not None else None,
            "source_id": source.id if source is not None else None,
        }

    # -- events -----------------------------------------------------------------------------

    def log(
        self,
        kind: str,
        *,
        artifact: Artifact | None = None,
        lab: Artifact | None = None,
        block: ArtifactBlock | None = None,
        concept: Concept | None = None,
        source: KnowledgeSource | None = None,
        payload: Mapping[str, Any] | None = None,
        at: datetime | None = None,
    ) -> StudyEvent:
        """Append one event of `kind` (see the module docstring) in the open session."""
        body, refs = self._checked(
            kind, artifact=artifact, lab=lab, block=block, concept=concept, source=source,
            payload=payload,
        )
        at = utcnow() if at is None else at
        event = StudyEvent(
            session_id=self.activity_session(at).id,
            occurred_at=at,
            kind=kind,
            payload=body,
            **refs,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _checked(
        self,
        kind: str,
        *,
        artifact: Artifact | None,
        lab: Artifact | None,
        block: ArtifactBlock | None,
        concept: Concept | None,
        source: KnowledgeSource | None,
        payload: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The payload and reference columns of a valid event of `kind`; ValueError otherwise."""
        spec = EVENT_SPECS.get(kind)
        if spec is None:
            raise ValueError(
                f"unknown study event kind {kind!r}: expected one of {', '.join(STUDY_EVENT_KINDS)}"
            )
        given = {
            name for name, obj in (("artifact", artifact), ("lab", lab), ("block", block),
                                   ("concept", concept), ("source", source))
            if obj is not None
        }
        missing = sorted(spec.required - given)
        if missing:
            raise ValueError(f"{kind}: needs {', '.join(missing)}")
        if spec.one_of and len(given & spec.one_of) != 1:
            raise ValueError(f"{kind}: needs exactly one of {', '.join(sorted(spec.one_of))}")
        extra = sorted(given - spec.required - spec.one_of - spec.optional)
        if extra:
            raise ValueError(f"{kind}: takes no {', '.join(extra)}")
        body = _check_payload(kind, payload if payload is not None else {}, spec.payload)
        refs = self._refs(
            kind, artifact=artifact, lab=lab, block=block, concept=concept, source=source
        )
        return body, refs

    def log_client(
        self,
        kind: str,
        *,
        artifact: Artifact | None = None,
        lab: Artifact | None = None,
        block: ArtifactBlock | None = None,
        concept: Concept | None = None,
        source: KnowledgeSource | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> StudyEvent:
        """`log` for an event a page reported: only `CLIENT_EVENT_KINDS`, and the payload must
        match the data (NotFoundError for a glossary term or lab step that does not exist)."""
        if kind not in CLIENT_EVENT_KINDS:
            if kind in EVENT_SPECS:
                raise ValueError(
                    f"{kind} events are logged by the server, not posted by a page; a page may "
                    f"post {', '.join(CLIENT_EVENT_KINDS)}"
                )
            raise ValueError(
                f"unknown study event kind {kind!r}: a page may post "
                f"{', '.join(CLIENT_EVENT_KINDS)}"
            )
        payload = dict(payload or {})
        self._checked(  # the kind's rules first, then the payload against the data
            kind, artifact=artifact, lab=lab, block=block, concept=concept, source=source,
            payload=payload,
        )
        if kind == "glossary_term_viewed" and artifact is not None:
            term = payload.get("term")
            if not isinstance(term, str) or not term:
                raise ValueError("glossary_term_viewed: payload.term must be a non-empty string")
            found = self.session.scalar(
                select(GlossaryTerm.id).where(
                    GlossaryTerm.artifact_id == artifact.id, GlossaryTerm.term == term
                )
            )
            if found is None:
                raise NotFoundError(f"{artifact.slug!r} has no glossary term {term!r}")
        if kind == "lab_hint_viewed" and lab is not None:
            step = payload.get("step")
            if isinstance(step, bool) or not isinstance(step, int):
                raise ValueError("lab_hint_viewed: payload.step must be a step index (an integer)")
            spec = read_lab(ArtifactManager(self.session, self.settings).dir(lab), slug=lab.slug)
            if not 0 <= step < len(spec.steps):
                raise NotFoundError(
                    f"lab {lab.slug!r} has no step {step} (its steps are numbered 0 to "
                    f"{len(spec.steps) - 1})"
                )
            if spec.steps[step].hint is None:
                raise ValueError(f"step {step} of lab {lab.slug!r} has no hint")
        return self.log(
            kind,
            artifact=artifact,
            lab=lab,
            block=block,
            concept=concept,
            source=source,
            payload=payload,
        )

    # -- heartbeats -------------------------------------------------------------------------

    def heartbeat(
        self,
        surface: str,
        *,
        artifact: Artifact | None = None,
        lab: Artifact | None = None,
        block: ArtifactBlock | None = None,
        source: KnowledgeSource | None = None,
        resumed: bool = False,
        at: datetime | None = None,
    ) -> AttentionSpan:
        """Record that `surface` is on screen and the reader active (see the module docstring);
        the span that was extended or opened. A study page takes `artifact` (and the `block` in
        view, if any), a lab `lab`, a source page `source`, the graph nothing. `resumed`: the
        reader is back after a pause the page saw (the tab was hidden, or no input for a
        while), so the time since the previous heartbeat is not credited."""
        if not isinstance(resumed, bool):
            raise ValueError("resumed must be a bool")
        allowed = {
            "study_page": ({"artifact"}, {"block"}),
            "lab": ({"lab"}, set()),
            "source": ({"source"}, set()),
            "graph": (set(), set()),
        }
        if surface not in allowed:
            raise ValueError(
                f"unknown attention surface {surface!r}: expected one of "
                f"{', '.join(ATTENTION_SURFACES)}"
            )
        required, optional = allowed[surface]
        given = {
            name for name, obj in (("artifact", artifact), ("lab", lab), ("block", block),
                                   ("source", source))
            if obj is not None
        }
        missing = sorted(required - given)
        if missing:
            raise ValueError(f"a {surface} heartbeat needs {', '.join(missing)}")
        extra = sorted(given - required - optional)
        if extra:
            raise ValueError(f"a {surface} heartbeat takes no {', '.join(extra)}")
        refs = self._refs(
            f"{surface} heartbeat",
            artifact=artifact, lab=lab, block=block, concept=None, source=source,
        )
        del refs["concept_id"]
        at = utcnow() if at is None else at
        current = self.activity_session(at)
        last = self.session.scalar(
            select(AttentionSpan)
            .where(AttentionSpan.session_id == current.id)
            .order_by(AttentionSpan.last_seen_at.desc(), AttentionSpan.id.desc())
            .limit(1)
        )
        if not resumed and last is not None and at - last.last_seen_at <= SPAN_CONTINUE_GAP:
            # The time since the previous heartbeat was spent on what was on screen then (a
            # page sends one at once when the block in view changes), so it goes to that span.
            credit = max(0.0, (at - last.last_seen_at).total_seconds())
            last.last_seen_at = max(last.last_seen_at, at)
            last.active_seconds = last.active_seconds + credit
            same = last.surface == surface and all(
                getattr(last, column) == value for column, value in refs.items()
            )
            if same:
                last.heartbeats = last.heartbeats + 1
                self.session.flush()
                return last
        span = AttentionSpan(
            session_id=current.id, surface=surface, started_at=at, last_seen_at=at,
            active_seconds=0.0, heartbeats=1, **refs,
        )
        self.session.add(span)
        self.session.flush()
        return span

    # -- the read model: which sessions ------------------------------------------------------

    def labs_of(self, page: Artifact) -> builtins.list[Artifact]:
        """Every lab made from the study page `page` (all versions), by id."""
        return list(
            self.session.scalars(
                select(Artifact)
                .where(Artifact.kind == LAB_KIND)
                .where(Artifact.params[STUDY_PAGE_PARAM].as_string() == page.slug)
                .order_by(Artifact.id)
            )
        )

    def _with_labs(self, artifacts: Iterable[Artifact]) -> set[int]:
        """The ids of `artifacts` and of every lab made from the study pages among them."""
        ids: set[int] = set()
        for artifact in artifacts:
            ids.add(artifact.id)
            if artifact.kind != LAB_KIND:
                ids.update(lab.id for lab in self.labs_of(artifact))
        return ids

    def _session_ids_touching(
        self, artifact_ids: set[int], source_ids: set[int]
    ) -> Select[Any]:
        """A SELECT of the ids of sessions with any activity on those artifacts (as the page or
        the lab) or those source pages."""
        ids = list(artifact_ids)
        parts: builtins.list[Select[Any]] = [
            select(StudyEvent.session_id).where(
                or_(StudyEvent.artifact_id.in_(ids), StudyEvent.lab_id.in_(ids),
                    StudyEvent.source_id.in_(list(source_ids)))
            ),
            select(AttentionSpan.session_id).where(
                or_(AttentionSpan.artifact_id.in_(ids), AttentionSpan.lab_id.in_(ids),
                    AttentionSpan.source_id.in_(list(source_ids)))
            ),
            select(BlockResponse.session_id)
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .where(ArtifactBlock.artifact_id.in_(ids)),
            select(ChatMessage.session_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatThread.artifact_id.in_(ids)),
            select(ChatBlockResponse.session_id)
            .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatThread.artifact_id.in_(ids)),
            select(LabRunRecord.session_id).where(LabRunRecord.artifact_id.in_(ids)),
        ]
        return union(*parts)

    def list_sessions(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        artifact: Artifact | None = None,
        source: KnowledgeSource | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> SessionPage:
        """Sessions newest first (by start), `limit` from `offset`, with their totals.

        `artifact` keeps the sessions with activity on that study page or any lab made from
        it (or on that lab); `source` those on any page or lab of that source or on the source
        page itself; `since`/`until` (naive UTC) those overlapping [since, until)."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SESSION_PAGE
        ):
            raise ValueError(f"limit must be an integer from 1 to {MAX_SESSION_PAGE}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if since is not None and until is not None and until <= since:
            raise ValueError("until must be later than since")
        stmt = select(StudySession)
        if artifact is not None:
            _require_persistent(artifact, "artifact")
            touching = self._session_ids_touching(self._with_labs([artifact]), set()).subquery()
            stmt = stmt.where(StudySession.id.in_(select(touching.c[0])))
        if source is not None:
            _require_persistent(source, "source")
            pages = list(
                self.session.scalars(
                    select(Artifact)
                    .join(ArtifactSource, ArtifactSource.artifact_id == Artifact.id)
                    .where(ArtifactSource.source_id == source.id)
                )
            )
            touching = self._session_ids_touching(self._with_labs(pages), {source.id}).subquery()
            stmt = stmt.where(StudySession.id.in_(select(touching.c[0])))
        if since is not None:
            stmt = stmt.where(StudySession.last_activity_at >= since)
        if until is not None:
            stmt = stmt.where(StudySession.started_at < until)
        total = self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(
            self.session.scalars(
                stmt.order_by(StudySession.started_at.desc(), StudySession.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return SessionPage(total=total, sessions=self.summaries(rows))

    # -- the read model: totals --------------------------------------------------------------

    def summaries(self, sessions: Sequence[StudySession]) -> builtins.list[SessionSummary]:
        """`sessions` with their totals, in the same order (a fixed number of queries)."""
        ids = [s.id for s in sessions]
        if not ids:
            return []
        counts: dict[int, dict[str, Any]] = {i: defaultdict(int) for i in ids}

        def add(rows: Iterable[Any], *names: str) -> None:
            for session_id, *values in rows:
                for name, value in zip(names, values, strict=True):
                    counts[session_id][name] += value or 0

        add(
            self.session.execute(
                select(AttentionSpan.session_id, func.sum(AttentionSpan.active_seconds))
                .where(AttentionSpan.session_id.in_(ids))
                .group_by(AttentionSpan.session_id)
            ),
            "active_seconds",
        )
        add(
            self.session.execute(
                select(StudyEvent.session_id, func.count())
                .where(StudyEvent.session_id.in_(ids))
                .group_by(StudyEvent.session_id)
            ),
            "events",
        )
        add(
            self.session.execute(
                select(BlockResponse.session_id, func.count(), _passed(BlockResponse.passed))
                .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
                .where(BlockResponse.session_id.in_(ids), _a_question(ArtifactBlock.type))
                .group_by(BlockResponse.session_id)
            ),
            "answers",
            "answers_passed",
        )
        add(
            self.session.execute(
                select(
                    ChatBlockResponse.session_id, func.count(), _passed(ChatBlockResponse.passed)
                )
                .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
                .where(ChatBlockResponse.session_id.in_(ids), _a_question(ChatBlock.type))
                .group_by(ChatBlockResponse.session_id)
            ),
            "chat_answers",
            "chat_answers_passed",
        )
        for session_id, role, n in self.session.execute(
            select(ChatMessage.session_id, ChatMessage.role, func.count())
            .where(ChatMessage.session_id.in_(ids))
            .group_by(ChatMessage.session_id, ChatMessage.role)
        ):
            counts[session_id]["user_messages" if role == "user" else "tutor_messages"] += n
        for session_id, kind, n, passed in self.session.execute(
            select(
                LabRunRecord.session_id, LabRunRecord.kind, func.count(),
                _passed(LabRunRecord.passed),
            )
            .where(LabRunRecord.session_id.in_(ids))
            .group_by(LabRunRecord.session_id, LabRunRecord.kind)
        ):
            if kind == "check":
                counts[session_id]["lab_checks"] += n
                counts[session_id]["lab_checks_passed"] += passed or 0
            else:
                counts[session_id]["lab_runs"] += n

        artifacts_of, sources_of = self._touched(ids)
        return [
            SessionSummary(
                session=s,
                active_seconds=float(counts[s.id]["active_seconds"]),
                events=int(counts[s.id]["events"]),
                answers=int(counts[s.id]["answers"]),
                answers_passed=int(counts[s.id]["answers_passed"]),
                chat_answers=int(counts[s.id]["chat_answers"]),
                chat_answers_passed=int(counts[s.id]["chat_answers_passed"]),
                user_messages=int(counts[s.id]["user_messages"]),
                tutor_messages=int(counts[s.id]["tutor_messages"]),
                lab_runs=int(counts[s.id]["lab_runs"]),
                lab_checks=int(counts[s.id]["lab_checks"]),
                lab_checks_passed=int(counts[s.id]["lab_checks_passed"]),
                artifacts=tuple(artifacts_of.get(s.id, ())),
                sources=tuple(sources_of.get(s.id, ())),
            )
            for s in sessions
        ]

    def _touched(
        self, ids: Sequence[int]
    ) -> tuple[dict[int, builtins.list[Artifact]], dict[int, builtins.list[KnowledgeSource]]]:
        """Per session id: the artifacts (pages and labs) and source pages it touched."""
        pairs: set[tuple[int, int]] = set()
        queries = [
            select(StudyEvent.session_id, StudyEvent.artifact_id),
            select(StudyEvent.session_id, StudyEvent.lab_id),
            select(AttentionSpan.session_id, AttentionSpan.artifact_id),
            select(AttentionSpan.session_id, AttentionSpan.lab_id),
            select(BlockResponse.session_id, ArtifactBlock.artifact_id).join(
                ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id
            ),
            select(ChatMessage.session_id, ChatThread.artifact_id).join(
                ChatThread, ChatThread.id == ChatMessage.thread_id
            ),
            select(ChatBlockResponse.session_id, ChatThread.artifact_id)
            .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id),
            select(LabRunRecord.session_id, LabRunRecord.artifact_id),
        ]
        for query in queries:
            session_col = query.selected_columns[0]
            for session_id, artifact_id in self.session.execute(
                query.where(session_col.in_(list(ids))).distinct()
            ):
                if artifact_id is not None:
                    pairs.add((session_id, artifact_id))
        source_pairs: set[tuple[int, int]] = set()
        for model in (StudyEvent, AttentionSpan):
            for session_id, source_id in self.session.execute(
                select(model.session_id, model.source_id)
                .where(model.session_id.in_(list(ids)), model.source_id.is_not(None))
                .distinct()
            ):
                source_pairs.add((session_id, source_id))
        artifacts = {
            a.id: a
            for a in self.session.scalars(
                select(Artifact).where(Artifact.id.in_({a for _, a in pairs}))
            )
        }
        sources = {
            s.id: s
            for s in self.session.scalars(
                select(KnowledgeSource).where(KnowledgeSource.id.in_({s for _, s in source_pairs}))
            )
        }
        artifacts_of: dict[int, builtins.list[Artifact]] = defaultdict(list)
        for session_id, artifact_id in sorted(pairs):
            artifacts_of[session_id].append(artifacts[artifact_id])
        sources_of: dict[int, builtins.list[KnowledgeSource]] = defaultdict(list)
        for session_id, source_id in sorted(source_pairs):
            sources_of[session_id].append(sources[source_id])
        return artifacts_of, sources_of

    # -- the read model: the home page ---------------------------------------------------------

    def last_studied(self) -> dict[int, datetime]:
        """Per artifact id (study pages and labs that still exist): the time of the reader's
        latest activity on it: opening it, time on it (attention spans), an answer on one of
        its blocks, a tutor message in one of its conversations, a lab run. Artifacts never
        studied are absent. A fixed number of queries."""
        queries: builtins.list[Select[Any]] = [
            select(StudyEvent.artifact_id, func.max(StudyEvent.occurred_at))
            .where(StudyEvent.kind == "study_page_opened")
            .group_by(StudyEvent.artifact_id),
            select(StudyEvent.lab_id, func.max(StudyEvent.occurred_at))
            .where(StudyEvent.kind == "lab_opened")
            .group_by(StudyEvent.lab_id),
            select(AttentionSpan.artifact_id, func.max(AttentionSpan.last_seen_at))
            .where(AttentionSpan.surface == "study_page")
            .group_by(AttentionSpan.artifact_id),
            select(AttentionSpan.lab_id, func.max(AttentionSpan.last_seen_at))
            .where(AttentionSpan.surface == "lab")
            .group_by(AttentionSpan.lab_id),
            select(ArtifactBlock.artifact_id, func.max(BlockResponse.created_at))
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .group_by(ArtifactBlock.artifact_id),
            select(ChatThread.artifact_id, func.max(ChatMessage.created_at))
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .group_by(ChatThread.artifact_id),
            select(LabRunRecord.artifact_id, func.max(LabRunRecord.created_at)).group_by(
                LabRunRecord.artifact_id
            ),
        ]
        latest: dict[int, datetime] = {}
        for query in queries:
            for artifact_id, at in self.session.execute(query):
                if artifact_id is None or at is None:
                    continue
                if artifact_id not in latest or at > latest[artifact_id]:
                    latest[artifact_id] = at
        return latest

    def seconds_on_artifacts(self) -> dict[int, float]:
        """Per artifact id: the reader's active time on the page itself (a study page's time
        on its own surface, a lab's on the lab surface), as `artifact_totals` counts it.
        Artifacts with no time are absent."""
        out: dict[int, float] = defaultdict(float)
        for column, surface in ((AttentionSpan.artifact_id, "study_page"),
                                (AttentionSpan.lab_id, "lab")):
            for artifact_id, seconds in self.session.execute(
                select(column, func.sum(AttentionSpan.active_seconds))
                .where(AttentionSpan.surface == surface, column.is_not(None))
                .group_by(column)
            ):
                out[artifact_id] += float(seconds or 0.0)
        return dict(out)

    def recently_studied(
        self, limit: int
    ) -> builtins.list[tuple[Artifact, datetime, float]]:
        """The `limit` study pages and labs studied most recently (of the study pages
        on the same sources only the one studied last), newest first, as
        `(artifact, last studied at, active seconds on it)` (see `last_studied`)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        latest = self.last_studied()
        if not latest:
            return []
        artifacts = {
            a.id: a
            for a in self.session.scalars(select(Artifact).where(Artifact.id.in_(latest)))
        }
        # One entry per source and kind: an older version of a page the reader has since
        # studied in a newer version is not something to continue.
        # Labs are kept apart: several labs of one page are different work, not versions.
        seen: set[tuple[Any, ...]] = set()
        ids: builtins.list[int] = []
        for i in sorted(latest, key=lambda i: (latest[i], i), reverse=True):
            artifact = artifacts.get(i)
            if artifact is None:
                continue
            source_ids = tuple(link.source_id for link in artifact.source_links)
            key = (
                (artifact.kind, source_ids)
                if artifact.kind == "study_page" and source_ids
                else ("artifact", artifact.id)
            )
            if key in seen:
                continue
            seen.add(key)
            ids.append(i)
            if len(ids) == limit:
                break
        seconds = self.seconds_on_artifacts()
        return [(artifacts[i], latest[i], seconds.get(i, 0.0)) for i in ids]

    def period_totals(self, since: datetime) -> dict[str, Any]:
        """What the reader did from `since` (naive UTC) on, as JSON: `active_seconds` (time on
        pages, from the attention spans that started since then), `sessions` (sessions with
        activity since then), `answers` (attempts at questions on study pages and at the
        tutor's exercises; predictions are not questions), `questions` (distinct questions
        answered), `first_attempts` and
        `first_attempts_passed` (those answered for the first time since then, and how many
        of them passed at once) and `first_attempt_pass_rate` (null without first attempts)."""
        if since.tzinfo is not None:
            raise ValueError("since must be a naive UTC datetime")
        active = self.session.scalar(
            select(func.coalesce(func.sum(AttentionSpan.active_seconds), 0.0)).where(
                AttentionSpan.started_at >= since
            )
        )
        sessions = self.session.scalar(
            select(func.count(StudySession.id)).where(StudySession.last_activity_at >= since)
        )
        answers = questions = first = first_passed = 0
        for model, block in ((BlockResponse, ArtifactBlock), (ChatBlockResponse, ChatBlock)):
            n, distinct, firsts, passed = self.session.execute(
                select(
                    func.count(model.id),
                    func.count(func.distinct(model.block_id)),
                    func.coalesce(func.sum(cast(model.attempt == 1, Integer)), 0),
                    func.coalesce(
                        func.sum(cast((model.attempt == 1) & model.passed, Integer)), 0
                    ),
                )
                .join(block, block.id == model.block_id)
                .where(model.created_at >= since, _a_question(block.type))
            ).one()
            answers += int(n)
            questions += int(distinct)
            first += int(firsts)
            first_passed += int(passed)
        return {
            "active_seconds": float(active or 0.0),
            "sessions": int(sessions or 0),
            "answers": answers,
            "questions": questions,
            "first_attempts": first,
            "first_attempts_passed": first_passed,
            "first_attempt_pass_rate": (first_passed / first) if first else None,
        }

    def artifact_totals(self, artifact: Artifact) -> dict[str, Any]:
        """An artifact's totals as JSON: time on it (and, for a study page, on its labs and per
        block), sessions, the first and last activity, attempts and pass rate, tutor messages
        and exercises, lab runs and checks (of its labs, or of the lab itself). Answers, their
        pass rate and `gates_attempted` count questions only, not predictions."""
        _require_persistent(artifact, "artifact")
        is_lab = artifact.kind == LAB_KIND
        labs = [artifact] if is_lab else self.labs_of(artifact)
        lab_ids = [lab.id for lab in labs]

        def seconds(*conditions: Any) -> float:
            return float(
                self.session.scalar(
                    select(func.coalesce(func.sum(AttentionSpan.active_seconds), 0.0)).where(
                        *conditions
                    )
                )
                or 0.0
            )

        if is_lab:
            active = seconds(AttentionSpan.surface == "lab", AttentionSpan.lab_id == artifact.id)
            lab_seconds = active
            blocks: builtins.list[dict[str, Any]] = []
        else:
            active = seconds(
                AttentionSpan.surface == "study_page", AttentionSpan.artifact_id == artifact.id
            )
            lab_seconds = seconds(AttentionSpan.surface == "lab", AttentionSpan.lab_id.in_(lab_ids))
            block_contents = ArtifactBlockManager(self.session, self.settings)
            blocks = [
                {
                    "block": block.key,
                    "label": block_label(block.type, block_contents.read_content(block)),
                    "seconds": float(total),
                }
                for block, total in self.session.execute(
                    select(ArtifactBlock, func.sum(AttentionSpan.active_seconds))
                    .join(ArtifactBlock, ArtifactBlock.id == AttentionSpan.block_id)
                    .where(
                        AttentionSpan.surface == "study_page",
                        AttentionSpan.artifact_id == artifact.id,
                    )
                    .group_by(ArtifactBlock.id)
                    .order_by(ArtifactBlock.position)
                )
            ]
        answers, answers_passed, gates = self.session.execute(
            select(
                func.count(BlockResponse.id),
                _passed(BlockResponse.passed),
                func.count(func.distinct(BlockResponse.block_id)),
            )
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .where(ArtifactBlock.artifact_id == artifact.id, _a_question(ArtifactBlock.type))
        ).one()
        chat_answers, chat_passed = self.session.execute(
            select(func.count(ChatBlockResponse.id), _passed(ChatBlockResponse.passed))
            .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatThread.artifact_id == artifact.id, _a_question(ChatBlock.type))
        ).one()
        user_messages = self.session.scalar(
            select(func.count(ChatMessage.id))
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatThread.artifact_id == artifact.id, ChatMessage.role == "user")
        ) or 0
        runs: dict[str, tuple[int, int]] = {
            kind: (int(n), int(passed or 0))
            for kind, n, passed in self.session.execute(
                select(LabRunRecord.kind, func.count(), _passed(LabRunRecord.passed))
                .where(LabRunRecord.artifact_id.in_(lab_ids))
                .group_by(LabRunRecord.kind)
            )
        }
        ids = {artifact.id, *lab_ids}
        touching = self._session_ids_touching(ids, set()).subquery()
        span = self.session.execute(
            select(
                func.count(StudySession.id),
                func.min(StudySession.started_at),
                func.max(StudySession.last_activity_at),
            ).where(StudySession.id.in_(select(touching.c[0])))
        ).one()
        first, last = self._activity_bounds(ids)
        return {
            "artifact": artifact_ref(artifact),
            "active_seconds": active,
            "lab_seconds": lab_seconds,
            "block_seconds": blocks,
            "sessions": int(span[0] or 0),
            "first_activity_at": iso(first),
            "last_activity_at": iso(last),
            "answers": int(answers),
            "answers_passed": int(answers_passed),
            "pass_rate": (int(answers_passed) / int(answers)) if answers else None,
            "gates_attempted": int(gates),
            "user_messages": int(user_messages),
            "chat_answers": int(chat_answers),
            "chat_answers_passed": int(chat_passed),
            "lab_runs": runs.get("run", (0, 0))[0],
            "lab_checks": runs.get("check", (0, 0))[0],
            "lab_checks_passed": runs.get("check", (0, 0))[1],
        }

    def _activity_bounds(self, ids: set[int]) -> tuple[datetime | None, datetime | None]:
        """The earliest and latest activity time on the artifacts `ids`."""
        id_list = list(ids)
        times: builtins.list[Select[Any]] = [
            select(StudyEvent.occurred_at.label("at")).where(
                or_(StudyEvent.artifact_id.in_(id_list), StudyEvent.lab_id.in_(id_list))
            ),
            select(AttentionSpan.started_at.label("at")).where(
                or_(AttentionSpan.artifact_id.in_(id_list), AttentionSpan.lab_id.in_(id_list))
            ),
            select(AttentionSpan.last_seen_at.label("at")).where(
                or_(AttentionSpan.artifact_id.in_(id_list), AttentionSpan.lab_id.in_(id_list))
            ),
            select(BlockResponse.created_at.label("at"))
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .where(ArtifactBlock.artifact_id.in_(id_list)),
            select(ChatMessage.created_at.label("at"))
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatThread.artifact_id.in_(id_list)),
            select(LabRunRecord.created_at.label("at")).where(
                LabRunRecord.artifact_id.in_(id_list)
            ),
        ]
        values = []
        for query in times:
            sub = query.subquery()
            values.append(
                self.session.execute(select(func.min(sub.c.at), func.max(sub.c.at))).one()
            )
        lows = [low for low, _ in values if low is not None]
        highs = [high for _, high in values if high is not None]
        return (min(lows) if lows else None, max(highs) if highs else None)

    # -- the read model: one session's timeline ----------------------------------------------

    def timeline(
        self, study_session: StudySession | None, *, with_files: bool = False
    ) -> builtins.list[dict[str, Any]]:
        """Everything in `study_session`, in time order, as JSON-ready records (see
        `TIMELINE_TYPES`); None gives the activity rows that have no session. `with_files` adds
        the files of each lab run (their contents can be large)."""
        if study_session is not None:
            _require_persistent(study_session, "study session")
            session_id = study_session.id

            def where(column: Any) -> Any:
                return column == session_id
        else:

            def where(column: Any) -> Any:
                return column.is_(None)

        records: builtins.list[tuple[datetime, int, int, dict[str, Any]]] = []
        artifacts: dict[int, Artifact] = {}

        def artifact_by_id(artifact_id: int | None) -> Artifact | None:
            if artifact_id is None:
                return None
            if artifact_id not in artifacts:
                manager = ArtifactManager(self.session, self.settings)
                artifacts[artifact_id] = manager.get(artifact_id)
            return artifacts[artifact_id]

        blocks_manager = ArtifactBlockManager(self.session, self.settings)
        contents: dict[int, dict[str, Any]] = {}

        def content_of(block: ArtifactBlock) -> dict[str, Any]:
            if block.id not in contents:
                contents[block.id] = blocks_manager.read_content(block)
            return contents[block.id]

        def label_of(block: ArtifactBlock | None) -> str | None:
            return block_label(block.type, content_of(block)) if block is not None else None

        if study_session is not None:
            for event in self.session.scalars(
                select(StudyEvent).where(where(StudyEvent.session_id)).order_by(StudyEvent.id)
            ):
                block = self.session.get(ArtifactBlock, event.block_id) if event.block_id else None
                concept = self.session.get(Concept, event.concept_id) if event.concept_id else None
                source = (
                    self.session.get(KnowledgeSource, event.source_id) if event.source_id else None
                )
                records.append((event.occurred_at, 0, event.id, {
                    "type": "event",
                    "at": iso(event.occurred_at),
                    "id": event.id,
                    "kind": event.kind,
                    "artifact": artifact_ref(artifact_by_id(event.artifact_id)),
                    "lab": artifact_ref(artifact_by_id(event.lab_id)),
                    "block": block.key if block is not None else None,
                    "block_label": label_of(block),
                    "concept": concept_ref(concept),
                    "source": source_ref(source),
                    "payload": dict(event.payload or {}),
                }))
            for span in self.session.scalars(
                select(AttentionSpan)
                .where(where(AttentionSpan.session_id))
                .order_by(AttentionSpan.id)
            ):
                block = self.session.get(ArtifactBlock, span.block_id) if span.block_id else None
                source = (
                    self.session.get(KnowledgeSource, span.source_id) if span.source_id else None
                )
                records.append((span.started_at, 5, span.id, {
                    "type": "span",
                    "at": iso(span.started_at),
                    "id": span.id,
                    "surface": span.surface,
                    "artifact": artifact_ref(artifact_by_id(span.artifact_id)),
                    "lab": artifact_ref(artifact_by_id(span.lab_id)),
                    "source": source_ref(source),
                    "block": block.key if block is not None else None,
                    "block_label": label_of(block),
                    "started_at": iso(span.started_at),
                    "last_seen_at": iso(span.last_seen_at),
                    "active_seconds": span.active_seconds,
                    "heartbeats": span.heartbeats,
                }))

        for row, block in self.session.execute(
            select(BlockResponse, ArtifactBlock)
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .where(where(BlockResponse.session_id))
            .order_by(BlockResponse.id)
        ):
            records.append((row.created_at, 1, row.id, {
                "type": "answer",
                "at": iso(row.created_at),
                "id": row.id,
                "artifact": artifact_ref(artifact_by_id(block.artifact_id)),
                "block": block.key,
                "block_label": label_of(block),
                "block_type": block.type,
                "question": _question(content_of(block)),
                "attempt": row.attempt,
                "response": row.response,
                "answer": reader_answer(block.type, content_of(block), row.response),
                "is_question": block.type not in NOT_QUESTIONS,
                "passed": row.passed,
                "verdict": row.verdict,
                "missing": list(row.missing),
                "confidence": row.confidence,
                "feedback": row.feedback,
                "grader": row.grader,
            }))
        for row, block, message in self.session.execute(
            select(ChatBlockResponse, ChatBlock, ChatMessage)
            .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .where(where(ChatBlockResponse.session_id))
            .order_by(ChatBlockResponse.id)
        ):
            thread = self.session.get(ChatThread, message.thread_id)
            assert thread is not None  # the FK guarantees it
            records.append((row.created_at, 2, row.id, {
                "type": "chat_answer",
                "at": iso(row.created_at),
                "id": row.id,
                "artifact": artifact_ref(artifact_by_id(thread.artifact_id)),
                "thread_id": thread.id,
                "block_id": block.id,
                "block_type": block.type,
                "question": _question(block.content or {}),
                "attempt": row.attempt,
                "response": row.response,
                "answer": reader_answer(block.type, block.content or {}, row.response),
                "is_question": block.type not in NOT_QUESTIONS,
                "passed": row.passed,
                "verdict": row.verdict,
                "missing": list(row.missing),
                "confidence": None,  # the tutor's exercises do not ask
                "feedback": row.feedback,
                "grader": row.grader,
            }))
        for message, thread in self.session.execute(
            select(ChatMessage, ChatThread)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(where(ChatMessage.session_id))
            .order_by(ChatMessage.id)
        ):
            records.append((message.created_at, 3, message.id, {
                "type": "chat_message",
                "at": iso(message.created_at),
                "id": message.id,
                "artifact": artifact_ref(artifact_by_id(thread.artifact_id)),
                "thread_id": thread.id,
                "role": message.role,
                "kind": message.kind,
                "text": message.text,
                "references": list(message.references or []),
                "model": message.model,
                "input_tokens": message.input_tokens,
                "output_tokens": message.output_tokens,
            }))
        for run in self.session.scalars(
            select(LabRunRecord).where(where(LabRunRecord.session_id)).order_by(LabRunRecord.id)
        ):
            lab = artifact_by_id(run.artifact_id)
            assert lab is not None
            record: dict[str, Any] = {
                "type": "lab_run",
                "at": iso(run.created_at),
                "id": run.id,
                "lab": artifact_ref(lab),
                "artifact": artifact_ref(self.study_page_of(lab)),
                "kind": run.kind,
                "step": run.step_index,
                "passed": run.passed,
                "output": run.output,
                "error": run.error,
                "file_paths": [f["path"] for f in run.files],
            }
            if with_files:
                record["files"] = [dict(f) for f in run.files]
            records.append((run.created_at, 4, run.id, record))
        records.sort(key=lambda item: (item[0], item[1], item[2]))
        return [record for _at, _order, _id, record in records]

    # -- JSON for sessions and the export -----------------------------------------------------

    @staticmethod
    def session_record(summary: SessionSummary) -> dict[str, Any]:
        """One `SessionSummary` as JSON."""
        s = summary.session
        return {
            "id": s.id,
            "started_at": iso(s.started_at),
            "last_activity_at": iso(s.last_activity_at),
            "backfilled": s.backfilled,
            "active_seconds": summary.active_seconds,
            "events": summary.events,
            "answers": summary.answers,
            "answers_passed": summary.answers_passed,
            "chat_answers": summary.chat_answers,
            "chat_answers_passed": summary.chat_answers_passed,
            "user_messages": summary.user_messages,
            "tutor_messages": summary.tutor_messages,
            "lab_runs": summary.lab_runs,
            "lab_checks": summary.lab_checks,
            "lab_checks_passed": summary.lab_checks_passed,
            "artifacts": [artifact_ref(a) for a in summary.artifacts],
            "sources": [source_ref(src) for src in summary.sources],
        }

    def export(self) -> dict[str, Any]:
        """The whole history as one JSON-ready dict: every session (oldest first) with its
        totals and full timeline (lab run files included), the activity rows without a
        session, every artifact's totals, every concept's standing
        (`teacher.managers.concept_standing`, with its thresholds) and every block concept
        tag (`block_concepts`)."""
        sessions = list(
            self.session.scalars(
                select(StudySession).order_by(StudySession.started_at, StudySession.id)
            )
        )
        out_sessions = []
        for summary in self.summaries(sessions):
            out_sessions.append(
                {
                    **self.session_record(summary),
                    "timeline": self.timeline(summary.session, with_files=True),
                }
            )
        artifacts = ArtifactManager(self.session, self.settings).list()
        standings = ConceptStandingManager(self.session, self.settings).standings()
        tags = BlockConceptManager(self.session, self.settings).all()
        reached = self._blocks_reached({page.id for _tag, _block, page, _concept in tags})
        block_contents = ArtifactBlockManager(self.session, self.settings)
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": iso(utcnow()),
            "session_idle_gap_minutes": SESSION_IDLE_GAP.total_seconds() / 60,
            "sessions": out_sessions,
            "unassigned": self.timeline(None, with_files=True),
            "artifacts": [self.artifact_totals(a) for a in artifacts],
            "standing_thresholds": standing_thresholds(),
            "concepts": [s.record() for s in standings.values()],
            "block_concepts": [
                {
                    "artifact": artifact_ref(page),
                    "block": block.key,
                    # null for a block the reader has not been shown yet (`_blocks_reached`)
                    "block_label": (
                        block_label(block.type, block_contents.read_content(block))
                        if block.id in reached
                        else None
                    ),
                    "block_type": block.type,
                    "concept": concept_ref(concept),
                    "role": tag.role,
                    "origin": tag.origin,
                    "rationale": tag.rationale,
                    "model": tag.model,
                    "created_at": iso(tag.created_at),
                }
                for tag, block, page, concept in tags
            ],
        }

    def _blocks_reached(self, page_ids: set[int]) -> set[int]:
        """The ids of the blocks of those study pages the reader is known to have been shown:
        on each page, every block up to the furthest one with an answer or time on it, and at
        least every block up to and including the first gate never passed. (A gate opened by
        running out of attempts is not known here, since the attempt limit is the web app's, so
        the blocks after it count only once there is activity on them.) The export names only
        these blocks by their text, so it never shows the opening words of a locked block."""
        if not page_ids:
            return set()
        rows = list(
            self.session.execute(
                select(ArtifactBlock.id, ArtifactBlock.artifact_id, ArtifactBlock.position,
                       ArtifactBlock.is_gate)
                .where(ArtifactBlock.artifact_id.in_(page_ids))
                .order_by(ArtifactBlock.artifact_id, ArtifactBlock.position)
            )
        )
        ids = [row.id for row in rows]
        passed = set(
            self.session.scalars(
                select(BlockResponse.block_id)
                .where(BlockResponse.block_id.in_(ids), BlockResponse.passed.is_(True))
                .distinct()
            )
        )
        active = set(
            self.session.scalars(
                select(BlockResponse.block_id).where(BlockResponse.block_id.in_(ids)).distinct()
            )
        ) | set(
            self.session.scalars(
                select(AttentionSpan.block_id).where(AttentionSpan.block_id.in_(ids)).distinct()
            )
        )
        by_page: dict[int, builtins.list[Any]] = defaultdict(list)
        for row in rows:
            by_page[row.artifact_id].append(row)
        reached: set[int] = set()
        for blocks in by_page.values():
            first_open = next(
                (b.position for b in blocks if b.is_gate and b.id not in passed),
                blocks[-1].position,
            )
            furthest = max((b.position for b in blocks if b.id in active), default=first_open)
            limit = max(first_open, furthest)
            reached.update(b.id for b in blocks if b.position <= limit)
        return reached


__all__ = [
    "ANSWER_FORMS",
    "CLIENT_EVENT_KINDS",
    "EVENT_SPECS",
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "HEARTBEAT_INTERVAL",
    "MAX_SESSION_PAGE",
    "NOT_QUESTIONS",
    "SESSION_IDLE_GAP",
    "SPAN_CONTINUE_GAP",
    "TIMELINE_TYPES",
    "EventSpec",
    "SessionPage",
    "SessionSummary",
    "StudyHistoryManager",
    "artifact_ref",
    "concept_ref",
    "iso",
    "reader_answer",
    "source_ref",
]
