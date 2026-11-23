"""run_generation, GenerationContext.stage and StubGenerator, driven directly (no web app)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from teacher.blocks import validate_block_content
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import (
    BlockDraft,
    GenerationContext,
    StubGenerator,
    run_generation,
)
from teacher.glossary import GlossaryEntry
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    GenerationRunManager,
    GlossaryManager,
    KnowledgeSourceManager,
    SourceSegmentManager,
)

MakeArtifact = Callable[..., str]

PROSE = BlockDraft("prose", {"markdown": "Hello"})
MCQ = BlockDraft(
    "multiple_choice",
    {"question": "Q?", "options": ["a", "b"], "private": {"correct_index": 1, "explanation": "b"}},
)


@pytest.fixture
def make_source(settings: Settings, tmp_path: Path) -> Callable[..., int]:
    """`make_source(slug, title)` adds a knowledge source and returns its id."""

    def make(slug: str = "paper", title: str = "A Paper") -> int:
        path = tmp_path / f"{slug}.txt"
        path.write_text(f"text of {slug}\n", encoding="utf-8")
        with session_scope(settings) as session:
            source = KnowledgeSourceManager(session, settings).add(
                path, slug=slug, kind="note", title=title
            )
            return source.id

    return make


@pytest.fixture
def make_artifact(settings: Settings, make_source: Callable[..., int]) -> MakeArtifact:
    """`make_artifact(slug, params=...)`: a `generating` artifact linked to one new source."""

    def make(slug: str = "page", *, params: dict | None = None, source_slug: str = "paper") -> str:
        make_source(source_slug, "A Paper")
        with session_scope(settings) as session:
            source = KnowledgeSourceManager(session, settings).get_by_slug(source_slug)
            ArtifactManager(session, settings).create(
                slug=slug,
                kind="study_page",
                title="Study",
                generator="test",
                params=params,
                sources=[(source, "primary")],
            )
        return slug

    return make


def state(settings: Settings, slug: str) -> tuple[str, list[tuple], list[dict]]:
    """(artifact status, [(stage, status, error, in, out)], [block content]) after a job."""
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        runs = [
            (r.stage, r.status, r.error, r.input_tokens, r.output_tokens)
            for r in GenerationRunManager(session, settings).list(artifact)
        ]
        blocks = ArtifactBlockManager(session, settings)
        contents = [blocks.read_content(b) for b in blocks.list(artifact)]
        return artifact.status, runs, contents


class FakeGenerator:
    name = "fake"
    model = "fake-model"

    def __init__(self, generate: Callable[[GenerationContext], list]) -> None:
        self._generate = generate
        self.ctx: GenerationContext | None = None

    def generate(self, ctx: GenerationContext) -> list:
        self.ctx = ctx
        return self._generate(ctx)


# -- StubGenerator ------------------------------------------------------------------------


def test_stub_generator_happy_path(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()
    assert run_generation(settings, slug, StubGenerator()) == "ready"

    status, runs, contents = state(settings, slug)
    assert status == "ready"
    assert [(r[0], r[1]) for r in runs] == [
        ("preflight", "succeeded"),
        ("analyse", "succeeded"),
        ("write", "succeeded"),
        ("verify", "succeeded"),
        ("concepts", "succeeded"),
        ("block_concepts", "succeeded"),
    ]
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert {r.model for r in GenerationRunManager(session, settings).list(artifact)} == {"none"}
        assert artifact.block_concepts_tagged_at is None  # the stub tagger tags nothing
        blocks = ArtifactBlockManager(session, settings).list(artifact)
        assert [(b.key, b.type, b.is_gate) for b in blocks] == [
            ("b01", "prose", False),
            ("b02", "multiple_choice", True),
            ("b03", "prose", False),
            ("b04", "blurb", True),
            ("b05", "prediction", True),
            ("b06", "prose", False),
            ("b07", "prose", False),
        ]
    assert "A Paper" in contents[0]["markdown"]
    types = ["prose", "multiple_choice", "prose", "blurb", "prediction", "prose", "prose"]
    for block_type, content in zip(types, contents, strict=True):
        assert validate_block_content(block_type, content) == content
    # Every new shape the writer produces: roles, per-option feedback, a prediction (which
    # comes before the explanation it leads into) and a critique at the end.
    assert [c.get("role") for c in contents] == [
        "explanation", None, "explanation", None, None, "explanation", "critique",
    ]
    mc = contents[1]
    assert len(mc["private"]["option_explanations"]) == len(mc["options"])
    assert mc["options"][mc["private"]["correct_index"]] == "A Paper"
    assert contents[4]["private"]["resolution"]
    # The stub builds no glossary: none is stored and the artifact counts as never built.
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert artifact.glossary_built_at is None
        assert GlossaryManager(session, settings).list(artifact) == []


GLOSSARY_ENTRY = GlossaryEntry("Hello", "term", "A greeting.")


def test_glossary_set_by_a_generator_is_stored_with_the_blocks(
    settings: Settings, make_artifact: MakeArtifact
) -> None:
    def generate(ctx: GenerationContext) -> list:
        ctx.set_glossary([GLOSSARY_ENTRY])
        return [PROSE, MCQ]

    slug = make_artifact()
    assert run_generation(settings, slug, FakeGenerator(generate)) == "ready"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert GlossaryManager(session, settings).entries(artifact) == [GLOSSARY_ENTRY]
        assert artifact.glossary_built_at is not None


def test_glossary_is_not_stored_when_the_blocks_fail(
    settings: Settings, make_artifact: MakeArtifact
) -> None:
    def generate(ctx: GenerationContext) -> list:
        ctx.set_glossary([GLOSSARY_ENTRY, GLOSSARY_ENTRY])  # a duplicate fails the store
        return [PROSE, MCQ]

    slug = make_artifact()
    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    status, runs, contents = state(settings, slug)
    assert runs[-1][:2] == ("store", "failed") and "repeats term 1" in runs[-1][2]
    assert contents == []  # the blocks were rolled back with the glossary
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert artifact.glossary_built_at is None


def test_set_glossary_checks_its_argument(settings: Settings, make_artifact: MakeArtifact) -> None:
    ctx = GenerationContext(settings, make_artifact(), {}, [], model="m")
    with pytest.raises(TypeError, match="list of GlossaryEntry"):
        ctx.set_glossary([{"term": "x"}])  # type: ignore[list-item]
    assert ctx.glossary is None


def test_stub_generator_attributes() -> None:
    assert StubGenerator.name == "stub-v1"
    assert StubGenerator.model == "none"


# -- context ------------------------------------------------------------------------------


def test_context_contents_and_tokens(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact(params={"depth": 2})

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage("draft") as stage:
            stage.set_tokens(120, 45)
        with ctx.stage("polish") as stage:
            stage.set_tokens(10, None)
        return [PROSE, MCQ]

    gen = FakeGenerator(generate)
    assert run_generation(settings, slug, gen) == "ready"
    ctx = gen.ctx
    assert ctx is not None
    assert ctx.artifact_slug == slug and ctx.params == {"depth": 2}
    assert [(s.slug, s.title, s.kind, s.role, s.media_type) for s in ctx.sources] == [
        ("paper", "A Paper", "note", "primary", "text/plain")
    ]
    assert ctx.sources[0].primary_path.read_text(encoding="utf-8") == "text of paper\n"

    status, runs, contents = state(settings, slug)
    assert status == "ready"
    assert runs == [
        ("draft", "succeeded", None, 120, 45),
        ("polish", "succeeded", None, 10, None),
    ]
    assert contents[1]["private"]["correct_index"] == 1
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        assert GenerationRunManager(session, settings).token_totals(artifact) == (130, 45)


def test_stage_commits_on_entry(settings: Settings, make_artifact: MakeArtifact) -> None:
    """While a stage is open, another session already sees its 'running' row."""
    slug = make_artifact()
    seen: list[list[tuple[str, str]]] = []

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage("analyse"):
            with session_scope(settings) as session:
                artifact = ArtifactManager(session, settings).get_by_slug(slug)
                runs = GenerationRunManager(session, settings).list(artifact)
                seen.append([(r.stage, r.status) for r in runs])
        return [PROSE]

    assert run_generation(settings, slug, FakeGenerator(generate)) == "ready"
    assert seen == [[("analyse", "running")]]


def test_nested_stages_are_rejected(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage("outer"):
            with ctx.stage("inner"):
                pass
        return [PROSE]

    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    status, runs, _ = state(settings, slug)
    assert status == "failed"
    assert len(runs) == 1
    assert runs[0][:2] == ("outer", "failed")
    assert "RuntimeError" in runs[0][2]


# -- failures -----------------------------------------------------------------------------


def test_generator_raising_inside_stage(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage("analyse"):
            pass
        with ctx.stage("write") as stage:
            stage.set_tokens(7, 3)
            raise RuntimeError("model exploded")

    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    status, runs, contents = state(settings, slug)
    assert status == "failed"
    assert runs == [
        ("analyse", "succeeded", None, None, None),
        ("write", "failed", "RuntimeError: model exploded", 7, 3),
    ]
    assert contents == []


def test_generator_raising_outside_stage(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        raise KeyError("missing")

    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    status, runs, _ = state(settings, slug)
    assert status == "failed"
    assert runs == [("generate", "failed", "KeyError: 'missing'", None, None)]


def test_generator_swallowing_stage_error_then_failing(
    settings: Settings, make_artifact: MakeArtifact
) -> None:
    """A stage failure caught by the generator is kept; a later unrelated error is recorded too."""
    slug = make_artifact()

    def generate(ctx: GenerationContext) -> list[BlockDraft]:
        try:
            with ctx.stage("try"):
                raise ValueError("first")
        except ValueError:
            pass
        raise RuntimeError("second")

    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    _, runs, _ = state(settings, slug)
    assert [(r[0], r[1], r[2]) for r in runs] == [
        ("try", "failed", "ValueError: first"),
        ("generate", "failed", "RuntimeError: second"),
    ]


@pytest.mark.parametrize(
    "drafts",
    [
        [PROSE, BlockDraft("multiple_choice", {"question": "Q?", "options": ["only one"]})],
        [BlockDraft("video", {"url": "x"})],
        [PROSE, {"type": "prose", "content": {"markdown": "not a draft"}}],
        [],
        "not a list",
        [BlockDraft("prose", {"markdown": "x"}, segment_ids=(9999,))],
    ],
    ids=["invalid-content", "unknown-type", "not-a-draft", "empty", "not-a-list", "bad-segment"],
)
def test_invalid_drafts_fail_without_writing(
    settings: Settings, make_artifact: MakeArtifact, drafts: object
) -> None:
    slug = make_artifact()

    def generate(ctx: GenerationContext) -> object:
        with ctx.stage("write"):
            pass
        return drafts

    assert run_generation(settings, slug, FakeGenerator(generate)) == "failed"
    status, runs, contents = state(settings, slug)
    assert status == "failed"
    assert [(r[0], r[1]) for r in runs] == [("write", "succeeded"), ("store", "failed")]
    assert runs[1][2]
    assert contents == []
    assert list((settings.artifacts_dir / slug / "blocks").iterdir()) == []


def test_segment_ids_are_linked(
    settings: Settings, make_artifact: MakeArtifact, make_source: Callable[..., int]
) -> None:
    slug = make_artifact()
    other_id = make_source("other", "Other")
    with session_scope(settings) as session:
        segments = SourceSegmentManager(session, settings)
        sources = KnowledgeSourceManager(session, settings)
        locator = {"type": "text_span", "start_char": 0, "end_char": 4}
        own = segments.add(sources.get_by_slug("paper"), kind="section", label="s1", locator=locator)
        foreign = segments.add(sources.get(other_id), kind="section", label="s1", locator=locator)
        own_id, foreign_id = own.id, foreign.id

    ok = FakeGenerator(lambda ctx: [BlockDraft("prose", {"markdown": "x"}, segment_ids=(own_id,))])
    assert run_generation(settings, slug, ok) == "ready"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        blocks = ArtifactBlockManager(session, settings)
        assert [s.id for s in blocks.segments(blocks.list(artifact)[0])] == [own_id]

    second = make_artifact("page-2", source_slug="paper-2")
    bad = FakeGenerator(
        lambda ctx: [BlockDraft("prose", {"markdown": "x"}, segment_ids=(foreign_id,))]
    )
    assert run_generation(settings, second, bad) == "failed"
    _, runs, _ = state(settings, second)
    assert runs[-1][:2] == ("store", "failed") and "not linked" in runs[-1][2]


def test_write_failure_rolls_back_and_removes_files(
    settings: Settings, make_artifact: MakeArtifact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while writing blocks leaves no rows and no orphan files behind."""
    slug = make_artifact()
    real_mark_ready = ArtifactManager.mark_ready

    def boom(self: ArtifactManager, artifact: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(ArtifactManager, "mark_ready", boom)
    assert run_generation(settings, slug, FakeGenerator(lambda ctx: [PROSE, MCQ])) == "failed"
    monkeypatch.setattr(ArtifactManager, "mark_ready", real_mark_ready)

    status, runs, contents = state(settings, slug)
    assert status == "failed"
    assert runs == [("store", "failed", "OSError: disk full", None, None)]
    assert contents == []
    assert list((settings.artifacts_dir / slug / "blocks").iterdir()) == []


def test_missing_artifact_is_ignored(settings: Settings) -> None:
    assert run_generation(settings, "nope", StubGenerator()) is None


def test_artifact_not_generating_is_skipped(settings: Settings, make_artifact: MakeArtifact) -> None:
    slug = make_artifact()
    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        manager.mark_ready(manager.get_by_slug(slug))
    assert run_generation(settings, slug, StubGenerator()) is None
    status, runs, contents = state(settings, slug)
    assert (status, runs, contents) == ("ready", [], [])


# -- the concepts stage -------------------------------------------------------------------


def _concept_links(settings: Settings, source_slug: str = "paper") -> list[tuple[str, str, str]]:
    from teacher.managers import SourceConceptManager

    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug(source_slug)
        return [
            (link.concept.slug, link.role, link.resolution)
            for link in SourceConceptManager(session, settings).for_source(source)
        ]


def test_stub_concept_stage_changes_nothing(settings: Settings, make_artifact: MakeArtifact) -> None:
    """The offline stub needs no keys and never replaces (erases) links a real run made."""
    from teacher.generation import StubConceptExtractor
    from teacher.managers import ConceptManager, LinkSpec, SourceConceptManager

    slug = make_artifact()
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("paper")
        concept = ConceptManager(session, settings).create("attention", "Weights.", "paper")
        SourceConceptManager(session, settings).replace(
            source, [LinkSpec(concept, "uses", "created", "attention")]
        )
    generator = StubGenerator()
    assert isinstance(generator.concept_extractor, StubConceptExtractor)
    assert run_generation(settings, slug, generator) == "ready"
    _status, runs, _contents = state(settings, slug)
    assert runs[-2] == ("concepts", "succeeded", None, None, None)
    assert _concept_links(settings) == [("attention", "uses", "created")]
    assert StubConceptExtractor().run(settings, None).record("t")["source"] is None


def test_stub_generator_runs_a_real_concept_extractor(
    settings: Settings, make_artifact: MakeArtifact, fake_anthropic, fake_embedder
) -> None:
    from teacher.concept_extraction import ConceptExtractor

    slug = make_artifact()
    fake = fake_anthropic(fake_anthropic.concepts_reply(
        [{"name": "attention", "aliases": ["attn"], "summary": "Weights over inputs.",
          "role": "introduces", "evidence": "we attend"}],
        input_tokens=21, output_tokens=4,
    ))
    extractor = ConceptExtractor(fake, embedder=fake_embedder(), model="claude-test-concepts")
    assert run_generation(settings, slug, StubGenerator(extractor)) == "ready"
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        concepts_run = GenerationRunManager(session, settings).list(artifact)[-2]
        assert (concepts_run.stage, concepts_run.model, concepts_run.status) == (
            "concepts", "claude-test-concepts", "succeeded"
        )
        assert (concepts_run.input_tokens, concepts_run.output_tokens) == (21, 4)
    assert _concept_links(settings) == [("attention", "introduces", "created")]
    (call,) = fake.calls
    assert call["messages"][0]["content"][0]["source"]["data"] == "text of paper\n"


def test_a_failed_concept_stage_fails_the_generation(
    settings: Settings, make_artifact: MakeArtifact, fake_anthropic, fake_embedder
) -> None:
    from teacher.concept_extraction import ConceptExtractor

    slug = make_artifact()
    fake = fake_anthropic(fake_anthropic.concepts_reply([], input_tokens=9, output_tokens=1))
    extractor = ConceptExtractor(fake, embedder=fake_embedder())
    assert run_generation(settings, slug, StubGenerator(extractor)) == "failed"
    status, runs, contents = state(settings, slug)
    assert status == "failed" and contents == []
    stage, run_status, error, tokens_in, tokens_out = runs[-1]
    assert (stage, run_status, tokens_in, tokens_out) == ("concepts", "failed", 9, 1)
    assert error == (
        "ConceptOutputError: Concepts could not be extracted: Claude listed 0 concepts; "
        "between 1 and 40 are allowed"
    )
    assert _concept_links(settings) == []


def test_the_preflight_fails_before_anything_is_spent(
    settings: Settings, make_artifact: MakeArtifact, fake_anthropic, fake_embedder
) -> None:
    from teacher.concept_extraction import ConceptExtractor

    slug = make_artifact()
    embedder = fake_embedder()
    embedder.available = False
    fake = fake_anthropic()
    assert run_generation(settings, slug, StubGenerator(ConceptExtractor(fake, embedder=embedder))) == "failed"
    _status, runs, _contents = state(settings, slug)
    assert [(r[0], r[1]) for r in runs] == [("preflight", "failed")]
    assert "no OpenAI API key (fake)" in runs[0][2]
    assert fake.calls == [] and embedder.calls == []
