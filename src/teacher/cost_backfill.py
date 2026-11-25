"""Backfill the cost ledger (`api_calls`) with the API calls made before it existed.

`teacher costs backfill` (`backfill`) adds one ledger row per past call it can find, priced
with today's table (`teacher.pricing`; the prices of the models involved did not change in
the days the app ran without the ledger). It is idempotent: every row it adds carries a
`backfill_key` naming the record it came from, and a key, or a response id, already in the
ledger is skipped. It never adds a row for something the live ledger already covers: a run or
chat message that live rows point at is skipped, and so is anything created at or after the
first live row (from then on every call was recorded as it was made).

Where the calls come from, and how exact they are:

- Generation runs (`generation_runs`, every stage of every page and lab, the review bank, the
  glossary rebuilds). A run's output file (`output_file`) is a stage record: one call with its
  full `usage` (analyse, write, review, revise, glossary, a lab's write and repair), a `calls`
  list (concepts, block_concepts), or the question check's `open_book` calls and
  `closed_book` call. Those calls are exact. The file is trusted only when its calls add up to
  no more than the run's own token totals (the files of the glossary and the block tags are
  rewritten by later runs, so a file may describe another run's calls); what the run counted
  beyond its file is one more row: in a question check, the judge's calls (the file does not
  keep their tokens), made on the judge model with no cache breakpoint, so exact. A run without
  a usable file gets one row from its totals, on the run's model.
- Readers' graded answers: `block_responses`, `chat_block_responses` and `review_attempts`
  whose grader details hold the judge's `input_tokens` / `output_tokens` (one call each).
- Tutor replies (`chat_messages` with tokens), one row per reply, linked to the thread, the
  message and the page.
- Reference extractions (`source_reference_extractions`: the latest run of each source).
- The command line's run records in `derived/`: `teacher concepts extract`
  (`<source>/concepts.json`), `concepts tag-blocks` (`_block_concepts/<page>.json`) and
  `concepts relate` (`_concept_relations/<concept>.json`), each a `calls` list with full usage
  (only the latest run of each is kept on disk).

Rows from totals. Before the ledger the app added fresh input, cache writes and cache reads
together, so a row made from a run's or a reply's totals has only that combined count. When
the request set no cache breakpoint (the judge, the review bank, block tagging, concept
relating, reference extraction), the combined count is all fresh input and the cost is exact.
Otherwise the row is an estimate: `input_tokens` holds the combined count, the cache columns
are 0, `is_estimate` is set, and `cost_usd` is an upper bound, every input token priced at the
cache-write rate of the longest TTL the request set (`teacher.pricing.upper_bound_cost`): the
5-minute rate for the generator's document calls, the 1-hour rate for the tutor. Pricing it
all as fresh input would not be an upper bound, since a cache write costs more than fresh
input. A row that adds up several calls on a model with a second rate card for long prompts
(Claude Haiku 5.5), whose total prompt is over the threshold, is an estimate too (each call may
have been on the cheaper card).

Not recoverable, so not added: calls whose run kept no tokens (a call that failed with an API
error, or a run that failed before recording its tokens), the embeddings (OpenAI usage was
never stored), and the metadata detection of uploaded papers (its tokens were never stored).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher import pricing
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import (
    ApiCall,
    Artifact,
    ArtifactBlock,
    BlockResponse,
    ChatBlock,
    ChatBlockResponse,
    ChatMessage,
    ChatThread,
    GenerationRun,
    KnowledgeSource,
    ReviewAttempt,
    ReviewItem,
    SourceReferenceExtraction,
)
from teacher.labs import LAB_KIND
from teacher.pricing import ANTHROPIC, TokenUsage

logger = logging.getLogger(__name__)

# Stages whose requests set a 5-minute cache breakpoint (the document, or the open-book page).
CACHED_STAGES = frozenset(
    {"analyse", "write", "review", "revise", "glossary", "concepts", "question_check", "repair"}
)
TUTOR_CACHE_TTL = "1h"  # the tutor's document breakpoint (`teacher.tutor`)
REVIEW_BANK_STAGE = "review_bank"
GLOSSARY_STAGE = "glossary"

# derived/ run records of the command line: (path pattern, purpose, what the stem names)
DERIVED_RECORDS = (
    ("_block_concepts/*.json", "block_tagging", "artifact"),
    ("_concept_relations/*.json", "concept_relating", None),
    ("*/concepts.json", "concept_extraction", "source"),
)


@dataclass(frozen=True)
class _Call:
    """One call found in a record: exact usage, or combined totals (`estimate` and the
    request's longest cache TTL say how to price it)."""

    model: str
    usage: TokenUsage
    response_id: str | None = None
    combined: bool = False  # only the combined input count is known
    cache_ttl: str | None = None  # the longest cache TTL the request set (combined only)
    aggregated: bool = False  # the totals of several calls


@dataclass
class BackfillReport:
    """What `backfill` added (or would add, with `dry_run`)."""

    added: Counter[str] = field(default_factory=Counter)  # rows by where they came from
    exact: int = 0
    estimate: int = 0
    unknown_price: int = 0
    unknown_models: set[str] = field(default_factory=set)
    skipped: int = 0  # already in the ledger (key or response id) or covered by live rows
    total_usd: float = 0.0
    dry_run: bool = False

    @property
    def rows(self) -> int:
        return sum(self.added.values())

    def lines(self) -> list[str]:
        verb = "Would add" if self.dry_run else "Added"
        out = [
            f"{verb} {self.rows} ledger row(s): {self.exact} exact, {self.estimate} estimate "
            f"(upper bound), {self.unknown_price} with no known price"
            + (f" ({', '.join(sorted(self.unknown_models))})" if self.unknown_models else "")
            + f"; ${self.total_usd:.4f} in total ({self.skipped} skipped: already recorded)."
        ]
        out += [f"  {where}: {count}" for where, count in sorted(self.added.items())]
        return out


def _usage(raw: Any) -> TokenUsage | None:
    """A stage record's `usage` dict as a TokenUsage (all cache writes at 5 minutes: every
    request with a stage record set only the default TTL), or None when it is not one."""
    if not isinstance(raw, Mapping):
        return None
    try:
        written = int(raw.get("cache_creation_input_tokens") or 0)
        return TokenUsage(
            input_tokens=int(raw["input_tokens"]),
            cache_write_tokens=written,
            cache_write_1h_tokens=0,
            cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
            output_tokens=int(raw["output_tokens"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _record_call(raw: Any) -> _Call | None:
    """A call in a stage record (`model`, `message_id`, `usage`), or None."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("model"), str):
        return None
    usage = _usage(raw.get("usage"))
    if usage is None:
        return None
    message_id = raw.get("message_id")
    return _Call(raw["model"], usage, message_id if isinstance(message_id, str) else None)


def record_calls(record: Any) -> list[_Call]:
    """Every call with full usage a stage or run record lists."""
    if not isinstance(record, Mapping):
        return []
    calls = []
    top = _record_call(record)
    if top is not None:
        calls.append(top)
    for key in ("calls", "open_book"):
        items = record.get(key)
        if isinstance(items, list):
            calls += [c for c in map(_record_call, items) if c is not None]
    closed = _record_call(record.get("closed_book"))
    if closed is not None:
        calls.append(closed)
    return calls


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _combined(call: _Call) -> tuple[int, int]:
    return call.usage.prompt_tokens, call.usage.output_tokens


def run_calls(settings: Settings, run: GenerationRun) -> list[_Call]:
    """The calls of generation run `run` (see the module docstring)."""
    if run.input_tokens is None and run.output_tokens is None:
        return []
    total_in, total_out = run.input_tokens or 0, run.output_tokens or 0
    calls: list[_Call] = []
    record: Any = None
    if run.output_file:
        record = _read_json(settings.data_root / run.output_file)
        calls = record_calls(record)
        used_in = sum(_combined(c)[0] for c in calls)
        used_out = sum(_combined(c)[1] for c in calls)
        if used_in > total_in or used_out > total_out:
            calls = []  # the file describes other calls (rewritten by a later run)
    rest_in = total_in - sum(_combined(c)[0] for c in calls)
    rest_out = total_out - sum(_combined(c)[1] for c in calls)
    if rest_in or rest_out:
        judged = record.get("judged") if calls and isinstance(record, Mapping) else None
        if run.stage == "question_check" and isinstance(judged, list) and judged:
            judge_model = record.get("judge_model")
            graded = sum(1 for j in judged if isinstance(j, Mapping) and j.get("graded"))
            calls.append(_Call(
                judge_model if isinstance(judge_model, str) else run.model,
                TokenUsage(input_tokens=rest_in, output_tokens=rest_out),
                combined=True, cache_ttl=None, aggregated=graded > 1,
            ))  # fmt: skip
        else:
            calls.append(_Call(
                run.model,
                TokenUsage(input_tokens=rest_in, output_tokens=rest_out),
                combined=True,
                cache_ttl="5m" if run.stage in CACHED_STAGES else None,
                aggregated=run.stage not in ("analyse", "write", "review", "revise", "repair",
                                             GLOSSARY_STAGE, REVIEW_BANK_STAGE),
            ))  # fmt: skip
    return calls


def _entry(
    call: _Call,
    *,
    key: str,
    purpose: str,
    step: str,
    created_at: datetime,
    links: Mapping[str, int | None],
) -> ApiCall:
    if call.combined:
        priced = pricing.upper_bound_cost(
            call.model, call.usage.input_tokens, call.usage.output_tokens,
            cache_ttl=call.cache_ttl,
        )
    else:
        priced = pricing.cost(call.model, call.usage)
    estimate = priced is not None and priced.upper_bound
    price = pricing.price_of(call.model)
    if (
        call.aggregated
        and price is not None
        and price.long_prompt is not None
        and call.usage.prompt_tokens > pricing.LONG_PROMPT_TOKENS
    ):
        estimate = True
    if priced is None and call.combined and call.cache_ttl is not None:
        estimate = True  # no price, and no breakdown either
    provider = price.provider if price is not None else ANTHROPIC
    return ApiCall(
        created_at=created_at,
        purpose=purpose,
        step=step,
        provider=provider,
        model=call.model,
        response_id=call.response_id,
        input_tokens=call.usage.input_tokens,
        cache_write_tokens=call.usage.cache_write_tokens,
        cache_write_1h_tokens=call.usage.cache_write_1h_tokens,
        cache_read_tokens=call.usage.cache_read_tokens,
        output_tokens=call.usage.output_tokens,
        cost_usd=priced.usd if priced is not None else None,
        priced_on=priced.checked_on if priced is not None else None,
        is_estimate=estimate,
        backfill_key=key,
        **links,
    )


def _generation_purposes(session: Session) -> dict[int, str]:
    """run id -> purpose for every generation run: a page's or lab's generation job is its runs
    up to and including its first glossary run (the job's last stage); review_bank runs and
    later glossary runs are builds of their own."""
    purposes: dict[int, str] = {}
    rows = session.execute(
        select(GenerationRun.id, GenerationRun.artifact_id, GenerationRun.stage, Artifact.kind)
        .join(Artifact, Artifact.id == GenerationRun.artifact_id)
        .order_by(GenerationRun.artifact_id, GenerationRun.id)
    ).all()
    job_over: set[int] = set()
    for run_id, artifact_id, stage, kind in rows:
        if stage == REVIEW_BANK_STAGE:
            purposes[run_id] = "review_bank"
        elif artifact_id in job_over:
            purposes[run_id] = GLOSSARY_STAGE if stage == GLOSSARY_STAGE else (
                "lab_generation" if kind == LAB_KIND else "generation"
            )
        else:
            purposes[run_id] = "lab_generation" if kind == LAB_KIND else "generation"
            if stage == GLOSSARY_STAGE:
                job_over.add(artifact_id)
    return purposes


def _judge_call(details: Any) -> _Call | None:
    """The judge call behind a grade's `details` (no cache breakpoint: exact), or None."""
    if not isinstance(details, Mapping):
        return None
    tokens_in, tokens_out = details.get("input_tokens"), details.get("output_tokens")
    model = details.get("model")
    if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
        return None
    if not isinstance(model, str):
        return None
    return _Call(model, TokenUsage(input_tokens=tokens_in, output_tokens=tokens_out),
                 combined=True, cache_ttl=None)


def _file_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None)


class _Backfill:
    def __init__(self, session: Session, settings: Settings, report: BackfillReport) -> None:
        self.session = session
        self.settings = settings
        self.report = report
        self.keys = set(
            session.scalars(select(ApiCall.backfill_key).where(ApiCall.backfill_key.is_not(None)))
        )
        self.response_ids = set(
            session.scalars(select(ApiCall.response_id).where(ApiCall.response_id.is_not(None)))
        )
        live = ApiCall.backfill_key.is_(None)
        self.cutoff: datetime | None = session.scalar(select(func.min(ApiCall.created_at)).where(live))
        self.live_runs = set(
            session.scalars(select(ApiCall.generation_run_id).where(
                live, ApiCall.generation_run_id.is_not(None)
            ))
        )  # fmt: skip
        self.live_messages = set(
            session.scalars(select(ApiCall.chat_message_id).where(
                live, ApiCall.chat_message_id.is_not(None)
            ))
        )  # fmt: skip

    def before_ledger(self, when: datetime | None) -> bool:
        return self.cutoff is None or (when is not None and when < self.cutoff)

    def add(self, source: str, call: _Call, **kwargs: Any) -> None:
        key = kwargs["key"]
        if key in self.keys or (call.response_id and call.response_id in self.response_ids):
            self.report.skipped += 1
            return
        row = _entry(call, **kwargs)
        self.session.add(row)
        self.keys.add(key)
        if call.response_id:
            self.response_ids.add(call.response_id)
        self.report.added[source] += 1
        if row.cost_usd is None:
            self.report.unknown_price += 1
            self.report.unknown_models.add(row.model)
        else:
            self.report.total_usd += row.cost_usd
        if row.is_estimate:
            self.report.estimate += 1
        elif row.cost_usd is not None:
            self.report.exact += 1

    # -- the sources --------------------------------------------------------------------------

    def runs(self) -> None:
        purposes = _generation_purposes(self.session)
        for run in self.session.scalars(select(GenerationRun).order_by(GenerationRun.id)):
            if run.id in self.live_runs:
                self.report.skipped += 1
                continue
            if not self.before_ledger(run.started_at):
                continue
            when = run.finished_at or run.started_at
            for n, call in enumerate(run_calls(self.settings, run), start=1):
                step = run.stage
                if call.combined and run.stage == "question_check" and call.cache_ttl is None:
                    step = "judge"
                self.add(
                    "generation runs", call,
                    key=f"generation_run:{run.id}:{n}", purpose=purposes[run.id], step=step,
                    created_at=when,
                    links={"artifact_id": run.artifact_id, "generation_run_id": run.id},
                )  # fmt: skip

    def graded_answers(self) -> None:
        rows = self.session.execute(
            select(BlockResponse, ArtifactBlock.artifact_id)
            .join(ArtifactBlock, ArtifactBlock.id == BlockResponse.block_id)
            .order_by(BlockResponse.id)
        ).all()
        for response, artifact_id in rows:
            call = _judge_call(response.details)
            if call is not None and self.before_ledger(response.created_at):
                self.add(
                    "graded answers", call, key=f"block_response:{response.id}",
                    purpose="grading", step="judge", created_at=response.created_at,
                    links={"artifact_id": artifact_id},
                )  # fmt: skip
        rows = self.session.execute(
            select(ChatBlockResponse, ChatThread.id, ChatThread.artifact_id)
            .join(ChatBlock, ChatBlock.id == ChatBlockResponse.block_id)
            .join(ChatMessage, ChatMessage.id == ChatBlock.message_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .order_by(ChatBlockResponse.id)
        ).all()
        for response, thread_id, artifact_id in rows:
            call = _judge_call(response.details)
            if call is not None and self.before_ledger(response.created_at):
                self.add(
                    "graded answers", call, key=f"chat_block_response:{response.id}",
                    purpose="grading", step="judge", created_at=response.created_at,
                    links={"artifact_id": artifact_id, "chat_thread_id": thread_id},
                )  # fmt: skip
        rows = self.session.execute(
            select(ReviewAttempt, ArtifactBlock.artifact_id)
            .join(ReviewItem, ReviewItem.id == ReviewAttempt.item_id)
            .join(ArtifactBlock, ArtifactBlock.id == ReviewItem.block_id)
            .order_by(ReviewAttempt.id)
        ).all()
        for attempt, artifact_id in rows:
            call = _judge_call(attempt.details)
            if call is not None and self.before_ledger(attempt.created_at):
                self.add(
                    "graded answers", call, key=f"review_attempt:{attempt.id}",
                    purpose="grading", step="judge", created_at=attempt.created_at,
                    links={"artifact_id": artifact_id},
                )  # fmt: skip

    def tutor_replies(self) -> None:
        rows = self.session.execute(
            select(ChatMessage, ChatThread.artifact_id)
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(ChatMessage.role == "assistant", ChatMessage.input_tokens.is_not(None))
            .order_by(ChatMessage.id)
        ).all()
        for message, artifact_id in rows:
            if message.id in self.live_messages:
                self.report.skipped += 1
                continue
            if not self.before_ledger(message.created_at):
                continue
            if not message.model:
                continue
            call = _Call(
                message.model,
                TokenUsage(input_tokens=message.input_tokens or 0,
                           output_tokens=message.output_tokens or 0),
                combined=True, cache_ttl=TUTOR_CACHE_TTL, aggregated=True,
            )  # fmt: skip
            self.add(
                "tutor replies", call, key=f"chat_message:{message.id}", purpose="tutor",
                step="tutor reply", created_at=message.created_at,
                links={"artifact_id": artifact_id, "chat_thread_id": message.thread_id,
                       "chat_message_id": message.id},
            )  # fmt: skip

    def reference_extractions(self) -> None:
        for run in self.session.scalars(
            select(SourceReferenceExtraction).order_by(SourceReferenceExtraction.source_id)
        ):
            if run.input_tokens is None or not self.before_ledger(run.started_at):
                continue
            call = _Call(
                run.model,
                TokenUsage(input_tokens=run.input_tokens, output_tokens=run.output_tokens or 0),
                combined=True, cache_ttl=None,
            )  # fmt: skip
            self.add(
                "reference extractions", call,
                key=f"reference_extraction:{run.source_id}:{run.run_number}",
                purpose="reference_extraction", step="references",
                created_at=run.finished_at or run.started_at,
                links={"knowledge_source_id": run.source_id},
            )  # fmt: skip

    def derived_records(self) -> None:
        root = self.settings.derived_dir
        if not root.is_dir():
            return
        artifacts = dict(self.session.execute(select(Artifact.slug, Artifact.id)).all())
        sources = dict(self.session.execute(select(KnowledgeSource.slug, KnowledgeSource.id)).all())
        for pattern, purpose, names in DERIVED_RECORDS:
            for path in sorted(root.glob(pattern)):
                when = _file_time(path)
                if not self.before_ledger(when):
                    continue
                stem = path.parent.name if names == "source" else path.stem
                links: dict[str, int | None] = {}
                if names == "artifact":
                    links["artifact_id"] = artifacts.get(stem)
                elif names == "source":
                    if stem not in sources:
                        continue  # not a source's folder
                    links["knowledge_source_id"] = sources[stem]
                record = _read_json(path)
                calls = record_calls({"calls": record.get("calls")}) if isinstance(
                    record, Mapping) else []  # fmt: skip
                relative = path.relative_to(self.settings.data_root).as_posix()
                for n, call in enumerate(calls, start=1):
                    self.add(
                        "command-line records", call, key=f"file:{relative}:{n}",
                        purpose=purpose, step=purpose.replace("_", " "), created_at=when,
                        links=links,
                    )  # fmt: skip


def backfill(settings: Settings, *, dry_run: bool = False) -> BackfillReport:
    """Add the past calls to the ledger in one transaction (rolled back with `dry_run`) and
    say what was added (see the module docstring)."""
    report = BackfillReport(dry_run=dry_run)
    with session_scope(settings) as session:
        work = _Backfill(session, settings, report)
        work.runs()
        work.graded_answers()
        work.tutor_replies()
        work.reference_extractions()
        work.derived_records()
        session.flush()
        if dry_run:
            session.rollback()
    return report


__all__ = [
    "CACHED_STAGES",
    "BackfillReport",
    "backfill",
    "record_calls",
    "run_calls",
]
