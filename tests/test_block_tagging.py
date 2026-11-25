"""The block tagger (`teacher.block_tagging`) with fake Claude clients (no network): the request,
the hard checks on the reply, tagging an existing page (write, re-check, retag), the stub, the
`block_concepts` generation stage, the manager's rules and a concept merge moving the tags."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import anthropic
import pytest
from sqlalchemy import select

from teacher.block_tagging import (
    ERROR_PREFIX,
    MAX_CONCEPTS_PER_BLOCK,
    MAX_RATIONALE_CHARS,
    SYSTEM_PROMPT,
    TAG_MAX_TOKENS,
    TAGGING_FORMAT,
    BlockTagger,
    TaggingChangedError,
    TaggingInputError,
    TaggingOutputError,
    TaggingUnavailableError,
    stub_tagger,
)
from teacher.claude_generator import DEFAULT_TIMEOUT, EFFORT
from teacher.config import DEFAULT_CONCEPT_MODEL, Settings
from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, BlockConcept, Concept
from teacher.generation import StubGenerator, run_generation
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockConceptManager,
    ConceptManager,
    DuplicateError,
    GenerationRunManager,
    NotFoundError,
    TagSpec,
)

MODEL = "claude-test-concepts"


def reply(fake: Any, blocks: dict[int, list[tuple[int, str, str]]], **kwargs: Any) -> Any:
    """A `WireTagging` reply: block number -> [(concept id, role, rationale)]."""
    body = {
        "blocks": [
            {
                "block": number,
                "concepts": [
                    {"concept": c, "role": role, "rationale": why} for c, role, why in items
                ],
            }
            for number, items in blocks.items()
        ]
    }
    kwargs.setdefault("model", MODEL)
    kwargs.setdefault("input_tokens", 900)
    kwargs.setdefault("output_tokens", 120)
    return fake.reply(text=json.dumps(body), **kwargs)


def full_reply(fake: Any, lib: Any, **kwargs: Any) -> Any:
    a, s, t = (lib.concepts[k] for k in ("attention", "softmax", "transformer"))
    return reply(
        fake,
        {
            1: [(a, "main", "It explains attention.")],
            2: [(a, "main", "The question asks what attention computes."), (s, "incidental", "Softmax is implied.")],
            3: [(s, "main", "It explains the softmax.")],
            4: [(t, "main", "It asks about transformers."), (a, "incidental", "It names attention.")],
            5: [(s, "main", "It asks about the softmax.")],
        },
        **kwargs,
    )


def stored(settings: Settings, page: str = "study-paper") -> tuple[datetime | None, list[tuple]]:
    """(the page's block_concepts_tagged_at, [(block key, concept slug, role, origin, model)])."""
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(page)
        rows = [
            (block.key, concept.slug, tag.role, tag.origin, tag.model)
            for block, tag, concept in BlockConceptManager(session, settings).for_artifact(artifact)
        ]
        return artifact.block_concepts_tagged_at, rows


# -- tagging an existing page --------------------------------------------------------------------


def test_tag_page_writes_every_tag_and_marks_the_page(tag_library, fake_anthropic) -> None:
    lib = tag_library
    fake = fake_anthropic(full_reply(fake_anthropic, lib))
    run = BlockTagger(fake, model=MODEL, timeout=42.0).tag_page(lib.settings, lib.page)

    tagged_at, rows = stored(lib.settings)
    assert tagged_at is not None and run.tagged_at == tagged_at
    assert rows == [
        ("b01", "attention", "main", "model", MODEL),
        ("b02", "attention", "main", "model", MODEL),
        ("b02", "softmax", "incidental", "model", MODEL),
        ("b03", "softmax", "main", "model", MODEL),
        ("b04", "transformer", "main", "model", MODEL),
        ("b04", "attention", "incidental", "model", MODEL),
        ("b05", "softmax", "main", "model", MODEL),
    ]
    assert (run.added, run.removed, run.kept_user) == (7, 0, 0)
    counts = run.tagging.counts()
    assert counts == {
        "blocks": 5, "questions": 3, "tags": 7, "main": 5, "incidental": 2, "concepts_used": 3,
        "concepts_offered": 4, "blocks_untagged": 0, "questions_with_main": 3,
    }
    assert (run.tagging.input_tokens, run.tagging.output_tokens) == (900, 120)

    (call,) = fake.calls
    assert call["model"] == MODEL and call["max_tokens"] == TAG_MAX_TOKENS
    assert call["system"] == SYSTEM_PROMPT and call["timeout"] == 42.0
    assert call["output_config"] == {"effort": EFFORT, "format": TAGGING_FORMAT}
    assert set(call) == {"model", "max_tokens", "system", "messages", "output_config", "timeout"}
    (content,) = call["messages"][0]["content"]
    text = content["text"]
    concepts = json.loads(text.split("<concepts>\n", 1)[1].split("\n</concepts>", 1)[0])
    assert [(c["id"], c["name"], c["in_source"]) for c in concepts] == [
        (lib.concepts["attention"], "attention", "introduces"),
        (lib.concepts["dot-product"], "dot product", "assumes"),
        (lib.concepts["softmax"], "softmax", "uses"),
        (lib.concepts["transformer"], "transformer", "uses"),
    ]
    blocks = json.loads(text.split("<blocks>\n", 1)[1].split("\n</blocks>", 1)[0])
    assert [(b["number"], b["key"], b["type"]) for b in blocks] == [
        (1, "b01", "prose"), (2, "b02", "multiple_choice"), (3, "b03", "prose"),
        (4, "b04", "blurb"), (5, "b05", "multiple_choice"),
    ]
    assert blocks[1]["content"]["private"]["explanation"] == "Attention weighs the values."

    record = run.record("cli")
    assert record["tagger"] == "claude-block-concepts-v1" and record["trigger"] == "cli"
    assert record["page"] == {"slug": "study-paper", "title": "Study: The Paper"}
    assert record["written"]["added"] == 7 and record["calls"][0]["message_id"] == "msg_fake"
    assert record["output"]["blocks"][1]["concepts"][1] == {
        "id": lib.concepts["softmax"], "slug": "softmax", "name": "softmax",
        "in_source": "uses", "role": "incidental", "rationale": "Softmax is implied.",
    }
    lines = run.summary_lines()
    assert lines[0].startswith("Tagged 'study-paper' (Study: The Paper): 5 blocks (3 questions)")
    assert "(900 input / 120 output tokens)" in lines[0]


def test_hidden_content_is_not_sent(tag_library, fake_anthropic) -> None:
    """An interactable's html, a chart's kind and its points are not text: never sent."""
    from teacher.block_tagging import PageBlock, blocks_payload

    chart = {
        "kind": "line", "title": "Loss", "x_label": "step", "y_label": "loss",
        "series": [{"name": "train", "points": [{"x": 1, "y": 2.0}]}], "caption": "c",
        "source": None,
    }
    widget = {"title": "Try it", "instructions": "Drag.", "html": "<div>secret</div>"}
    payload = blocks_payload([PageBlock(1, "b01", "chart", chart),
                              PageBlock(2, "b02", "interactable", widget)])
    assert payload[0]["content"] == {
        "title": "Loss", "x_label": "step", "y_label": "loss", "series": [{"name": "train"}],
        "caption": "c", "source": None,
    }
    assert payload[1]["content"] == {"title": "Try it", "instructions": "Drag."}


BAD_REPLIES = [
    ("a block not on the page", lambda a: {9: [(a, "main", "x.")]},
     "the reply tags block 9, which is not on the page (its blocks are numbered 1 to 5)"),
    ("a block missing", lambda a: {1: [], 2: [], 3: [], 4: []},
     "the reply gives no answer for block(s) [5]"),
    ("an unknown concept", lambda a: {1: [(999, "main", "x.")], 2: [], 3: [], 4: [], 5: []},
     "block 1 (b01, prose): concept id 999 is not one of the page's concepts"),
    ("a concept twice", lambda a: {1: [(a, "main", "x."), (a, "incidental", "y.")], 2: [], 3: [],
                                   4: [], 5: []},
     "block 1 (b01, prose): concept "),
    ("too many concepts", lambda a: {1: [(a, "main", "x.")] * (MAX_CONCEPTS_PER_BLOCK + 1), 2: [],
                                     3: [], 4: [], 5: []},
     f"more than {MAX_CONCEPTS_PER_BLOCK}"),
    ("a blank rationale", lambda a: {1: [(a, "main", "  ")], 2: [], 3: [], 4: [], 5: []},
     "the rationale is blank"),
    ("a rationale on two lines", lambda a: {1: [(a, "main", "One.\nTwo.")], 2: [], 3: [], 4: [],
                                            5: []},
     "spans several lines"),
    ("a rationale too long", lambda a: {1: [(a, "main", "x" * (MAX_RATIONALE_CHARS + 1))], 2: [],
                                        3: [], 4: [], 5: []},
     f"more than {MAX_RATIONALE_CHARS}"),
]


@pytest.mark.parametrize(("label", "blocks", "fragment"), BAD_REPLIES, ids=[b[0] for b in BAD_REPLIES])
def test_a_broken_reply_is_an_error_and_writes_nothing(
    tag_library, fake_anthropic, label: str, blocks: Any, fragment: str
) -> None:
    lib = tag_library
    # A rejected reply is asked for once more; the second rejection is the error.
    bad = reply(fake_anthropic, blocks(lib.concepts["attention"]))
    fake = fake_anthropic(bad, bad)
    with pytest.raises(TaggingOutputError) as caught:
        BlockTagger(fake, model=MODEL).tag_page(lib.settings, lib.page)
    message = str(caught.value)
    assert message.startswith(ERROR_PREFIX) and message.count(fragment) == 2, message
    assert "(second attempt; the first reply was rejected too: " in message
    assert message.endswith("(the 2 call(s) used 1800 input / 240 output tokens)")
    first, second = fake.calls
    assert second["messages"][0]["content"][:-1] == first["messages"][0]["content"]
    assert fragment in second["messages"][0]["content"][-1]["text"]
    assert stored(lib.settings) == (None, [])


def test_a_rejected_reply_is_asked_for_once_more(tag_library, fake_anthropic) -> None:
    lib = tag_library
    bad = reply(fake_anthropic, {1: [(999, "main", "x.")], 2: [], 3: [], 4: [], 5: []})
    fake = fake_anthropic(bad, full_reply(fake_anthropic, lib))
    run = BlockTagger(fake, model=MODEL).tag_page(lib.settings, lib.page)

    reason = "concept id 999 is not one of the page's concepts"
    first, second = run.tagging.calls
    assert reason in first["rejected"] and "rejected" not in second
    again = fake.calls[1]["messages"][0]["content"]
    assert again[:-1] == fake.calls[0]["messages"][0]["content"]
    assert reason in again[-1]["text"] and "Reply again for every block" in again[-1]["text"]
    assert (run.tagging.input_tokens, run.tagging.output_tokens) == (1800, 240)
    assert len(stored(lib.settings)[1]) == 7


def test_a_retried_tagging_stage_succeeds_with_a_note(tag_library, fake_anthropic) -> None:
    lib = tag_library
    slug = _generating_page(lib)
    empty = {n: [] for n in range(1, 8)}
    bad = reply(fake_anthropic, {**empty, 1: [(999, "main", "x.")]})
    fake = fake_anthropic(bad, reply(fake_anthropic, empty))
    assert run_generation(lib.settings, slug, StubGenerator(block_tagger=BlockTagger(fake))) == "ready"
    with session_scope(lib.settings) as session:
        artifact = ArtifactManager(session, lib.settings).get_by_slug(slug)
        runs = GenerationRunManager(session, lib.settings).list(artifact)
        (tagging,) = [r for r in runs if r.stage == "block_concepts"]
        assert tagging.status == "succeeded"
        assert "The first tagging reply was rejected (" in tagging.note
        assert "concept id 999" in tagging.note


def test_a_schema_invalid_reply_and_service_failures(tag_library, fake_anthropic) -> None:
    lib = tag_library
    a = lib.concepts["attention"]
    bad_role = reply(fake_anthropic, {1: [(a, "core", "x.")], 2: [], 3: [], 4: [], 5: []})
    for outcome, error, fragment in [
        (bad_role, TaggingOutputError, "reply is not a valid WireTagging"),
        (fake_anthropic.reply(text="{}", stop_reason="refusal"), TaggingUnavailableError,
         "Claude declined the block concepts step"),
        (fake_anthropic.reply(text="{}", stop_reason="max_tokens"), TaggingUnavailableError,
         f"cut off at max_tokens={TAG_MAX_TOKENS}"),
        (fake_anthropic.status_error(anthropic.OverloadedError, 529, "overloaded_error"),
         TaggingUnavailableError, "the block concepts call to Claude failed"),
    ]:
        outcomes = [outcome] * (2 if error is TaggingOutputError else 1)  # one more try
        fake = fake_anthropic(*outcomes)
        with pytest.raises(error, match=fragment):
            BlockTagger(fake, model=MODEL).tag_page(lib.settings, lib.page)
        assert len(fake.calls) == len(outcomes)
    assert stored(lib.settings) == (None, [])


def test_pages_that_cannot_be_tagged_fail_before_any_call(
    tag_library, fake_anthropic, settings: Settings, tmp_path
) -> None:
    lib = tag_library
    fake = fake_anthropic()
    tagger = BlockTagger(fake, model=MODEL)
    with pytest.raises(NotFoundError):
        tagger.tag_page(settings, "nope")
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        source = artifacts.sources(artifacts.get_by_slug(lib.page))[0][0]
        artifacts.create(slug="lab-paper", kind="lab", title="Lab", generator="stub-lab-v1",
                         params={"study_page": lib.page}, sources=[source])
        artifacts.create(slug="draft", kind="study_page", title="Draft", generator="test",
                         sources=[source])
        bare = tmp_path / "bare.txt"
        bare.write_text("Nothing linked.\n", encoding="utf-8")
        from teacher.managers import KnowledgeSourceManager

        other = KnowledgeSourceManager(session, settings).add(bare, slug="bare", kind="note",
                                                             title="Bare")
        unlinked = artifacts.create(slug="unlinked", kind="study_page", title="U",
                                    generator="test", sources=[other])
        ArtifactBlockManager(session, settings).add(unlinked, type="prose",
                                                    content={"markdown": "Hi."})
        artifacts.mark_ready(unlinked)
    for slug, fragment in [
        ("lab-paper", "'lab-paper' is a lab; only study pages have blocks to tag"),
        ("draft", "'draft' is generating, not ready"),
        ("unlinked", "no concepts are linked to 'bare', the source(s) of 'unlinked'; run "
                     "`teacher concepts extract <source-slug>` first"),
    ]:
        with pytest.raises(TaggingInputError, match=fragment.replace("(", r"\(").replace(")", r"\)")):
            tagger.tag_page(settings, slug)
    assert fake.calls == []


def test_a_tagged_page_needs_retag_which_keeps_tags_a_person_made(
    tag_library, fake_anthropic
) -> None:
    lib = tag_library
    BlockTagger(fake_anthropic(full_reply(fake_anthropic, lib)), model=MODEL).tag_page(
        lib.settings, lib.page
    )
    first, _rows = stored(lib.settings)
    again = fake_anthropic()
    with pytest.raises(TaggingInputError, match="was already tagged on .* pass --retag"):
        BlockTagger(again, model=MODEL).tag_page(lib.settings, lib.page)
    assert again.calls == []

    with session_scope(lib.settings) as session:  # two tags a person made
        manager = BlockConceptManager(session, lib.settings)
        b03 = session.get(ArtifactBlock, lib.blocks["b03"])
        b05 = session.get(ArtifactBlock, lib.blocks["b05"])
        manager.add(b03, session.get(Concept, lib.concepts["dot-product"]), "incidental", "user")
        session.execute(  # the model's b05 tag is replaced by the person's own
            BlockConcept.__table__.delete().where(BlockConcept.block_id == b05.id)
        )
        manager.add(b05, session.get(Concept, lib.concepts["softmax"]), "main", "user",
                    rationale="I say so.")
    s, t = lib.concepts["softmax"], lib.concepts["transformer"]
    retag = reply(fake_anthropic, {1: [], 2: [], 3: [(s, "main", "Softmax.")], 4: [(t, "main", "T.")],
                                   5: [(s, "incidental", "Mentioned.")]})
    run = BlockTagger(fake_anthropic(retag), model="claude-sonnet-5-5").tag_page(
        lib.settings, lib.page, retag=True
    )
    tagged_at, rows = stored(lib.settings)
    assert tagged_at is not None and tagged_at >= first
    assert rows == [
        ("b03", "softmax", "main", "model", "claude-sonnet-5-5"),
        ("b03", "dot-product", "incidental", "user", None),
        ("b04", "transformer", "main", "model", "claude-sonnet-5-5"),
        ("b05", "softmax", "main", "user", None),  # the person's tag wins over the model's
    ]
    assert (run.removed, run.added, run.kept_user) == (6, 2, 1)
    assert "Replaced 6 earlier model tag(s) with 2; kept 1 tag(s) a person made." in run.summary_lines()


class _ChangingClient:
    """Delegates to `fake`, running `change()` just before the reply arrives."""

    def __init__(self, fake: Any, change: Any) -> None:
        self.fake = fake
        self.change = change
        self.messages = self

    def stream(self, **kwargs: Any) -> Any:
        self.change()
        return self.fake.stream(**kwargs)


def test_a_concept_edited_during_the_call_writes_nothing(tag_library, fake_anthropic) -> None:
    lib = tag_library

    def rename() -> None:
        with session_scope(lib.settings) as session:
            ConceptManager(session, lib.settings).update(
                session.get(Concept, lib.concepts["softmax"]), summary="Changed meanwhile."
            )

    client = _ChangingClient(fake_anthropic(full_reply(fake_anthropic, lib)), rename)
    with pytest.raises(TaggingChangedError, match="concept 'softmax' was edited while the blocks"):
        BlockTagger(client, model=MODEL).tag_page(lib.settings, lib.page)
    assert stored(lib.settings) == (None, [])


def test_models_a_run_may_use(tag_library, fake_anthropic) -> None:
    tagger = BlockTagger(fake_anthropic(), model="claude-custom")
    ids = [m for m, _label in tagger.model_choices()]
    assert ids[-1] == "claude-custom" and "claude-sonnet-5-5" in ids
    assert tagger.choose_model(None) == "claude-custom"
    with pytest.raises(ValueError, match="unknown model 'claude-nope': expected one of"):
        tagger.choose_model("claude-nope")
    with pytest.raises(ValueError, match="unknown model"):
        tagger.tag_page(tag_library.settings, tag_library.page, model="claude-nope")
    assert BlockTagger().model == DEFAULT_CONCEPT_MODEL and BlockTagger().timeout == DEFAULT_TIMEOUT


def test_no_credentials_is_an_error_before_any_call(forbid_real_messages_api: None) -> None:
    with pytest.raises(TaggingUnavailableError, match="^Block concepts could not be tagged: "):
        BlockTagger().check_available()


def test_the_stub_tags_the_concepts_a_block_names(tag_library) -> None:
    lib = tag_library
    run = stub_tagger().tag_page(lib.settings, lib.page)
    _tagged_at, rows = stored(lib.settings)
    assert rows == [
        ("b01", "attention", "main", "model", "stub-block-concepts"),  # in its heading
        ("b02", "attention", "main", "model", "stub-block-concepts"),  # in the question
        ("b03", "softmax", "main", "model", "stub-block-concepts"),
        ("b04", "attention", "main", "model", "stub-block-concepts"),
        ("b04", "transformer", "main", "model", "stub-block-concepts"),
        ("b05", "softmax", "main", "model", "stub-block-concepts"),  # named twice (option, why)
    ]
    assert run.tagging.input_tokens == 0


# -- the generation stage ------------------------------------------------------------------------


def _generating_page(lib: Any, slug: str = "fresh") -> str:
    with session_scope(lib.settings) as session:
        artifacts = ArtifactManager(session, lib.settings)
        source = artifacts.sources(artifacts.get_by_slug(lib.page))[0][0]
        artifacts.create(slug=slug, kind="study_page", title="Fresh", generator="stub-v1",
                         sources=[source])
    return slug


def test_generation_stores_the_tags_with_the_blocks(tag_library, fake_anthropic) -> None:
    lib = tag_library
    slug = _generating_page(lib)
    a = lib.concepts["attention"]
    fake = fake_anthropic(reply(fake_anthropic, {1: [], 2: [(a, "main", "It asks.")], 3: [],
                                                 4: [(a, "incidental", "Named.")], 5: [], 6: [],
                                                 7: [(a, "main", "It discusses it.")]},
                                input_tokens=33, output_tokens=4))
    generator = StubGenerator(block_tagger=BlockTagger(fake, model=MODEL))
    assert run_generation(lib.settings, slug, generator) == "ready"
    tagged_at, rows = stored(lib.settings, slug)
    assert tagged_at is not None
    assert rows == [("b02", "attention", "main", "model", MODEL),
                    ("b04", "attention", "incidental", "model", MODEL),
                    ("b07", "attention", "main", "model", MODEL)]
    with session_scope(lib.settings) as session:
        artifact = ArtifactManager(session, lib.settings).get_by_slug(slug)
        last = GenerationRunManager(session, lib.settings).list(artifact)[-1]
        assert (last.stage, last.status, last.model) == ("block_concepts", "succeeded", MODEL)
        assert (last.input_tokens, last.output_tokens) == (33, 4)
    (call,) = fake.calls
    text = call["messages"][0]["content"][0]["text"]
    assert "Which source is this study page based on?" in text  # the drafts were sent
    blocks = json.loads(text.split("<blocks>\n", 1)[1].split("\n</blocks>", 1)[0])
    # The prose role is given beside the type (the content's role is not text); a prediction
    # comes with its resolution, and every option with its explanation.
    assert [(b["type"], b.get("role")) for b in blocks] == [
        ("prose", "explanation"), ("multiple_choice", None), ("prose", "explanation"),
        ("blurb", None), ("prediction", None), ("prose", "explanation"), ("prose", "critique"),
    ]
    assert all("role" not in b["content"] for b in blocks)
    assert "resolution" in blocks[4]["content"]["private"]
    assert len(blocks[1]["content"]["private"]["option_explanations"]) == 3
    assert "A prediction asks the learner to guess" in text and '"critique" is' in text

    # A prediction is a gate but not a question: it is left out of the question counts.
    with session_scope(lib.settings) as session:
        artifact = ArtifactManager(session, lib.settings).get_by_slug(slug)
        gates = [b.type for b in ArtifactBlockManager(session, lib.settings).list(artifact)
                 if b.is_gate]
    assert gates == ["multiple_choice", "blurb", "prediction"]


def test_a_prediction_is_not_counted_as_a_question() -> None:
    from teacher.block_tagging import ConceptOption, PageBlock, Tagging

    concept = ConceptOption(1, "attention", "attention", (), "s", "uses", (), "t")
    blocks = (
        PageBlock(1, "b01", "multiple_choice", {}),
        PageBlock(2, "b02", "prediction", {}),
        PageBlock(3, "b03", "blurb", {}),
    )
    tagging = Tagging("t", "m", blocks, (concept,), (), (), 0, 0)
    assert [b.is_question for b in blocks] == [True, False, True]
    counts = tagging.counts()
    assert (counts["questions"], counts["questions_with_main"]) == (2, 0)
    assert "Questions with no main concept 2: b01, b03." in tagging.summary_lines()


def test_a_failed_tagging_stage_fails_the_generation(tag_library, fake_anthropic) -> None:
    lib = tag_library
    slug = _generating_page(lib)
    bad = reply(fake_anthropic, {1: [(999, "main", "x.")], 2: [], 3: [], 4: [], 5: [], 6: [], 7: []})
    fake = fake_anthropic(bad, bad)
    assert run_generation(lib.settings, slug, StubGenerator(block_tagger=BlockTagger(fake))) == "failed"
    with session_scope(lib.settings) as session:
        artifact = ArtifactManager(session, lib.settings).get_by_slug(slug)
        last = GenerationRunManager(session, lib.settings).list(artifact)[-1]
        assert (last.stage, last.status) == ("block_concepts", "failed")
        assert last.error.startswith("TaggingOutputError: Block concepts could not be tagged: ")
        assert ArtifactBlockManager(session, lib.settings).list(artifact) == []
        assert artifact.status == "failed" and artifact.block_concepts_tagged_at is None


# -- the manager and the merge -------------------------------------------------------------------


def test_manager_rules(tag_library) -> None:
    lib = tag_library
    with session_scope(lib.settings) as session:
        manager = BlockConceptManager(session, lib.settings)
        block = session.get(ArtifactBlock, lib.blocks["b01"])
        attention = session.get(Concept, lib.concepts["attention"])
        with pytest.raises(ValueError, match="unknown block concept role 'core'"):
            manager.add(block, attention, "core", "user")
        with pytest.raises(ValueError, match="unknown block concept origin 'paper'"):
            manager.add(block, attention, "main", "paper")
        with pytest.raises(ValueError, match="a model tag needs the model and a rationale"):
            manager.add(block, attention, "main", "model", model="m")
        with pytest.raises(ValueError, match="a user tag names no model"):
            manager.add(block, attention, "main", "user", model="m")
        manager.add(block, attention, "main", "user")
        with pytest.raises(DuplicateError, match="is already tagged with 'attention'"):
            manager.add(block, attention, "incidental", "user")
        page = ArtifactManager(session, lib.settings).get_by_slug(lib.page)
        spec = TagSpec(block, attention, "main", "Why.")
        with pytest.raises(DuplicateError, match="repeats concept 'attention' on block b01"):
            manager.replace_model_tags(page, [spec, spec], "m")
    with pytest.raises(Exception, match="ck_block_concepts_rationale"):  # the database, too
        with session_scope(lib.settings) as session:
            session.add(BlockConcept(block_id=lib.blocks["b02"], concept_id=lib.concepts["softmax"],
                                     role="main", origin="model", rationale=None, model="m"))
            session.flush()


def test_merge_moves_and_combines_the_block_tags(tag_library) -> None:
    lib = tag_library
    a, s = lib.concepts["attention"], lib.concepts["softmax"]
    with session_scope(lib.settings) as session:
        manager = BlockConceptManager(session, lib.settings)
        get = lambda key: session.get(ArtifactBlock, lib.blocks[key])  # noqa: E731
        manager.add(get("b02"), session.get(Concept, a), "incidental", "model", rationale="A.",
                    model="m1")
        manager.add(get("b02"), session.get(Concept, s), "main", "model", rationale="S wins.",
                    model="m2")
        manager.add(get("b03"), session.get(Concept, s), "main", "user")
        manager.add(get("b04"), session.get(Concept, a), "main", "user")
    with session_scope(lib.settings) as session:
        concepts = ConceptManager(session, lib.settings)
        result = concepts.merge(concepts.get(s), concepts.get(a))
        assert (result.moved_block_tags, result.combined_block_tags) == (1, 1)
    with session_scope(lib.settings) as session:
        rows = sorted(
            (session.get(ArtifactBlock, t.block_id).key, t.concept_id, t.role, t.origin, t.model,
             t.rationale)
            for t in session.scalars(select(BlockConcept))
        )
    assert rows == [
        ("b02", a, "main", "model", "m2", "S wins."),  # softmax's stronger role came with it
        ("b03", a, "main", "user", None, None),  # moved
        ("b04", a, "main", "user", None, None),
    ]
