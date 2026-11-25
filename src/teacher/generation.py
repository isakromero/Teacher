"""Artifact generation: the `Generator` protocol, a `StubGenerator`, and `run_generation`.

A generator turns an artifact's linked sources into a list of `BlockDraft`s. It reports its
pipeline stages through `GenerationContext.stage(name)`, which records one `generation_runs` row per
stage and commits at entry and exit, so a client polling the artifact sees progress.

A generator may also link the source to the concept graph in a `concepts` stage, through a
`ConceptStage` collaborator (`teacher.concept_extraction.ConceptExtractor` in the
`ClaudeGenerator`; the model-free `StubConceptExtractor` in the `StubGenerator`). That stage
commits its own changes to the graph (they belong to the source, not to the page), and the
generator checks the collaborator in a `preflight` stage before anything is spent.

A generator may also produce the page's glossary: it hands the terms to
`GenerationContext.set_glossary(entries)` (the `ClaudeGenerator` does this in its `glossary`
stage). `run_generation` stores them in the same transaction as the blocks, so a `ready`
artifact has either its glossary or, if the generator set none (the `StubGenerator`), no
glossary at all (`artifacts.glossary_built_at` stays NULL).

Likewise for the concepts each block covers: a `block_concepts` stage, right after `concepts`,
runs a `BlockTaggingStage` (`teacher.block_tagging.BlockTagger` in the `ClaudeGenerator`; the
model-free `StubBlockTagger`, which tags nothing, in the `StubGenerator`) on the drafts and
hands the tags to `GenerationContext.set_block_concepts(plan)`; `run_generation` writes them
with the blocks (and stamps `artifacts.block_concepts_tagged_at`), or, if none were set, leaves
the page untagged (NULL). A failed tagging stage fails the generation, as a failed `concepts`
stage does.

Costs. Every API call of a generation is recorded in the cost ledger (`teacher.costs`) with
purpose `generation` (`lab_generation` for a lab), the artifact and, inside a stage, the
stage's run: `run_generation` and `GenerationContext.stage` set the scopes. A ledger row is
committed on its own, so a failed generation keeps the rows of the calls it made.

`run_generation(settings, artifact_slug, generator)` drives one job (typically from a FastAPI
background task, i.e. in another thread). It opens its own short sessions, validates every draft
with `teacher.blocks.validate_block_content`, writes the blocks and marks the artifact `ready`. On
any exception the open run is failed with the error text, the artifact is marked `failed`, and
nothing is re-raised. A failure outside a generator stage (e.g. an invalid draft) is recorded as a
failed run of the pseudo-stage `generate` or `store`, so the error is always visible in the runs.

A generator whose artifacts are not made of blocks (the lab generators in
`teacher.lab_generator`) brings its own store step: a `store(ctx, result)` method, which
`run_generation` calls with whatever `generate` returned instead of storing blocks. It must write
the result and mark the artifact `ready` itself; its failures are recorded like the block
store's, as a failed `store` run. Such a generator cannot set a glossary (the glossary is stored
with blocks), so a glossary it set anyway is an error rather than silently dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from teacher.blocks import is_gate_type, validate_block_content
from teacher.config import Settings
from teacher.costs import cost_scope
from teacher.db.engine import session_scope
from teacher.db.models import Artifact, ArtifactBlock, Concept, GenerationRun, SourceSegment
from teacher.glossary import GlossaryEntry
from teacher.labs import LAB_KIND
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    GenerationRunManager,
    GlossaryManager,
    KnowledgeSourceManager,
    NotFoundError,
    TagSpec,
)
from teacher.managers.artifact_blocks import BLOCKS_DIR

logger = logging.getLogger(__name__)

STAGE_GENERATE = "generate"  # pseudo-stage for a failure in generate() outside any ctx.stage()
STAGE_STORE = "store"  # pseudo-stage for a failure while validating or writing the drafts
PREFLIGHT_STAGE = "preflight"  # cheap checks before any model call (credentials, keys)
CONCEPTS_STAGE = "concepts"  # the source's concepts, linked into the concept graph
BLOCK_CONCEPTS_STAGE = "block_concepts"  # which of those concepts each block covers
MODEL_PARAM = "model"  # artifacts.params key: the model chosen for this artifact, if any


@dataclass(frozen=True)
class BlockDraft:
    """One block as produced by a generator, before validation."""

    type: str
    content: dict
    segment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BlockConceptsPlan:
    """The concept tags of a page being generated, stored with its blocks: `tags` are
    `(block number from 1, concept id, role, rationale)`, all made by `model`."""

    model: str
    tags: tuple[tuple[int, int, str, str], ...]


@dataclass(frozen=True)
class SourceInfo:
    """What a generator may know about one linked knowledge source."""

    slug: str
    title: str
    kind: str
    role: str
    primary_path: Path
    media_type: str


class StageHandle:
    """Yielded by `GenerationContext.stage`; lets the generator report token usage, the file
    holding the stage's output and a note for the owner on a stage that succeeded (something
    worth knowing that did not fail the generation, shown with the stage)."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.output_file: Path | None = None
        self.note: str | None = None

    def set_tokens(self, input: int | None, output: int | None) -> None:
        self.input_tokens = input
        self.output_tokens = output

    def set_output_file(self, path: Path) -> None:
        """Record `path` (inside the data root) on the run when the stage succeeds; it is stored
        relative to the root."""
        self.output_file = path

    def set_note(self, note: str) -> None:
        """Record `note` (non-blank text for the owner) on the run when the stage succeeds."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("a stage note must be non-blank text")
        self.note = note.strip()


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    name = exc.__class__.__name__
    return f"{name}: {message}" if message else name


def generation_purpose(kind: str) -> str:
    """The cost-ledger purpose of generating an artifact of `kind` (`teacher.costs`)."""
    return "lab_generation" if kind == LAB_KIND else "generation"


class GenerationContext:
    """Everything a generator gets: settings, the artifact slug, params, sources, and `stage()`."""

    def __init__(
        self,
        settings: Settings,
        artifact_slug: str,
        params: dict[str, Any],
        sources: list[SourceInfo],
        *,
        model: str,
    ) -> None:
        self.settings = settings
        self.artifact_slug = artifact_slug
        self.params = params
        self.sources = sources
        self.model = model
        self.open_run_id: int | None = None
        # The exception whose failure a stage already recorded (so it is not recorded twice).
        self.recorded_exception: BaseException | None = None
        # The page's glossary, if the generator built one (stored with the blocks).
        self.glossary: list[GlossaryEntry] | None = None
        # The concepts each block covers, if the generator tagged them (stored with the blocks).
        self.block_concepts: BlockConceptsPlan | None = None
        # Set by `run_generation`: what the generation's model calls are recorded with.
        self.artifact_id: int | None = None
        self.purpose: str = "generation"

    def set_glossary(self, entries: list[GlossaryEntry]) -> None:
        """Hand over the page's glossary; `run_generation` stores it with the blocks. An empty
        list is a glossary with no terms (not the same as never calling this)."""
        if not isinstance(entries, list) or not all(isinstance(e, GlossaryEntry) for e in entries):
            raise TypeError("set_glossary takes a list of GlossaryEntry")
        self.glossary = list(entries)

    def set_block_concepts(self, plan: BlockConceptsPlan) -> None:
        """Hand over the blocks' concept tags; `run_generation` stores them with the blocks and
        marks the page tagged. A plan with no tags is a page tagged with none (not the same as
        never calling this)."""
        if not isinstance(plan, BlockConceptsPlan):
            raise TypeError("set_block_concepts takes a BlockConceptsPlan")
        self.block_concepts = plan

    @contextmanager
    def stage(self, name: str, *, model: str | None = None) -> Iterator[StageHandle]:
        """Record a run for stage `name`: 'running' on entry, 'succeeded'/'failed' on exit.

        The run records `model` if given (a stage that calls a model of its own, such as
        `concepts`), else the generation model. Each transition is committed in its own short
        transaction. Stages cannot be nested. Every API call made inside the stage is recorded
        in the cost ledger with the artifact and this run (`teacher.costs.cost_scope`).
        """
        if self.open_run_id is not None:
            raise RuntimeError(f"stage {name!r} started while another stage is still open")
        with session_scope(self.settings) as session:
            artifact = ArtifactManager(session, self.settings).get_by_slug(self.artifact_slug)
            run = GenerationRunManager(session, self.settings).start(
                artifact, stage=name, model=model if model is not None else self.model
            )
            self.open_run_id = run.id
            artifact_id, purpose = artifact.id, generation_purpose(artifact.kind)
        handle = StageHandle()
        try:
            with cost_scope(
                self.settings, purpose=purpose, artifact_id=artifact_id,
                generation_run_id=run.id,
            ):
                yield handle
        except BaseException as exc:
            self.fail_open_run(_error_text(exc), handle)
            self.recorded_exception = exc
            raise
        with session_scope(self.settings) as session:
            run = session.get(GenerationRun, self.open_run_id)
            GenerationRunManager(session, self.settings).succeed(
                run,
                input_tokens=handle.input_tokens,
                output_tokens=handle.output_tokens,
                output_file=handle.output_file,
                note=handle.note,
            )
        self.open_run_id = None

    def session(self) -> AbstractContextManager[Session]:
        """A short transaction for a generator that writes to the database (e.g. source
        segments): commits on exit, rolls back on error. Keep it short; do not call the model
        inside it."""
        return session_scope(self.settings)

    @property
    def artifact_dir(self) -> Path:
        """Absolute path of the artifact's folder (where a generator may keep stage outputs)."""
        with session_scope(self.settings) as session:
            manager = ArtifactManager(session, self.settings)
            return manager.dir(manager.get_by_slug(self.artifact_slug))

    def fail_open_run(self, error: str, handle: StageHandle | None = None) -> None:
        """Fail the currently open run (if any) with `error`."""
        if self.open_run_id is None:
            return
        with session_scope(self.settings) as session:
            run = session.get(GenerationRun, self.open_run_id)
            if run is not None and run.status == "running":
                GenerationRunManager(session, self.settings).fail(
                    run,
                    error,
                    input_tokens=handle.input_tokens if handle else None,
                    output_tokens=handle.output_tokens if handle else None,
                )
        self.open_run_id = None


class Generator(Protocol):
    name: str  # stored in artifacts.generator
    model: str  # stored on each run

    def generate(self, ctx: GenerationContext) -> list[BlockDraft]:
        """Return the page's blocks; optionally call `ctx.set_glossary(...)` first. (A generator
        with its own `store` returns whatever that store takes.)"""
        ...

    # Optional: `model_for(params) -> str`, the model one artifact is generated with (see
    # `generation_model`). A generator without it always uses `model` and cannot be given a
    # per-artifact model.
    #
    # Optional: `store(ctx, result) -> None`, the generator's own store step (see the module
    # docstring); without it `run_generation` stores `result` as blocks.


def supports_model_choice(generator: Generator) -> bool:
    """Whether `generator` can generate one artifact with a model other than its default."""
    return callable(getattr(generator, "model_for", None))


def generation_model(generator: Generator, params: dict[str, Any]) -> str:
    """The model `generator` uses for an artifact with `params`: `generator.model_for(params)`
    if it has one, else `generator.model`. ValueError if `params` names a model but the
    generator cannot choose one (never a silent fallback to its default)."""
    if supports_model_choice(generator):
        return generator.model_for(params)  # type: ignore[attr-defined]
    if MODEL_PARAM in params:
        raise ValueError(
            f"generator {generator.name!r} does not support choosing a model, but the artifact "
            f"asks for {params[MODEL_PARAM]!r}"
        )
    return generator.model


class ConceptStage(Protocol):
    """What a generator's `concepts` stage needs (`teacher.concept_extraction.ConceptExtractor`,
    or `StubConceptExtractor`)."""

    name: str
    model: str  # recorded on the stage's run

    def check_available(self) -> None:
        """Raise now if `run` could not possibly succeed (missing credentials or keys); makes
        no model call."""
        ...

    def run(
        self,
        settings: Settings,
        source: SourceInfo | None,
        *,
        document: dict[str, Any] | None = None,
        stage: StageHandle | None = None,
    ) -> Any:
        """Extract the source's concepts and write them and the source's links (committed).
        `document` is the source's `document` content block when the caller already built one
        (so the request can share its prompt cache); `stage` receives the tokens. Returns a
        result whose `record(trigger)` is the stage's output file content."""
        ...


@dataclass(frozen=True)
class StubConceptRun:
    """What `StubConceptExtractor.run` returns: nothing was extracted or written."""

    source_slug: str | None

    def record(self, trigger: str) -> dict[str, Any]:
        return {
            "extractor": StubConceptExtractor.name,
            "trigger": trigger,
            "source": self.source_slug,
            "note": "the stub reads no source: no concepts were extracted and the concept "
            "graph and the source's links were left unchanged",
        }


class StubConceptExtractor:
    """The model-free `concepts` stage of the `StubGenerator`: deterministic, offline, no keys.

    It never reads the source and changes nothing: no concept is created, and the source's
    existing links are kept (an empty extraction would otherwise replace, i.e. erase, links an
    earlier real run made). `check_available` always passes. TEST/OFFLINE TOOL ONLY, like the
    `StubGenerator`.
    """

    name = "stub-concepts-v1"
    model = "none"

    def check_available(self) -> None:
        return None

    def run(
        self,
        settings: Settings,
        source: SourceInfo | None,
        *,
        document: dict[str, Any] | None = None,
        stage: StageHandle | None = None,
    ) -> StubConceptRun:
        return StubConceptRun(source.slug if source is not None else None)


class BlockTaggingStage(Protocol):
    """What a generator's `block_concepts` stage needs (`teacher.block_tagging.BlockTagger`, or
    `StubBlockTagger`)."""

    name: str
    model: str  # recorded on the stage's run

    def check_available(self) -> None:
        """Raise now if `run` could not possibly succeed (missing credentials); no model call."""
        ...

    def run(
        self,
        settings: Settings,
        sources: list[SourceInfo],
        blocks: list[tuple[str, dict[str, Any]]],
        *,
        stage: StageHandle | None = None,
    ) -> Any:
        """Tag the drafts `blocks` ((type, content), in page order) with the concepts linked to
        `sources`; writes nothing. Returns a result with `record(trigger)` (the stage's output
        file content) and `plan` (a BlockConceptsPlan, or None for nothing to store)."""
        ...


@dataclass(frozen=True)
class StubTaggingRun:
    """What `StubBlockTagger.run` returns: nothing was tagged, so there is no plan."""

    blocks: int
    plan: None = None

    def record(self, trigger: str) -> dict[str, Any]:
        return {
            "tagger": StubBlockTagger.name,
            "trigger": trigger,
            "blocks": self.blocks,
            "note": "the stub tags nothing: the page is left untagged "
            "(artifacts.block_concepts_tagged_at stays NULL)",
        }


class StubBlockTagger:
    """The model-free `block_concepts` stage of the `StubGenerator`: tags nothing and leaves the
    page untagged, so `teacher concepts tag-blocks` can tag it later. TEST/OFFLINE TOOL ONLY.
    (`teacher.block_tagging.stub_tagger()` is the offline tagger that does tag, by name.)"""

    name = "stub-block-concepts-none"
    model = "none"

    def check_available(self) -> None:
        return None

    def run(
        self,
        settings: Settings,
        sources: list[SourceInfo],
        blocks: list[tuple[str, dict[str, Any]]],
        *,
        stage: StageHandle | None = None,
    ) -> StubTaggingRun:
        return StubTaggingRun(len(blocks))


class StubGenerator:
    """A model-free generator: a fixed seven-block page built from the source title(s).

    Blocks: prose, multiple_choice (with `option_explanations`), prose, blurb, prediction, prose
    (the explanation the prediction leads into), and a prose block with role `critique`, so an
    offline page shows every block shape the writer produces except charts and interactables.
    Every prose block states its role. The verify stage checks each block's stored shape only
    (the writer's page rules, `teacher.claude_generator.page_violations`, need a wire page and
    an outline, which the stub has neither of). Stages: preflight, analyse, write,
    verify, concepts, block_concepts. The `concepts` stage runs `concept_extractor` (default
    `StubConceptExtractor`, which changes nothing) on the first source, and `block_concepts`
    runs `block_tagger` (default `StubBlockTagger`, which tags nothing) on the drafts; tests
    pass real ones on fake clients to drive extraction and tagging through a generation.

    TEST/OFFLINE TOOL ONLY: it never reads the source. It is not the app's default (that is
    `teacher.claude_generator.ClaudeGenerator`) and is used only when passed explicitly, e.g.
    `create_app(generator=StubGenerator())` in tests or for offline work.
    """

    name = "stub-v1"
    model = "none"

    def __init__(
        self,
        concept_extractor: ConceptStage | None = None,
        block_tagger: BlockTaggingStage | None = None,
    ) -> None:
        self.concept_extractor: ConceptStage = (
            concept_extractor if concept_extractor is not None else StubConceptExtractor()
        )
        self.block_tagger: BlockTaggingStage = (
            block_tagger if block_tagger is not None else StubBlockTagger()
        )

    def generate(self, ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage(PREFLIGHT_STAGE):
            self.concept_extractor.check_available()
            self.block_tagger.check_available()
        with ctx.stage("analyse"):
            titles = [s.title for s in ctx.sources] or [ctx.artifact_slug]
            title = titles[0]
        with ctx.stage("write"):
            drafts = self._page(title, titles)
        with ctx.stage("verify"):
            for draft in drafts:
                validate_block_content(draft.type, draft.content)
        with ctx.stage(CONCEPTS_STAGE, model=self.concept_extractor.model) as stage:
            source = ctx.sources[0] if ctx.sources else None
            self.concept_extractor.run(ctx.settings, source, stage=stage)
        with ctx.stage(BLOCK_CONCEPTS_STAGE, model=self.block_tagger.model) as stage:
            result = self.block_tagger.run(
                ctx.settings, ctx.sources, [(d.type, d.content) for d in drafts], stage=stage
            )
            if result.plan is not None:
                ctx.set_block_concepts(result.plan)
        return drafts

    @staticmethod
    def _page(title: str, titles: list[str]) -> list[BlockDraft]:
        listed = ", ".join(f"*{t}*" for t in titles)
        return [
            BlockDraft(
                "prose",
                {
                    "markdown": f"# {title}\n\nThis study page is a placeholder produced without "
                    f"a language model. It is based on: {listed}.",
                    "role": "explanation",
                },
            ),
            BlockDraft(
                "multiple_choice",
                {
                    "question": "Which source is this study page based on?",
                    "options": [title, "An unrelated text", "None at all"],
                    "private": {
                        "correct_index": 0,
                        "explanation": f"This page was generated from {title!r}.",
                        "option_explanations": [
                            f"Right: the page names {title!r} as its source.",
                            "Not quite: the page names its source in its first block.",
                            "Not quite: every study page is made from a source.",
                        ],
                    },
                },
            ),
            BlockDraft(
                "prose",
                {
                    "markdown": f"## Reflect\n\nThink about the main idea of *{title}* "
                    "before you continue.",
                    "role": "explanation",
                },
            ),
            BlockDraft(
                "blurb",
                {
                    "prompt": f"In your own words, summarise the main idea of {title}.",
                    "min_words": 20,
                    "private": {
                        "model_answer": f"A good summary of {title} names its central claim "
                        "and the evidence offered for it.",
                        "rubric": [
                            {"criterion": "States the central idea", "required": True},
                            {"criterion": "Mentions supporting evidence", "required": True},
                            {"criterion": "Uses own words", "required": False},
                        ],
                    },
                },
            ),
            BlockDraft(
                "prediction",
                {
                    "prompt": "Before you read on: what does this placeholder page do next?",
                    "options": [
                        "It explains the source in detail",
                        "It ends with a short critical note",
                    ],
                    "private": {
                        "correct_index": 1,
                        "resolution": "It ends with a short critical note, because the stub "
                        "never reads the source and so has nothing more to explain.",
                    },
                },
            ),
            BlockDraft(
                "prose",
                {
                    "markdown": "## Done\n\nYou have reached the end of what this placeholder "
                    "explains.",
                    "role": "explanation",
                },
            ),
            BlockDraft(
                "prose",
                {
                    "markdown": "## A critical look\n\nA page written by a language model "
                    "would step back from the source here, beyond what it says.",
                    "role": "critique",
                },
            ),
        ]


# -- the job ------------------------------------------------------------------------------


def source_infos(session: Session, settings: Settings, artifact: Artifact) -> list[SourceInfo]:
    """The artifact's linked sources as `SourceInfo`, ordered by source id (the first one is
    the source a generator teaches)."""
    source_manager = KnowledgeSourceManager(session, settings)
    return [
        SourceInfo(
            slug=source.slug,
            title=source.title,
            kind=source.kind,
            role=role,
            primary_path=source_manager.primary_path(source),
            media_type=source.media_type,
        )
        for source, role in ArtifactManager(session, settings).sources(artifact)
    ]


def _load_context(settings: Settings, artifact_slug: str, generator: Generator) -> GenerationContext | None:
    """Build the context, or None if the artifact is no longer `generating`."""
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.get_by_slug(artifact_slug)
        if artifact.status != "generating":
            logger.warning("artifact %s is %s, not generating; skipped", artifact_slug, artifact.status)
            return None
        sources = source_infos(session, settings, artifact)
        params = dict(artifact.params or {})
        artifact_id, kind = artifact.id, artifact.kind
    model = generation_model(generator, params)
    ctx = GenerationContext(settings, artifact_slug, params, sources, model=model)
    ctx.artifact_id, ctx.purpose = artifact_id, generation_purpose(kind)
    return ctx


def _store(
    settings: Settings,
    artifact_slug: str,
    drafts: Any,
    glossary: list[GlossaryEntry] | None = None,
    block_concepts: BlockConceptsPlan | None = None,
) -> None:
    """Validate every draft, then write all blocks (and the glossary and the blocks' concept
    tags, if any) and mark the artifact ready (one transaction)."""
    if not isinstance(drafts, list) or not drafts:
        raise ValueError("generator must return a non-empty list of BlockDraft")
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.get_by_slug(artifact_slug)
        linked = {source.id for source, _role in artifacts.sources(artifact)}
        prepared: list[tuple[str, dict[str, Any], list[SourceSegment]]] = []
        for index, draft in enumerate(drafts, start=1):
            if not isinstance(draft, BlockDraft):
                raise TypeError(f"draft {index} is a {draft.__class__.__name__}, not a BlockDraft")
            content = validate_block_content(draft.type, draft.content)
            segments: list[SourceSegment] = []
            for segment_id in draft.segment_ids:
                segment = session.get(SourceSegment, segment_id)
                if segment is None:
                    raise NotFoundError(f"draft {index}: no source segment with id {segment_id}")
                if segment.source_id not in linked:
                    raise ValueError(
                        f"draft {index}: segment {segment_id} belongs to a source that is not "
                        f"linked to {artifact_slug!r}"
                    )
                segments.append(segment)
            prepared.append((draft.type, content, segments))
        blocks = ArtifactBlockManager(session, settings)
        added: list[ArtifactBlock] = []
        for type_, content, segments in prepared:
            added.append(
                blocks.add(
                    artifact,
                    type=type_,
                    content=content,
                    is_gate=is_gate_type(type_),
                    segments=segments,
                )
            )
        if glossary is not None:
            GlossaryManager(session, settings).replace(artifact, glossary)
        if block_concepts is not None:
            _store_block_concepts(session, settings, artifact, added, block_concepts)
        artifacts.mark_ready(artifact)


def _store_block_concepts(
    session: Session,
    settings: Settings,
    artifact: Artifact,
    blocks: list[ArtifactBlock],
    plan: BlockConceptsPlan,
) -> None:
    """Write the tags of `plan` on the page's new `blocks` (block number n is `blocks[n-1]`).
    A block number off the page or a concept deleted since the tagging is an error."""
    specs = []
    for number, concept_id, role, rationale in plan.tags:
        if not 1 <= number <= len(blocks):
            raise ValueError(
                f"block concept tag on block {number}, but the page has {len(blocks)} blocks"
            )
        concept = session.get(Concept, concept_id)
        if concept is None:
            raise NotFoundError(
                f"concept id {concept_id}, tagged on block {number}, was deleted while the page "
                "was being generated"
            )
        specs.append(TagSpec(blocks[number - 1], concept, role, rationale))
    BlockConceptManager(session, settings).replace_model_tags(artifact, specs, plan.model)


def _record_failure(
    settings: Settings,
    artifact_slug: str,
    ctx: GenerationContext | None,
    phase: str,
    error: str,
    *,
    recorded: bool,
) -> None:
    """Fail the open run (or record a failed pseudo-stage run) and mark the artifact failed.

    `recorded` says a stage already stored this failure on its run. Failures while loading the
    context are not recorded as runs (there is no generator model or stage to attach them to).
    """
    if ctx is not None and ctx.open_run_id is not None:
        ctx.fail_open_run(error)
        recorded = True
    if phase == "load":
        recorded = True
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        try:
            artifact = artifacts.get_by_slug(artifact_slug)
        except NotFoundError:
            return
        if not recorded:
            runs = GenerationRunManager(session, settings)
            model = ctx.model if ctx is not None else "none"
            runs.fail(runs.start(artifact, stage=phase, model=model), error)
        _remove_orphan_block_files(settings, session, artifact)
        artifacts.mark_failed(artifact)


def _remove_orphan_block_files(settings: Settings, session: Session, artifact: Artifact) -> None:
    """Delete block files a rolled-back store left behind (files without a block row)."""
    known = {block.content_file for block in ArtifactBlockManager(session, settings).list(artifact)}
    blocks_dir = ArtifactManager(session, settings).dir(artifact) / BLOCKS_DIR
    if not blocks_dir.is_dir():
        return
    for entry in blocks_dir.iterdir():
        if entry.is_file() and f"{BLOCKS_DIR}/{entry.name}" not in known:
            entry.unlink(missing_ok=True)


def run_generation(settings: Settings, artifact_slug: str, generator: Generator) -> str | None:
    """Generate the blocks of artifact `artifact_slug`; never raises.

    Returns the artifact's final status (`ready` or `failed`), or None if the artifact was missing
    or not `generating` when the job started.
    """
    ctx: GenerationContext | None = None
    phase = "load"
    try:
        ctx = _load_context(settings, artifact_slug, generator)
        if ctx is None:
            return None
        phase = STAGE_GENERATE
        with cost_scope(settings, purpose=ctx.purpose, artifact_id=ctx.artifact_id):
            drafts = generator.generate(ctx)
        if ctx.open_run_id is not None:  # defensive: a stage left open without the context manager
            raise RuntimeError("generator returned while a stage was still open")
        phase = STAGE_STORE
        store = getattr(generator, "store", None)
        if store is None:
            _store(settings, artifact_slug, drafts, ctx.glossary, ctx.block_concepts)
        else:
            if ctx.glossary is not None:
                raise ValueError(
                    f"generator {generator.name!r} stores its own result but set a glossary, "
                    "which only the block store can store"
                )
            if ctx.block_concepts is not None:
                raise ValueError(
                    f"generator {generator.name!r} stores its own result but set block concept "
                    "tags, which only the block store can store"
                )
            store(ctx, drafts)
        return "ready"
    except Exception as exc:
        if phase == "load" and isinstance(exc, NotFoundError):
            logger.warning("generation of %s skipped: %s", artifact_slug, exc)
            return None
        error = _error_text(exc)
        recorded = ctx is not None and ctx.recorded_exception is exc
    logger.error("generation of %s failed in %s: %s", artifact_slug, phase, error)
    try:
        _record_failure(settings, artifact_slug, ctx, phase, error, recorded=recorded)
    except Exception:
        logger.exception("could not record the failure of %s", artifact_slug)
        return None
    return "failed"


__all__ = [
    "BLOCK_CONCEPTS_STAGE",
    "CONCEPTS_STAGE",
    "MODEL_PARAM",
    "PREFLIGHT_STAGE",
    "BlockConceptsPlan",
    "BlockDraft",
    "BlockTaggingStage",
    "ConceptStage",
    "GenerationContext",
    "Generator",
    "SourceInfo",
    "StageHandle",
    "StubBlockTagger",
    "StubConceptExtractor",
    "StubConceptRun",
    "StubGenerator",
    "StubTaggingRun",
    "generation_model",
    "generation_purpose",
    "run_generation",
    "source_infos",
    "supports_model_choice",
]
