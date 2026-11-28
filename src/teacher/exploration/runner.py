"""Running an exploration: the four stages in order, as a background job (the web app) or in
the foreground (`teacher explore`).

`Explorer(planner, matcher, finder)` holds what a run needs: the `Planner` (brief and outline),
the `GraphMatcher` and milestone 1's `SourceFinder`. `build_explorer(models, finder=...,
embedder=...)` builds the default one (the generation model plans, the concept model judges the
match, the embedder is the concept graph's).

`start_exploration(settings, explorer, prompt, level, owner=...)` checks the topic and level
(ValueError), checks that the run can work at all before anything is spent
(`Explorer.check_available`: Claude credentials, the embedder's key, an up-to-date embedding
index; ExplorationUnavailableError) and creates the exploration (`running`), committed, so the
caller can start the job and show it at once.

`run_exploration(settings, exploration_id, explorer)` runs the stages; like
`teacher.generation.run_generation` it opens its own short sessions (committed at each step, so
a page polling the exploration sees its progress), never holds one open while a model or a
search runs, and never raises: any failure marks the exploration `failed` with the error text
(an `ExplorationError`'s message as it is; anything else, a bug, as "<class>: <message>", logged
with its traceback), and the stage it happened in is marked failed in the stage log.

1. `brief`: the reader's standings (`planner.reader_standings`) and the brief.
2. `outline`: the outline, checked and stored as the exploration's concepts (`planned`).
3. `graph_match`: every concept matched (`GraphMatcher.match`, read-only); a matched concept
   is `known`, a new one stays `planned`; each gets its coverage (`sources.coverage_of`, from
   the library sources that introduce the matched concept) and is marked for discovery when it
   is new or thinly covered.
4. `discovery`: the concepts that need sources are searched (`sources.discover`); each
   outcome is stored as it arrives (progress: concepts done of those searched). A concept whose
   search failed carries its error; the run still completes.

Then the exploration is `completed`. Every API call of the run is recorded in the cost ledger
with purpose `exploration` and the exploration (`teacher.costs.cost_scope`). Nothing is added to
the library or the concept graph.

Interruption. Runs execute inside the server process (or the `teacher explore` process), so one
cannot survive a restart: at startup the app marks every exploration still `running` as failed
(`fail_interrupted_explorations`, with `INTERRUPTED_ERROR` naming the stage), as it does for
generation runs; it is never left looking as if it ran. A run that finds its exploration no
longer running (marked failed meanwhile) stops with an error instead of writing on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import func, select

from teacher.config import ModelSettings, Settings
from teacher.costs import cost_scope
from teacher.db.engine import session_scope
from teacher.db.models import Exploration, SourceConcept
from teacher.discovery import LibraryCatalog
from teacher.embeddings import EmbeddingError
from teacher.exploration.common import (
    ExplorationError,
    check_level,
    check_prompt,
    unavailable,
)
from teacher.exploration.matching import GraphMatcher
from teacher.exploration.planner import Brief, DraftConcept, Planner, reader_standings
from teacher.exploration.sources import (
    INTRODUCES,
    SearchTarget,
    TargetOutcome,
    coverage_of,
    discover,
    needs_sources,
)
from teacher.managers import ExplorationManager

logger = logging.getLogger(__name__)

PURPOSE = "exploration"
INTERRUPTED_ERROR = "interrupted: the server stopped while the {stage} stage ran"


#: What `Explorer.check_available` embeds to prove the embedder answers.
EMBEDDER_PROBE_TEXT = "availability check"


class Explorer:
    """What a run needs (see the module docstring)."""

    def __init__(self, planner: Any, matcher: Any, finder: Any) -> None:
        if finder is None:
            raise ValueError("an Explorer needs a source finder")
        self.planner = planner
        self.matcher = matcher
        self.finder = finder

    def check_available(self, settings: Settings) -> None:
        """ExplorationUnavailableError when a run could not possibly succeed: no Claude client,
        no embedder key, concepts without an up-to-date embedding, or an embedder that does not
        answer. The last is the one request made: one short text is embedded, so a rejected key
        is found here and not after the brief and the outline were paid for."""
        self.planner.check_available()
        self.matcher.check_available()
        self.matcher.check_index(settings)
        try:
            self.matcher.embedder.embed([EMBEDDER_PROBE_TEXT])
        except EmbeddingError as exc:
            raise unavailable(f"the embedder does not answer, so nothing was started: {exc}") from exc


def build_explorer(
    models: ModelSettings, *, finder: Any, embedder: Any, client: Any = None
) -> Explorer:
    """The default explorer: a `Planner` with the generation model, a `GraphMatcher` with the
    concept model and `embedder` (the concept graph's), and `finder`."""
    return Explorer(
        Planner(client, model=models.generation),
        GraphMatcher(client, embedder=embedder, model=models.concept),
        finder,
    )


def start_exploration(
    settings: Settings, explorer: Explorer, prompt: str, level: str, *, owner: int | None
) -> int:
    """Check the request and the explorer, create the exploration (committed); its id."""
    text = check_prompt(prompt)
    check_level(level)
    explorer.check_available(settings)
    with session_scope(settings) as session:
        return ExplorationManager(session, settings, owner=owner).create(text, level).id


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, ExplorationError):
        return str(exc)
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


class _Run:
    """One run's bookkeeping: short sessions on the exploration, by id."""

    def __init__(self, settings: Settings, exploration_id: int, on_stage: Callable[[str], None] | None):
        self.settings = settings
        self.id = exploration_id
        self.on_stage = on_stage

    @contextmanager
    def edit(self) -> Iterator[tuple[ExplorationManager, Exploration]]:
        with session_scope(self.settings) as session:
            row = session.get(Exploration, self.id)
            if row is None:
                raise ExplorationError(f"exploration {self.id} no longer exists")
            yield ExplorationManager(session, self.settings, owner=row.user_id), row

    @contextmanager
    def stage(self, name: str, *, total: int = 0) -> Iterator[None]:
        with self.edit() as (manager, row):
            manager.begin_stage(row, name, total=total)
        if self.on_stage is not None:
            self.on_stage(name)
        yield
        with self.edit() as (manager, row):
            manager.end_stage(row)


def _introducing_sources(settings: Settings, concept_ids: set[int]) -> dict[int, int]:
    """How many library sources introduce each of `concept_ids` (`source_concepts.role`)."""
    if not concept_ids:
        return {}
    with session_scope(settings) as session:
        rows = session.execute(
            select(SourceConcept.concept_id, func.count(SourceConcept.id))
            .where(SourceConcept.concept_id.in_(concept_ids), SourceConcept.role == INTRODUCES)
            .group_by(SourceConcept.concept_id)
        ).all()
    return {concept_id: count for concept_id, count in rows}


def run_exploration(
    settings: Settings,
    exploration_id: int,
    explorer: Explorer,
    *,
    on_stage: Callable[[str], None] | None = None,
) -> None:
    """Run the exploration's stages (see the module docstring). Never raises: a failure is
    stored on the exploration. `on_stage(name)` is called as each stage starts."""
    run = _Run(settings, exploration_id, on_stage)
    try:
        with cost_scope(settings, purpose=PURPOSE, exploration_id=exploration_id):
            _stages(run, explorer)
    except BaseException as exc:
        error = _error_text(exc)
        if not isinstance(exc, ExplorationError):
            logger.exception("exploration %s failed", exploration_id)
        try:
            with run.edit() as (manager, row):
                if row.status == "running":
                    manager.fail(row, error)
        except Exception:  # the failure itself could not be stored: say so in the log
            logger.exception("exploration %s: its failure could not be recorded", exploration_id)
        if not isinstance(exc, Exception):
            raise


def _stages(run: _Run, explorer: Explorer) -> None:
    settings = run.settings
    with run.edit() as (_manager, row):
        prompt, level = row.prompt, row.level
        if row.status != "running" or row.stage is not None:
            raise ExplorationError(f"exploration {row.id} has already run ({row.status})")

    with run.stage("brief"):
        with session_scope(settings) as session:  # reads only
            studied = reader_standings(session, settings)
        brief: Brief = explorer.planner.brief(prompt, level, studied)
        with run.edit() as (manager, row):
            manager.store_brief(row, {**brief.to_dict(), "studied": [s.to_dict() for s in studied]})

    with run.stage("outline"):
        outline = explorer.planner.outline(brief)
        with run.edit() as (manager, row):
            manager.store_outline(
                row,
                questions=outline.questions(),
                steps=outline.steps,
                concepts=[c.to_dict() for c in outline.concepts],
            )
            for note in outline.notes:
                manager.add_note(row, note)

    drafts: list[DraftConcept] = list(outline.concepts)
    with run.stage("graph_match", total=len(drafts)):
        matches = explorer.matcher.match(settings, drafts)
        introducing = _introducing_sources(
            settings, {m.concept_id for m in matches if m.concept_id is not None}
        )
        targets: list[SearchTarget] = []
        with run.edit() as (manager, row):
            rows = {r.key: r for r in manager.concepts(row)}
            for draft, match in zip(drafts, matches, strict=True):
                coverage = coverage_of(match.concept_id, introducing.get(match.concept_id or 0, 0))
                manager.store_match(
                    rows[draft.key],
                    concept_id=match.concept_id,
                    method=match.verdict,
                    reason=match.reason,
                    detail=match.detail(),
                    coverage=coverage,
                    needs_sources=needs_sources(coverage),
                )
                if needs_sources(coverage):
                    targets.append(
                        SearchTarget(
                            key=draft.key, name=draft.name, aliases=draft.aliases,
                            summary=draft.summary, search_query=draft.search_query,
                            coverage=coverage,
                        )
                    )  # fmt: skip
            manager.advance(row, len(drafts))

    with run.stage("discovery", total=len(targets)):
        with session_scope(settings) as session:  # reads only
            catalog = LibraryCatalog.load(session)

        def store(outcome: TargetOutcome) -> None:
            with run.edit() as (manager, row):
                concept_row = next(r for r in manager.concepts(row) if r.key == outcome.target.key)
                manager.record_discovery(
                    row,
                    concept_row,
                    searches=outcome.searches(),
                    candidates=outcome.candidates(),
                    log=outcome.log(),
                    error=outcome.error,
                )
                manager.advance(row)

        discover(explorer.finder, catalog, targets, level, on_done=store)

    with run.edit() as (manager, row):
        manager.complete(row)


def fail_interrupted_explorations(settings: Settings) -> int:
    """Mark every exploration still `running` as failed (`INTERRUPTED_ERROR`); how many. Run
    at startup, when none can be running."""
    with session_scope(settings) as session:
        stale = list(session.scalars(select(Exploration).where(Exploration.status == "running")))
        for row in stale:
            manager = ExplorationManager(session, settings, owner=row.user_id)
            manager.fail(row, INTERRUPTED_ERROR.format(stage=row.stage or "first"))
        return len(stale)


__all__ = [
    "INTERRUPTED_ERROR",
    "PURPOSE",
    "Explorer",
    "build_explorer",
    "fail_interrupted_explorations",
    "run_exploration",
    "start_exploration",
]
