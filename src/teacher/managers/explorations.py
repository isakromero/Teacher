"""ExplorationManager: explorations, their outline concepts, searches and candidate sources.

Scope. Every exploration belongs to one account; the manager is made for one `owner` (a user
id, or None when the app runs without accounts and for `teacher explore`: then `user_id` is
NULL) and only ever sees that owner's explorations: another account's is "not found", never
shown or changed (as `teacher.managers.highlights` does for highlights).

Three groups of methods:

- The run (`teacher.exploration.runner`): `create`, the stage bookkeeping (`begin_stage`,
  `advance`, `end_stage`, `complete`, `fail`; each refuses an exploration that is no longer
  running, so a run that a restart marked failed cannot write on), and what each stage stores
  (`store_brief`, `store_outline`, `store_match`, `record_discovery`). `record_discovery`
  deduplicates across the whole exploration: a candidate found for a second concept (same
  registry id, arXiv id, DOI, or canonical address or landing address) is the same row, linked
  to both concepts with each concept's own rank and triage score.
- The reader's edits, allowed only on a `completed` exploration (StateConflictError, HTTP 409,
  otherwise): `rename_concept`, `set_dropped`, `reorder_steps` (a permutation of the steps,
  refused when it would put a prerequisite in the same or a later step than a concept that
  builds on it; dropped concepts are not checked) and `decide` (a candidate `approved`,
  `rejected` or back to `proposed`). They are stored and nothing else happens.
- For milestone 3 (ingesting approved sources, writing to the graph): `approved_candidates`
  (the approved candidates linked to at least one concept that is not dropped),
  `concepts_of(candidate)`, `mark_ingested(candidate, source)` (approved -> ingested, with the
  knowledge source it became) and `mark_grounded(concept_row, concept)` (planned or known ->
  grounded, linked to the graph concept), `mark_ungrounded(concept_row)` (back, when its
  grounding source is gone).
- Enrichment runs (`teacher.exploration.enrichment`): `enrichments`, `latest_enrichment`,
  `running_enrichment`, `create_enrichment` (refused unless the exploration is completed and no
  enrichment of it runs: StateConflictError; a partial UNIQUE index makes the database refuse a
  second running one too), the stage bookkeeping (`begin_enrichment_stage`,
  `advance_enrichment`, `end_enrichment_stage`, `complete_enrichment`, `fail_enrichment`; each
  refuses an enrichment that is no longer running) and what each stage stores
  (`record_ingest`, `record_ingest_failure`, `store_graph`, `store_prerequisite_links`,
  `store_grounding`). While an enrichment runs, the reader's edits are refused
  (StateConflictError).
- The roadmap's choices (`teacher.exploration.roadmap`), on a concept of a completed exploration
  that is not dropped (StateConflictError otherwise; allowed while an enrichment runs):
  `set_primary_source` (study it from another of its grounding sources, ValueError for a source
  that does not ground it; None goes back to the recommended one) and `set_marked_known` ("I
  know this", or taken back).

Like every manager it flushes and never commits.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concepts import normalize_name
from teacher.config import Settings
from teacher.db.models import (
    ENRICHMENT_STAGES,
    EXPLORATION_LEVELS,
    EXPLORATION_STAGES,
    GRAPH_OUTCOMES,
    Concept,
    Exploration,
    ExplorationCandidate,
    ExplorationConcept,
    ExplorationEnrichment,
    ExplorationLink,
    ExplorationSearch,
    KnowledgeSource,
    utcnow,
)
from teacher.managers.artifacts import _is_unique_violation, _require_persistent
from teacher.managers.errors import NotFoundError, StateConflictError

MAX_NAME_CHARS = 120
#: The decisions a reader may make on a candidate (`ingested` is milestone 3's, `mark_ingested`).
READER_DECISIONS = ("proposed", "approved", "rejected")


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def check_step_order(steps_of: Mapping[str, int], prerequisites: Mapping[str, Sequence[str]]) -> None:
    """ValueError unless every prerequisite (by key) of a concept in `steps_of` that is itself
    in `steps_of` is not in a later step (the same step is fine: the order within a step does
    not change); prerequisites not in `steps_of` (dropped concepts) are not checked."""
    for key, prereqs in prerequisites.items():
        if key not in steps_of:
            continue
        for prereq in prereqs:
            if prereq in steps_of and steps_of[prereq] > steps_of[key]:
                raise ValueError(
                    f"{key!r} would be in step {steps_of[key]} but builds on {prereq!r} in step "
                    f"{steps_of[prereq]}; a prerequisite must stay in an earlier step"
                )


def work_keys(
    *, candidate_key: str, arxiv_id: str | None, doi: str | None, urls: Sequence[str]
) -> set[str]:
    """The keys two candidates are the same work by: the registry id, the arXiv id, the DOI and
    each canonical address (`urls`, already canonical)."""
    keys = {f"id:{candidate_key}"}
    if arxiv_id:
        keys.add(f"arxiv:{arxiv_id}")
    if doi:
        keys.add(f"doi:{doi.lower()}")
    keys.update(f"url:{url}" for url in urls if url)
    return keys


class ExplorationManager:
    """Read and change explorations of one owner (see the module docstring)."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        owner: int | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if owner is not None and (isinstance(owner, bool) or not isinstance(owner, int)):
            raise TypeError("owner is a user id (int) or None")
        self.session = session
        self.settings = settings
        self.owner = owner
        self.clock = clock

    def _owned(self) -> Any:
        if self.owner is None:
            return Exploration.user_id.is_(None)
        return Exploration.user_id == self.owner

    # -- reading ------------------------------------------------------------------------------

    def get(self, exploration_id: int) -> Exploration:
        """The owner's exploration `exploration_id`; NotFoundError for any other."""
        if isinstance(exploration_id, bool) or not isinstance(exploration_id, int):
            raise NotFoundError(f"no exploration {exploration_id!r}")
        row = self.session.scalar(
            select(Exploration).where(Exploration.id == exploration_id, self._owned())
        )
        if row is None:
            raise NotFoundError(f"no exploration {exploration_id}")
        return row

    def list(self) -> builtins.list[Exploration]:
        """The owner's explorations, newest first."""
        return list(
            self.session.scalars(
                select(Exploration).where(self._owned()).order_by(Exploration.id.desc())
            )
        )

    def concepts(self, exploration: Exploration) -> builtins.list[ExplorationConcept]:
        """Its outline concepts in study order (by position)."""
        return list(
            self.session.scalars(
                select(ExplorationConcept)
                .where(ExplorationConcept.exploration_id == exploration.id)
                .order_by(ExplorationConcept.position)
            )
        )

    def concept(self, exploration: Exploration, concept_row_id: int) -> ExplorationConcept:
        row = self.session.get(ExplorationConcept, concept_row_id)
        if row is None or row.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no concept {concept_row_id}")
        return row

    def candidates(self, exploration: Exploration) -> builtins.list[ExplorationCandidate]:
        """Its candidate sources, in the order they were found."""
        return list(
            self.session.scalars(
                select(ExplorationCandidate)
                .where(ExplorationCandidate.exploration_id == exploration.id)
                .order_by(ExplorationCandidate.id)
            )
        )

    def candidate(self, exploration: Exploration, candidate_id: int) -> ExplorationCandidate:
        row = self.session.get(ExplorationCandidate, candidate_id)
        if row is None or row.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no candidate {candidate_id}")
        return row

    def links(self, exploration: Exploration) -> builtins.list[ExplorationLink]:
        """Every candidate-concept link of the exploration, by concept then rank."""
        return list(
            self.session.scalars(
                select(ExplorationLink)
                .join(ExplorationConcept, ExplorationConcept.id == ExplorationLink.outline_concept_id)
                .where(ExplorationConcept.exploration_id == exploration.id)
                .order_by(ExplorationConcept.position, ExplorationLink.rank)
            )
        )

    def searches(self, exploration: Exploration) -> builtins.list[ExplorationSearch]:
        """Its provider calls, in the order they were recorded."""
        return list(
            self.session.scalars(
                select(ExplorationSearch)
                .where(ExplorationSearch.exploration_id == exploration.id)
                .order_by(ExplorationSearch.id)
            )
        )

    # -- the run ------------------------------------------------------------------------------

    def create(self, prompt: str, level: str) -> Exploration:
        """A new exploration, `running`, before its first stage. `prompt` is stored as given
        (the caller checks it: `teacher.exploration.common.check_prompt`)."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("the topic is empty: say what to explore")
        if level not in EXPLORATION_LEVELS:
            raise ValueError(f"unknown level {level!r}; choose one of: {', '.join(EXPLORATION_LEVELS)}")
        row = Exploration(user_id=self.owner, prompt=prompt, level=level, status="running")
        self.session.add(row)
        self.session.flush()
        return row

    def _require_running(self, exploration: Exploration) -> None:
        if exploration.status != "running":
            raise StateConflictError(
                f"exploration {exploration.id} is {exploration.status}, not running"
                + (f" ({exploration.error})" if exploration.error else "")
            )

    def begin_stage(self, exploration: Exploration, stage: str, *, total: int = 0) -> None:
        self._require_running(exploration)
        if stage not in EXPLORATION_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        exploration.stage = stage
        exploration.progress_done = 0
        exploration.progress_total = total
        log = [dict(entry) for entry in exploration.stages]
        log.append(
            {"name": stage, "status": "running", "started_at": _iso(self.clock()),
             "finished_at": None, "error": None}
        )  # fmt: skip
        exploration.stages = log
        self.session.flush()

    def set_total(self, exploration: Exploration, total: int) -> None:
        self._require_running(exploration)
        exploration.progress_total = total
        self.session.flush()

    def advance(self, exploration: Exploration, done: int = 1) -> None:
        self._require_running(exploration)
        exploration.progress_done = min(exploration.progress_done + done, exploration.progress_total)
        self.session.flush()

    def _close_stage(self, exploration: Exploration, status: str, error: str | None) -> None:
        log = [dict(entry) for entry in exploration.stages]
        if log and log[-1]["status"] == "running":
            log[-1].update(status=status, finished_at=_iso(self.clock()), error=error)
            exploration.stages = log

    def end_stage(self, exploration: Exploration) -> None:
        self._require_running(exploration)
        self._close_stage(exploration, "succeeded", None)
        self.session.flush()

    def complete(self, exploration: Exploration) -> None:
        self._require_running(exploration)
        exploration.status = "completed"
        exploration.finished_at = self.clock()
        self.session.flush()

    def fail(self, exploration: Exploration, error: str) -> None:
        """Mark a running exploration failed with `error` (non-blank), and its open stage."""
        self._require_running(exploration)
        if not isinstance(error, str) or not error.strip():
            raise ValueError("a failed exploration needs an error message")
        self._close_stage(exploration, "failed", error.strip())
        exploration.status = "failed"
        exploration.error = error.strip()
        exploration.finished_at = self.clock()
        self.session.flush()

    def add_note(self, exploration: Exploration, note: str) -> None:
        if not note.strip():
            raise ValueError("a note must be non-blank text")
        exploration.notes = [*exploration.notes, note.strip()]
        self.session.flush()

    def store_brief(self, exploration: Exploration, brief: dict[str, Any]) -> None:
        self._require_running(exploration)
        exploration.brief = dict(brief)
        self.session.flush()

    def store_outline(
        self,
        exploration: Exploration,
        *,
        questions: Sequence[dict[str, Any]],
        steps: Sequence[str],
        concepts: Sequence[dict[str, Any]],
    ) -> builtins.list[ExplorationConcept]:
        """Store a checked outline (`teacher.exploration.planner.DraftOutline`, as plain data:
        `questions()`, `steps` and each concept's `to_dict()`)."""
        self._require_running(exploration)
        if self.concepts(exploration):
            raise StateConflictError(f"exploration {exploration.id} already has an outline")
        exploration.questions = [dict(q) for q in questions]
        exploration.steps = list(steps)
        rows = []
        for item in concepts:
            row = ExplorationConcept(
                exploration_id=exploration.id,
                key=item["key"],
                position=item["position"],
                step=item["step"],
                name=item["name"],
                drafted_name=item["name"],
                aliases=list(item["aliases"]),
                summary=item["summary"],
                prerequisites=list(item["prerequisites"]),
                search_query=item["search_query"],
                status="planned",
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def store_match(
        self,
        row: ExplorationConcept,
        *,
        concept_id: int | None,
        method: str,
        reason: str | None,
        detail: dict[str, Any] | None,
        coverage: str,
        needs_sources: bool,
    ) -> None:
        """The graph match of one concept: known (matched) or planned (new), its coverage, and
        whether discovery searches for it (`pending`) or not (`not_needed`)."""
        if (method == "new") != (concept_id is None):
            raise ValueError("a new concept has no matched concept, and a match has one")
        row.concept_id = concept_id
        row.match_method = method
        row.match_reason = reason
        row.match_detail = detail
        row.coverage = coverage
        row.status = "planned" if concept_id is None else "known"
        row.discovery_status = "pending" if needs_sources else "not_needed"
        self.session.flush()

    def record_discovery(
        self,
        exploration: Exploration,
        row: ExplorationConcept,
        *,
        searches: Sequence[dict[str, Any]],
        candidates: Sequence[dict[str, Any]],
        log: Sequence[dict[str, Any]],
        error: str | None,
    ) -> None:
        """Store one concept's discovery: its provider calls (`searches`: attempt, query,
        query_detail, provider, outcome, hits, error, warnings, seconds), its ranked candidates
        (`candidates`, best first: the candidate's fields, `canonical_urls` (its address first,
        then its landing address) to deduplicate by, and for this concept `attempt`, `triage`, `overall`, `fits`), its `log` and its
        `error` (None: done; else failed, the candidates found before it are kept). A
        candidate the exploration already has (see `work_keys`) is linked, not stored again."""
        self._require_running(exploration)
        if row.exploration_id != exploration.id:
            raise ValueError("the concept is not one of this exploration's")
        for search in searches:
            self.session.add(
                ExplorationSearch(
                    exploration_id=exploration.id,
                    outline_concept_id=row.id,
                    attempt=search["attempt"],
                    query=search["query"],
                    query_detail=dict(search["query_detail"]),
                    provider=search["provider"],
                    outcome=search["outcome"],
                    hits=search["hits"],
                    error=search["error"],
                    warnings=list(search["warnings"]),
                    seconds=max(0.0, float(search["seconds"])),
                )
            )
        # Imported here: teacher.discovery.registry imports teacher.managers.
        from teacher.discovery.registry import canonical_url

        index: dict[str, ExplorationCandidate] = {}
        for stored in self.candidates(exploration):
            urls = [stored.canonical_url]
            if stored.landing_url:
                urls.append(canonical_url(stored.landing_url))
            for key in work_keys(
                candidate_key=stored.candidate_key, arxiv_id=stored.arxiv_id, doi=stored.doi,
                urls=urls,
            ):  # fmt: skip
                index.setdefault(key, stored)
        for rank, item in enumerate(candidates, start=1):
            keys = work_keys(
                candidate_key=item["candidate_key"], arxiv_id=item["arxiv_id"], doi=item["doi"],
                urls=item["canonical_urls"],
            )  # fmt: skip
            found = next((index[k] for k in sorted(keys) if k in index), None)
            if found is None:
                found = ExplorationCandidate(
                    exploration_id=exploration.id,
                    candidate_key=item["candidate_key"],
                    url=item["url"],
                    canonical_url=item["canonical_urls"][0],
                    landing_url=item["landing_url"],
                    arxiv_id=item["arxiv_id"],
                    doi=item["doi"],
                    title=item["title"],
                    authors=list(item["authors"]),
                    year=item["year"],
                    summary=item["summary"],
                    found_by=list(item["found_by"]),
                    verdict=item["verdict"],
                    verdict_reason=item["verdict_reason"],
                    readable=item["verdict"] == "readable",
                    probe=dict(item["probe"]),
                    library=list(item["library"]),
                    decision="proposed",
                )
                self.session.add(found)
                self.session.flush()
                for key in keys:
                    index.setdefault(key, found)
            self.session.add(
                ExplorationLink(
                    candidate_id=found.id,
                    outline_concept_id=row.id,
                    rank=rank,
                    attempt=item["attempt"],
                    triage=item["triage"],
                    overall=item["overall"],
                    fits=item["fits"],
                )
            )
        row.discovery_log = [dict(entry) for entry in log]
        row.discovery_status = "failed" if error else "done"
        row.discovery_error = error
        self.session.flush()

    # -- the reader's edits -------------------------------------------------------------------

    def _require_completed(self, exploration: Exploration) -> None:
        if exploration.status != "completed":
            raise StateConflictError(
                f"exploration {exploration.id} is {exploration.status}: only a completed "
                "exploration can be edited"
            )
        if self.running_enrichment(exploration) is not None:
            raise StateConflictError(
                f"exploration {exploration.id} is being enriched: it cannot be edited until "
                "the enrichment ends"
            )

    def rename_concept(self, exploration: Exploration, row: ExplorationConcept, name: str) -> None:
        """Give the concept another name (one line, at most `MAX_NAME_CHARS` characters, not
        the name of another concept of the exploration). Its key, prerequisites, match and
        sources stay as they are (they were found for the drafted name)."""
        self._require_completed(exploration)
        if row.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no concept {row.id}")
        if not isinstance(name, str):
            raise ValueError("the name must be text")
        text = " ".join(name.split())
        if not text:
            raise ValueError("the name is blank")
        if len(text) > MAX_NAME_CHARS:
            raise ValueError(f"the name is longer than {MAX_NAME_CHARS} characters")
        norm = normalize_name(text)
        for other in self.concepts(exploration):
            if other.id != row.id and normalize_name(other.name) == norm:
                raise ValueError(f"another concept of this exploration is called {other.name!r}")
        row.name = text
        self.session.flush()

    def set_dropped(self, exploration: Exploration, row: ExplorationConcept, dropped: bool) -> None:
        """Drop the concept from the plan, or take it back (known again when it matched a
        graph concept, else planned). A grounded concept cannot be dropped."""
        self._require_completed(exploration)
        if row.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no concept {row.id}")
        if row.status == "grounded":
            raise StateConflictError(f"concept {row.name!r} is grounded and cannot be dropped")
        if dropped:
            row.status = "dropped"
        else:
            row.status = "known" if row.match_method in ("exact", "judged") else "planned"
        self.session.flush()

    def reorder_steps(self, exploration: Exploration, order: Sequence[int]) -> None:
        """Put the steps in a new order: `order` lists every current step number (1-based)
        once, in the new order. Concepts move with their step; positions are renumbered.
        ValueError for anything but a permutation, or one that would put a prerequisite in the
        same or a later step than a (not dropped) concept that builds on it."""
        self._require_completed(exploration)
        count = len(exploration.steps)
        if (
            not isinstance(order, list | tuple)
            or any(isinstance(n, bool) or not isinstance(n, int) for n in order)
            or sorted(order) != list(range(1, count + 1))
        ):
            raise ValueError(f"the order must list each of the steps 1 to {count} exactly once")
        new_step = {old: new for new, old in enumerate(order, start=1)}
        rows = self.concepts(exploration)
        active = {r.key: new_step[r.step] for r in rows if r.status != "dropped"}
        check_step_order(active, {r.key: r.prerequisites for r in rows})
        for row in rows:  # out of the way of the unique positions first
            row.position += 1000
        self.session.flush()
        ordered = sorted(rows, key=lambda r: (new_step[r.step], r.position))
        for position, row in enumerate(ordered, start=1):
            row.step = new_step[row.step]
            row.position = position
        exploration.steps = [exploration.steps[old - 1] for old in order]
        self.session.flush()

    def decide(
        self, exploration: Exploration, candidate: ExplorationCandidate, decision: str
    ) -> None:
        """The reader approves (ticks), rejects or un-decides (`proposed`) a candidate. An
        ingested candidate cannot be changed."""
        self._require_completed(exploration)
        if candidate.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no candidate {candidate.id}")
        if decision not in READER_DECISIONS:
            raise ValueError(
                f"unknown decision {decision!r}; choose one of: {', '.join(READER_DECISIONS)}"
            )
        if candidate.decision == "ingested":
            raise StateConflictError(f"candidate {candidate.id} is already ingested")
        if candidate.decision != decision:
            candidate.decision = decision
            candidate.decided_at = self.clock()
        self.session.flush()

    # -- milestone 3: ingesting and grounding -------------------------------------------------

    def concepts_of(self, candidate: ExplorationCandidate) -> builtins.list[ExplorationConcept]:
        """The concepts that found `candidate`, in study order (dropped ones included)."""
        return list(
            self.session.scalars(
                select(ExplorationConcept)
                .join(ExplorationLink, ExplorationLink.outline_concept_id == ExplorationConcept.id)
                .where(ExplorationLink.candidate_id == candidate.id)
                .order_by(ExplorationConcept.position)
            )
        )

    def approved_candidates(self, exploration: Exploration) -> builtins.list[ExplorationCandidate]:
        """The approved candidates linked to at least one concept that is not dropped, in the
        order they were found."""
        return [
            candidate
            for candidate in self.candidates(exploration)
            if candidate.decision == "approved"
            and any(c.status != "dropped" for c in self.concepts_of(candidate))
        ]

    def mark_ingested(self, candidate: ExplorationCandidate, source: KnowledgeSource) -> None:
        """An approved candidate became the library source `source`."""
        _require_persistent(source, "knowledge source")
        if candidate.decision != "approved":
            raise StateConflictError(
                f"candidate {candidate.id} is {candidate.decision}; only an approved candidate "
                "can be marked ingested"
            )
        candidate.decision = "ingested"
        candidate.knowledge_source_id = source.id
        candidate.decided_at = self.clock()
        self.session.flush()

    def mark_grounded(self, row: ExplorationConcept, concept: Concept) -> None:
        """The outline concept is now in the graph as `concept` (through an ingested source)."""
        _require_persistent(concept, "concept")
        if row.status not in ("planned", "known"):
            raise StateConflictError(
                f"concept {row.name!r} is {row.status}; only a planned or known concept can be "
                "grounded"
            )
        row.status = "grounded"
        row.concept_id = concept.id
        self.session.flush()

    def mark_ungrounded(self, row: ExplorationConcept) -> None:
        """A grounded concept whose grounding source is gone: known again when it matched a
        graph concept, else planned (as taking a dropped concept back)."""
        if row.status != "grounded":
            raise StateConflictError(f"concept {row.name!r} is {row.status}, not grounded")
        row.status = "known" if row.match_method in ("exact", "judged") else "planned"
        self.session.flush()

    # -- enrichment runs ----------------------------------------------------------------------

    def enrichments(self, exploration: Exploration) -> builtins.list[ExplorationEnrichment]:
        """Its enrichments, newest first."""
        return list(
            self.session.scalars(
                select(ExplorationEnrichment)
                .where(ExplorationEnrichment.exploration_id == exploration.id)
                .order_by(ExplorationEnrichment.id.desc())
            )
        )

    def latest_enrichment(self, exploration: Exploration) -> ExplorationEnrichment | None:
        found = self.enrichments(exploration)
        return found[0] if found else None

    def running_enrichment(self, exploration: Exploration) -> ExplorationEnrichment | None:
        return self.session.scalar(
            select(ExplorationEnrichment).where(
                ExplorationEnrichment.exploration_id == exploration.id,
                ExplorationEnrichment.status == "running",
            )
        )

    def create_enrichment(
        self, exploration: Exploration, plan: dict[str, Any]
    ) -> ExplorationEnrichment:
        """A new enrichment of a completed exploration, `running`, with the confirmed `plan`.
        StateConflictError when the exploration is not completed or one is running already."""
        if exploration.status != "completed":
            raise StateConflictError(
                f"exploration {exploration.id} is {exploration.status}: only a completed "
                "exploration can be enriched"
            )
        running = self.running_enrichment(exploration)
        if running is not None:
            raise StateConflictError(
                f"exploration {exploration.id} is being enriched already (enrichment "
                f"{running.id}); wait until it ends"
            )
        row = ExplorationEnrichment(exploration_id=exploration.id, status="running", plan=dict(plan))
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise StateConflictError(
                    f"exploration {exploration.id} is being enriched already; wait until it ends"
                ) from exc
            raise
        return row

    def _require_enriching(self, enrichment: ExplorationEnrichment) -> None:
        if enrichment.status != "running":
            raise StateConflictError(
                f"enrichment {enrichment.id} is {enrichment.status}, not running"
                + (f" ({enrichment.error})" if enrichment.error else "")
            )

    def begin_enrichment_stage(
        self, enrichment: ExplorationEnrichment, stage: str, *, total: int = 0
    ) -> None:
        self._require_enriching(enrichment)
        if stage not in ENRICHMENT_STAGES:
            raise ValueError(f"unknown enrichment stage {stage!r}")
        enrichment.stage = stage
        enrichment.progress_done = 0
        enrichment.progress_total = total
        log = [dict(entry) for entry in enrichment.stages]
        log.append(
            {"name": stage, "status": "running", "started_at": _iso(self.clock()),
             "finished_at": None, "error": None}
        )  # fmt: skip
        enrichment.stages = log
        self.session.flush()

    def advance_enrichment(self, enrichment: ExplorationEnrichment, done: int = 1) -> None:
        self._require_enriching(enrichment)
        enrichment.progress_done = min(
            enrichment.progress_done + done, enrichment.progress_total
        )
        self.session.flush()

    def _close_enrichment_stage(
        self, enrichment: ExplorationEnrichment, status: str, error: str | None
    ) -> None:
        log = [dict(entry) for entry in enrichment.stages]
        if log and log[-1]["status"] == "running":
            log[-1].update(status=status, finished_at=_iso(self.clock()), error=error)
            enrichment.stages = log

    def end_enrichment_stage(self, enrichment: ExplorationEnrichment) -> None:
        self._require_enriching(enrichment)
        self._close_enrichment_stage(enrichment, "succeeded", None)
        self.session.flush()

    def complete_enrichment(
        self, enrichment: ExplorationEnrichment, summary: dict[str, Any]
    ) -> None:
        self._require_enriching(enrichment)
        enrichment.status = "completed"
        enrichment.summary = dict(summary)
        enrichment.finished_at = self.clock()
        self.session.flush()

    def fail_enrichment(self, enrichment: ExplorationEnrichment, error: str) -> None:
        """Mark a running enrichment failed with `error` (non-blank), and its open stage."""
        self._require_enriching(enrichment)
        if not isinstance(error, str) or not error.strip():
            raise ValueError("a failed enrichment needs an error message")
        self._close_enrichment_stage(enrichment, "failed", error.strip())
        enrichment.status = "failed"
        enrichment.error = error.strip()
        enrichment.finished_at = self.clock()
        self.session.flush()

    def record_ingest(
        self, candidate: ExplorationCandidate, source: KnowledgeSource, detail: dict[str, Any]
    ) -> None:
        """An approved candidate is in the library as `source` (added now or held already):
        `mark_ingested`, with what the attempt did; its last error is cleared."""
        candidate.ingest_error = None
        self.mark_ingested(candidate, source)
        candidate.ingest_detail = dict(detail)
        self.session.flush()

    def record_ingest_failure(
        self,
        candidate: ExplorationCandidate,
        error: str,
        detail: dict[str, Any],
        *,
        source: KnowledgeSource | None = None,
    ) -> None:
        """The attempt to ingest an approved candidate failed with `error`: it stays approved
        (the next enrichment tries it again), with the error and what the attempt did. `source`:
        the library source it was stored as or found to be before the failure (e.g. its concept
        extraction failed), which the next attempt links instead of fetching it again."""
        if candidate.decision != "approved":
            raise StateConflictError(
                f"candidate {candidate.id} is {candidate.decision}; only an approved candidate "
                "is ingested"
            )
        if not isinstance(error, str) or not error.strip():
            raise ValueError("a failed ingest needs an error message")
        if source is not None:
            _require_persistent(source, "knowledge source")
            candidate.knowledge_source_id = source.id
        candidate.ingest_error = error.strip()
        candidate.ingest_detail = dict(detail)
        self.session.flush()

    def store_graph(
        self, row: ExplorationConcept, concept: Concept, *, outcome: str, reason: str | None
    ) -> None:
        """The outline concept's graph concept, and how it got it (`GRAPH_OUTCOMES`)."""
        _require_persistent(concept, "concept")
        if outcome not in GRAPH_OUTCOMES:
            raise ValueError(f"unknown graph outcome {outcome!r}")
        if row.status == "dropped":
            raise StateConflictError(f"concept {row.name!r} is dropped: it is not written")
        row.concept_id = concept.id
        row.graph_outcome = outcome
        row.graph_reason = reason
        self.session.flush()

    def store_prerequisite_links(
        self, row: ExplorationConcept, links: Sequence[dict[str, Any]]
    ) -> None:
        row.prerequisite_links = [dict(link) for link in links]
        self.session.flush()

    def store_grounding(
        self, row: ExplorationConcept, sources: Sequence[dict[str, Any]]
    ) -> None:
        row.grounding = [dict(source) for source in sources]
        self.session.flush()

    # -- milestone 4: the reader's roadmap choices --------------------------------------------

    def _require_roadmap(self, exploration: Exploration, row: ExplorationConcept) -> None:
        """A roadmap choice needs a completed exploration and one of its concepts that is not
        dropped. Unlike the plan's edits it is allowed while an enrichment runs: it changes
        nothing an enrichment reads or writes."""
        if row.exploration_id != exploration.id:
            raise NotFoundError(f"exploration {exploration.id} has no concept {row.id}")
        if exploration.status != "completed":
            raise StateConflictError(
                f"exploration {exploration.id} is {exploration.status}: only a completed "
                "exploration has a roadmap"
            )
        if row.status == "dropped":
            raise StateConflictError(
                f"concept {row.name!r} is dropped: it is not on the roadmap (restore it on the "
                "review page first)"
            )

    def set_primary_source(
        self, exploration: Exploration, row: ExplorationConcept, source: KnowledgeSource | None
    ) -> None:
        """Study the concept from `source` instead of the recommended source, or (None) go back
        to the recommended one. `source` must be one of the concept's grounding sources as the
        graph is now (`teacher.exploration.grounding.grounding_of`: a library source linked to
        its graph concept with a grounding role); ValueError otherwise, naming the ones that
        are."""
        self._require_roadmap(exploration, row)
        if source is None:
            row.primary_source_id = None
            self.session.flush()
            return
        _require_persistent(source, "knowledge source")
        if row.concept_id is None:
            raise ValueError(
                f"concept {row.name!r} is not in your concept graph yet, so no source grounds "
                "it: enrich the exploration first"
            )
        # Imported here: teacher.exploration imports teacher.managers.
        from teacher.exploration.grounding import grounding_of

        grounding = [s for s in grounding_of(self.session, row.concept_id) if s.grounds]
        if not any(s.source_id == source.id for s in grounding):
            choices = ", ".join(s.slug for s in grounding) or "none"
            raise ValueError(
                f"{source.slug!r} does not ground concept {row.name!r} (it does not introduce "
                f"it); choose one of its grounding sources: {choices}"
            )
        row.primary_source_id = source.id
        self.session.flush()

    def set_marked_known(
        self, exploration: Exploration, row: ExplorationConcept, known: bool
    ) -> None:
        """Mark the concept "I know this" (the roadmap counts it done), or take the mark back."""
        self._require_roadmap(exploration, row)
        if not isinstance(known, bool):
            raise ValueError("known must be true or false")
        if known and row.marked_known_at is None:
            row.marked_known_at = self.clock()
        elif not known:
            row.marked_known_at = None
        self.session.flush()


__all__ = ["READER_DECISIONS", "ExplorationManager", "check_step_order", "work_keys"]
