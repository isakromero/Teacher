"""The cost ledger: every model and embedding API call recorded with its tokens and its cost.

Recording. Every place that calls an API (the call sites are listed below) first takes the
current `Meter` with `require_meter()`, which raises `UnmeteredCallError` when there is none,
so a call that could not be recorded is never sent; right after the API answers it records the
call (`record_reply` for a Claude message, `record_embedding` for an OpenAI embedding
response), before the reply is checked, so a reply that is then rejected (refusal, truncation,
a schema error, a failed check) still has its row. A call that raises an SDK error has no
usage to record and is not recorded (an HTTP error is not billed; a stream that breaks off
after tokens were produced is billed but reports nothing, a gap the ledger cannot close).

The cost is computed when the call is recorded (`teacher.pricing.cost`) and stored with the
date the price was checked, so a stored figure never changes when prices do. A model without a
known price is recorded with its tokens and a NULL cost (logged as a warning), never a 0, and
never an error: the money is already spent.

A row is written by the meter's sink in a short transaction of its own (`session_scope`, a
connection of its own), so it stays when the work around the call rolls back, e.g. a
generation that fails after its model calls (the same reason `generation_runs` commits each
stage on its own). The caller must not hold a write transaction open during the call (none
does: the call sites already keep sessions closed while the model runs).

Scopes. What a call was for comes from the current meter, set for the duration of a job with
`cost_scope(settings, purpose=..., <links>)` (a context variable, so it reaches the call sites
without being passed down) or passed explicitly where the job spans a generator that a
threadpool iterates step by step (the tutor's streamed reply: `Tutor.stream(..., meter=...)`).
Scopes nest: an inner scope adds links (artifact, generation run, chat thread and message,
knowledge source) to the outer scope's, and may not change a link the outer one set. The
`purpose` is the job the call served (`teacher.db.models.API_CALL_PURPOSES`): the outermost
scope that names one decides, and a call site's own purpose (e.g. "grading" for the judge) is
used only when no scope names one. So the judge's calls during a page generation are
`generation` calls, step `judge`; the same judge grading a reader's answer is `grading`.

Where the scopes are set: `teacher.generation.run_generation` (purpose generation or
lab_generation, the artifact; each `GenerationContext.stage` adds its run), the review bank and
glossary builds (their run), the reading, chat and review routers (grading, the artifact and
thread), the tutor (explicit meter: thread, then the stored reply's message via
`link_message`), reference extraction, concept extraction and block tagging (their source or
artifact), concept relating (no link: it belongs to a concept, which the ledger cannot point
at), and `teacher concepts reindex` (embeddings). Every web request also runs under a base
meter with no links (`teacher.web.app.CostMeterMiddleware`), so a call without a closer scope
is still recorded, unlinked: a search query's embedding, the embedding of an edited concept,
and the metadata detection of a file being added (it runs before the source exists, in a
request of its own, and the file may never be added). The command line has no base meter:
each command that calls an API sets its own scope.

Code that calls a component outside the app and the command line (tests, a script) installs a
meter itself (`metering(Meter.in_memory())`; the tests' `cost_meter` fixture); without one the
component fails before its call.

Call sites (every `messages.create`, `messages.stream` and `embeddings.create` in the app):
`teacher.claude_generator.stream_message` (every streamed call of the generator, the page
checks in `teacher.page_revision`, the glossary, the lab generator, concept extraction, block
tagging, concept relating and reference extraction), `teacher.judge.LLMJudgeGrader._call`,
`teacher.metadata.MetadataExtractor`, `teacher.review_bank.ClaudeReviewBankBuilder.build`,
`teacher.tutor.Tutor._call` (each round of the tool loop) and
`teacher.embeddings.OpenAIEmbedder.embed` (each batch).

Totals. `artifact_cost` (what generating a page or lab cost, by stage, and what was spent on it
since, by purpose) and `thread_cost` / `message_costs` (a tutor conversation and its replies)
sum the stored figures. A total counts the calls with no known price separately and names
their models (`CostTotal.unknown_price_calls`), and says when it includes estimates
(`CostTotal.estimate_calls`: backfilled calls priced at an upper bound, see
`teacher.cost_backfill`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from teacher import pricing
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.providers import create_db_provider
from teacher.db.models import API_CALL_PURPOSES, ApiCall, GenerationRun, utcnow
from teacher.pricing import ANTHROPIC, OPENAI, TokenUsage

logger = logging.getLogger(__name__)

GENERATION_PURPOSES = ("generation", "lab_generation")

PURPOSE_LABELS: dict[str, str] = {
    "generation": "generation",
    "lab_generation": "lab generation",
    "review_bank": "review questions",
    "glossary": "glossary",
    "tutor": "tutor",
    "grading": "grading",
    "block_tagging": "concept tagging",
    "concept_extraction": "concept extraction",
    "concept_relating": "concept relating",
    "metadata": "metadata",
    "reference_extraction": "references",
    "embedding": "embeddings",
}
# How a generation's stages are named in its cost breakdown (other stages: their own name).
STAGE_LABELS: dict[str, str] = {
    "analyse": "outline",
    "write": "writing",
    "review": "review",
    "question_check": "question check",
    "revise": "revision",
    "concepts": "concepts",
    "block_concepts": "concept tags",
    "glossary": "glossary",
}


class UnmeteredCallError(RuntimeError):
    """An API call was about to be made with no cost meter in place; it was not sent."""


@dataclass(frozen=True)
class CallLinks:
    """What a call was for (each optional)."""

    artifact_id: int | None = None
    generation_run_id: int | None = None
    chat_thread_id: int | None = None
    chat_message_id: int | None = None
    knowledge_source_id: int | None = None

    def merged(self, **links: int | None) -> CallLinks:
        """These links plus `links` (None leaves a link as it is). ValueError for an unknown
        link or one that would change a link already set."""
        known = {f.name for f in fields(self)}
        unknown = set(links) - known
        if unknown:
            raise ValueError(f"unknown cost link(s): {', '.join(sorted(unknown))}")
        changes: dict[str, int] = {}
        for name, value in links.items():
            if value is None:
                continue
            current = getattr(self, name)
            if current is not None and current != value:
                raise ValueError(
                    f"a cost scope cannot change {name} from {current} to {value}"
                )
            changes[name] = value
        return replace(self, **changes)


@dataclass(frozen=True)
class LedgerEntry:
    """One recorded call, as written to `api_calls` (`id` once written)."""

    purpose: str
    step: str
    provider: str
    model: str
    usage: TokenUsage
    cost_usd: float | None
    priced_on: str | None
    is_estimate: bool
    links: CallLinks
    response_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    backfill_key: str | None = None
    id: int | None = None

    def row(self) -> ApiCall:
        return ApiCall(
            created_at=self.created_at,
            purpose=self.purpose,
            step=self.step,
            provider=self.provider,
            model=self.model,
            response_id=self.response_id,
            input_tokens=self.usage.input_tokens,
            cache_write_tokens=self.usage.cache_write_tokens,
            cache_write_1h_tokens=self.usage.cache_write_1h_tokens,
            cache_read_tokens=self.usage.cache_read_tokens,
            output_tokens=self.usage.output_tokens,
            cost_usd=self.cost_usd,
            priced_on=self.priced_on,
            is_estimate=self.is_estimate,
            backfill_key=self.backfill_key,
            **{f.name: getattr(self.links, f.name) for f in fields(self.links)},
        )


class Sink(Protocol):
    def write(self, entry: LedgerEntry) -> int:
        """Store `entry` (committed) and return its id."""
        ...


class DatabaseSink:
    """Writes each entry to `api_calls` in a transaction of its own."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def write(self, entry: LedgerEntry) -> int:
        with session_scope(self.settings) as session:
            row = entry.row()
            session.add(row)
            session.flush()
            return row.id


class MemorySink:
    """Keeps the entries in `entries` (for tests and dry runs); ids count from 1."""

    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def write(self, entry: LedgerEntry) -> int:
        stored = replace(entry, id=len(self.entries) + 1)
        self.entries.append(stored)
        return stored.id  # type: ignore[return-value]


def _check_purpose(purpose: str | None) -> None:
    if purpose is not None and purpose not in API_CALL_PURPOSES:
        raise ValueError(
            f"unknown cost purpose {purpose!r}; expected one of {', '.join(API_CALL_PURPOSES)}"
        )


class Meter:
    """Records calls to a sink with a purpose and links (see the module docstring).

    `recorded_ids` lists the ids of the calls recorded through this meter and its children,
    so a caller can link them afterwards (`link_message`)."""

    def __init__(
        self,
        sink: Sink,
        *,
        purpose: str | None = None,
        links: CallLinks = CallLinks(),
        db_key: str | None = None,
        parent: Meter | None = None,
    ) -> None:
        _check_purpose(purpose)
        self.sink = sink
        self.purpose = purpose
        self.links = links
        self.db_key = db_key
        self.parent = parent
        self.recorded_ids: list[int] = []

    @classmethod
    def for_settings(
        cls, settings: Settings, *, purpose: str | None = None, **links: int | None
    ) -> Meter:
        """A meter writing to `settings`' database."""
        return cls(
            DatabaseSink(settings),
            purpose=purpose,
            links=CallLinks().merged(**links),
            db_key=create_db_provider(settings).cache_key,
        )

    @classmethod
    def in_memory(cls, *, purpose: str | None = None, **links: int | None) -> Meter:
        """A meter keeping its entries in memory (`entries`)."""
        return cls(MemorySink(), purpose=purpose, links=CallLinks().merged(**links))

    @property
    def entries(self) -> list[LedgerEntry]:
        """What an in-memory meter recorded (all of its tree's calls)."""
        if not isinstance(self.sink, MemorySink):
            raise TypeError("only an in-memory meter keeps its entries")
        return self.sink.entries

    def records_to(self, settings: Settings) -> bool:
        return self.db_key == create_db_provider(settings).cache_key

    def child(self, *, purpose: str | None = None, **links: int | None) -> Meter:
        """A meter with this one's links plus `links`; `purpose` applies only when this meter
        has none (the outermost job decides)."""
        _check_purpose(purpose)
        return Meter(
            self.sink,
            purpose=self.purpose if self.purpose is not None else purpose,
            links=self.links.merged(**links),
            db_key=self.db_key,
            parent=self,
        )

    def record(
        self,
        *,
        purpose: str,
        step: str,
        provider: str,
        model: str,
        usage: TokenUsage,
        response_id: str | None = None,
    ) -> LedgerEntry:
        """Price and store one call; `purpose` is the call site's own, used when the meter
        names none. A model without a known price is stored with no cost (and logged)."""
        purpose = self.purpose if self.purpose is not None else purpose
        _check_purpose(purpose)
        if not isinstance(step, str) or not step.strip():
            raise ValueError("a recorded call needs a step")
        priced = pricing.cost(model, usage)
        if priced is None:
            logger.warning(
                "no known price for model %r: its %s call was recorded without a cost",
                model,
                step,
            )
        entry = LedgerEntry(
            purpose=purpose,
            step=step,
            provider=provider,
            model=model,
            usage=usage,
            cost_usd=priced.usd if priced is not None else None,
            priced_on=priced.checked_on if priced is not None else None,
            is_estimate=(
                priced.upper_bound
                if priced is not None
                else usage.cache_write_1h_tokens is None and usage.cache_write_tokens > 0
            ),
            links=self.links,
            response_id=response_id,
        )
        entry_id = self.sink.write(entry)
        meter: Meter | None = self
        while meter is not None:
            meter.recorded_ids.append(entry_id)
            meter = meter.parent
        return replace(entry, id=entry_id)


_CURRENT: ContextVar[Meter | None] = ContextVar("teacher_cost_meter", default=None)


def current_meter() -> Meter | None:
    """The meter calls are recorded to now, if any."""
    return _CURRENT.get()


@contextmanager
def metering(meter: Meter) -> Iterator[Meter]:
    """Make `meter` the current meter for the duration (in this thread's context)."""
    token = _CURRENT.set(meter)
    try:
        yield meter
    finally:
        _CURRENT.reset(token)


@contextmanager
def cost_scope(
    settings: Settings, *, purpose: str | None = None, **links: int | None
) -> Iterator[Meter]:
    """Record the calls made inside to `settings`' database with `purpose` (unless an
    enclosing scope names one) and `links` added to the enclosing scope's. An enclosing meter
    that records elsewhere (another database, or memory) is not extended: a new one is used."""
    outer = current_meter()
    if outer is not None and outer.records_to(settings):
        meter = outer.child(purpose=purpose, **links)
    else:
        meter = Meter.for_settings(settings, purpose=purpose, **links)
    with metering(meter):
        yield meter


def require_meter(meter: Meter | None = None, *, what: str) -> Meter:
    """`meter`, else the current meter; UnmeteredCallError (naming `what`) when there is none.
    Called before an API call, so a call that could not be recorded is never sent."""
    found = meter if meter is not None else current_meter()
    if found is None:
        raise UnmeteredCallError(
            f"the {what} was not sent: no cost meter is in place to record it (a programming "
            "error: wrap the job in teacher.costs.cost_scope or pass a meter)"
        )
    return found


# -- recording a call -----------------------------------------------------------------------


def anthropic_usage(message: Any) -> TokenUsage:
    """The token breakdown of a Claude message (`message.usage`). The 1-hour share of the cache
    writes comes from `usage.cache_creation`; without it (and with cache writes) it is None."""
    usage = getattr(message, "usage", None)
    if usage is None:
        raise ValueError("the Claude reply carries no usage, so its cost cannot be recorded")
    written = getattr(usage, "cache_creation_input_tokens", None) or 0
    split = getattr(usage, "cache_creation", None)
    if split is not None:
        one_hour: int | None = getattr(split, "ephemeral_1h_input_tokens", None) or 0
    else:
        one_hour = 0 if written == 0 else None
    return TokenUsage(
        input_tokens=usage.input_tokens or 0,
        cache_write_tokens=written,
        cache_write_1h_tokens=one_hour,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        output_tokens=usage.output_tokens or 0,
    )


def record_reply(
    message: Any, *, meter: Meter, purpose: str, step: str
) -> LedgerEntry:
    """Record one Claude reply (its model, usage and id) through `meter`."""
    return meter.record(
        purpose=purpose,
        step=step,
        provider=ANTHROPIC,
        model=str(message.model),
        usage=anthropic_usage(message),
        response_id=getattr(message, "id", None),
    )


def record_embedding(
    response: Any, *, model: str, meter: Meter, purpose: str, step: str = "embedding"
) -> LedgerEntry:
    """Record one OpenAI embeddings response (`usage.prompt_tokens`) through `meter`; the model
    is the response's own when it names one, else `model` (the one requested)."""
    usage = getattr(response, "usage", None)
    tokens = getattr(usage, "prompt_tokens", None)
    if not isinstance(tokens, int):
        raise ValueError(
            "the embeddings response carries no usage, so its cost cannot be recorded"
        )
    return meter.record(
        purpose=purpose,
        step=step,
        provider=OPENAI,
        model=str(getattr(response, "model", None) or model),
        usage=TokenUsage(input_tokens=tokens),
    )


def link_message(session: Session, call_ids: Sequence[int], message_id: int) -> None:
    """Point the calls `call_ids` (a tutor reply's, recorded with its thread) at the stored
    message `message_id`, in the caller's transaction."""
    if call_ids:
        session.execute(
            update(ApiCall).where(ApiCall.id.in_(list(call_ids))).values(
                chat_message_id=message_id
            )
        )


# -- totals ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostPart:
    """One line of a breakdown."""

    key: str
    label: str
    total_usd: float
    calls: int
    unknown_price_calls: int
    estimate_calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "total_usd": round(self.total_usd, 6),
            "calls": self.calls,
            "unknown_price_calls": self.unknown_price_calls,
            "estimate": self.estimate_calls > 0,
        }


@dataclass(frozen=True)
class CostTotal:
    """The sum of the known costs of `calls` calls. `unknown_price_calls` of them have no known
    price (their models: `unknown_price_models`) and are not in `total_usd`; `estimate_calls`
    are priced at an upper bound, so with any, `total_usd` is itself an upper bound."""

    total_usd: float = 0.0
    calls: int = 0
    unknown_price_calls: int = 0
    unknown_price_models: tuple[str, ...] = ()
    estimate_calls: int = 0
    parts: tuple[CostPart, ...] = ()

    @property
    def estimate(self) -> bool:
        return self.estimate_calls > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_usd, 6),
            "calls": self.calls,
            "unknown_price_calls": self.unknown_price_calls,
            "unknown_price_models": list(self.unknown_price_models),
            "estimate": self.estimate,
            "estimate_calls": self.estimate_calls,
            "parts": [part.to_dict() for part in self.parts],
        }


@dataclass(frozen=True)
class _Row:
    key: str
    cost_usd: float | None
    is_estimate: bool
    model: str


def summarise(rows: Iterable[_Row], labels: dict[str, str]) -> CostTotal:
    """Total `rows`, with one part per key (most expensive first; labels from `labels`, else
    the key with underscores as spaces)."""
    rows = list(rows)
    groups: dict[str, list[_Row]] = {}
    for row in rows:
        groups.setdefault(row.key, []).append(row)

    def total(group: Sequence[_Row]) -> tuple[float, int, int]:
        return (
            sum(r.cost_usd for r in group if r.cost_usd is not None),
            sum(1 for r in group if r.cost_usd is None),
            sum(1 for r in group if r.is_estimate),
        )

    parts = []
    for key, group in groups.items():
        usd, unknown, estimates = total(group)
        parts.append(
            CostPart(key, labels.get(key, key.replace("_", " ")), usd, len(group), unknown,
                     estimates)
        )
    parts.sort(key=lambda p: (-p.total_usd, p.label))
    usd, unknown, estimates = total(rows)
    return CostTotal(
        total_usd=usd,
        calls=len(rows),
        unknown_price_calls=unknown,
        unknown_price_models=tuple(sorted({r.model for r in rows if r.cost_usd is None})),
        estimate_calls=estimates,
        parts=tuple(parts),
    )


@dataclass(frozen=True)
class ArtifactCost:
    """What a page or lab cost: `total` (every call linked to it, by purpose), `generation`
    (its generation job, by stage) and `since_generation` (everything else, by purpose)."""

    total: CostTotal
    generation: CostTotal
    since_generation: CostTotal

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.total.to_dict(),
            "generation": self.generation.to_dict(),
            "since_generation": self.since_generation.to_dict(),
        }


def artifact_cost(session: Session, artifact_id: int) -> ArtifactCost:
    """The ledger's totals for artifact `artifact_id` (see `ArtifactCost`)."""
    found = session.execute(
        select(
            ApiCall.purpose, ApiCall.cost_usd, ApiCall.is_estimate, ApiCall.model,
            GenerationRun.stage,
        )
        .outerjoin(GenerationRun, GenerationRun.id == ApiCall.generation_run_id)
        .where(ApiCall.artifact_id == artifact_id)
    ).all()
    generation = [r for r in found if r.purpose in GENERATION_PURPOSES]
    since = [r for r in found if r.purpose not in GENERATION_PURPOSES]
    return ArtifactCost(
        total=summarise(
            (_Row(r.purpose, r.cost_usd, r.is_estimate, r.model) for r in found), PURPOSE_LABELS
        ),
        generation=summarise(
            (_Row(r.stage or "other", r.cost_usd, r.is_estimate, r.model) for r in generation),
            STAGE_LABELS,
        ),
        since_generation=summarise(
            (_Row(r.purpose, r.cost_usd, r.is_estimate, r.model) for r in since), PURPOSE_LABELS
        ),
    )


def thread_cost(session: Session, thread_id: int) -> CostTotal:
    """The ledger's total for chat thread `thread_id` (tutor replies and the grading of its
    exercises), by purpose."""
    found = session.execute(
        select(ApiCall.purpose, ApiCall.cost_usd, ApiCall.is_estimate, ApiCall.model).where(
            ApiCall.chat_thread_id == thread_id
        )
    ).all()
    return summarise(
        (_Row(r.purpose, r.cost_usd, r.is_estimate, r.model) for r in found), PURPOSE_LABELS
    )


def message_costs(session: Session, message_ids: Sequence[int]) -> dict[int, CostTotal]:
    """The ledger's total for each of `message_ids` that has calls (a tutor reply's rounds)."""
    if not message_ids:
        return {}
    found = session.execute(
        select(
            ApiCall.chat_message_id, ApiCall.purpose, ApiCall.cost_usd, ApiCall.is_estimate,
            ApiCall.model,
        ).where(ApiCall.chat_message_id.in_(list(message_ids)))
    ).all()
    by_message: dict[int, list[_Row]] = {}
    for r in found:
        by_message.setdefault(r.chat_message_id, []).append(
            _Row(r.purpose, r.cost_usd, r.is_estimate, r.model)
        )
    return {mid: summarise(rows, PURPOSE_LABELS) for mid, rows in by_message.items()}


__all__ = [
    "GENERATION_PURPOSES",
    "PURPOSE_LABELS",
    "STAGE_LABELS",
    "ArtifactCost",
    "CallLinks",
    "CostPart",
    "CostTotal",
    "DatabaseSink",
    "LedgerEntry",
    "MemorySink",
    "Meter",
    "UnmeteredCallError",
    "anthropic_usage",
    "artifact_cost",
    "cost_scope",
    "current_meter",
    "link_message",
    "message_costs",
    "metering",
    "record_embedding",
    "record_reply",
    "require_meter",
    "summarise",
    "thread_cost",
]
