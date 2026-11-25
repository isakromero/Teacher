"""ConceptExtractor with fake Claude and embedding clients (no network): extraction checks,
resolution against the graph (exact, judged, shortlists from search and embeddings), the
judge's checks, the all-or-nothing write, re-runs, and the request shape."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import pytest
from sqlalchemy import text

from teacher.claude_generator import EFFORT, SYSTEM_PROMPT
from teacher.concept_extraction import (
    CONCEPTS_FORMAT,
    CONCEPTS_INSTRUCTIONS,
    ERROR_PREFIX,
    JUDGE_FORMAT,
    JUDGE_INSTRUCTIONS,
    JUDGE_SYSTEM_PROMPT,
    MAX_ALIASES,
    MAX_CANDIDATES,
    MAX_EVIDENCE_CHARS,
    MAX_NAME_CHARS,
    MAX_SUMMARY_CHARS,
    SHORTLIST_LIMIT,
    ConceptExtractionError,
    ConceptExtractor,
    ConceptGraphChangedError,
    ConceptInputError,
    ConceptOutputError,
    ConceptRun,
    ConceptsUnavailableError,
    WireConcepts,
    check_candidates,
    source_info,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.embeddings import concept_text, text_sha256
from teacher.generation import StageHandle
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptManager,
    KnowledgeSourceManager,
    LinkSpec,
    SourceConceptManager,
)

MODEL = "claude-test-concepts"
SOURCE_TEXT = "# Distillation\n\nWe distil a teacher into a student with a KL divergence loss.\n"


def cand(
    name: str,
    aliases: list[str] | None = None,
    *,
    summary: str | None = None,
    role: str = "uses",
    evidence: str | None = "as the paper puts it",
) -> dict[str, Any]:
    return {
        "name": name,
        "aliases": aliases or [],
        "summary": summary if summary is not None else f"The general idea of {name}.",
        "role": role,
        "evidence": evidence,
    }


def same(candidate: int, concept_id: int, reason: str = "They are the same idea.") -> dict:
    return {"candidate": candidate, "verdict": "same_as", "concept_id": concept_id, "reason": reason}


def new(candidate: int, reason: str = "A different idea.") -> dict:
    return {"candidate": candidate, "verdict": "new", "concept_id": None, "reason": reason}


# -- fixtures -------------------------------------------------------------------------------


@pytest.fixture
def make_source(settings: Settings, tmp_path: Path) -> Callable[..., str]:
    def make(slug: str = "paper", content: str = SOURCE_TEXT, title: str = "A Paper") -> str:
        path = tmp_path / "uploads" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        with session_scope(settings) as session:
            KnowledgeSourceManager(session, settings).add(
                path, slug=slug, kind="paper", title=title, media_type="text/markdown"
            )
        return slug

    return make


@pytest.fixture
def embedder(fake_embedder: type) -> Any:
    return fake_embedder()


@pytest.fixture
def seed(settings: Settings, embedder: Any) -> Callable[..., dict[str, int]]:
    """`seed(("Name", [aliases], "slug" or None), ...)`: existing concepts, embedded with the
    test's embedder (whose calls are then cleared); returns {slug: id}."""

    def make(*specs: tuple, reviewed: bool = False) -> dict[str, int]:
        with session_scope(settings) as session:
            concepts = ConceptManager(session, settings)
            ids = {}
            for spec in specs:
                name = spec[0]
                aliases = spec[1] if len(spec) > 1 else []
                slug = spec[2] if len(spec) > 2 else None
                concept = concepts.create(
                    name, f"Existing: {name}.", "user", aliases=aliases, slug=slug
                )
                if reviewed:
                    concepts.mark_reviewed(concept)
                ids[concept.slug] = concept.id
            ConceptEmbeddingManager(session, settings).embed_stale(embedder)
        embedder.calls.clear()
        return ids

    return make


def run(settings: Settings, extractor: ConceptExtractor, slug: str = "paper",
        stage: StageHandle | None = None) -> ConceptRun:
    with session_scope(settings) as session:
        info = source_info(session, settings, slug)
    return extractor.run(settings, info, stage=stage)


def links(settings: Settings, slug: str = "paper") -> list[tuple]:
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
        return [
            (l.concept.slug, l.role, l.resolution, l.candidate_name, l.judge_reason, l.evidence)
            for l in SourceConceptManager(session, settings).for_source(source)
        ]


def snapshot(settings: Settings) -> dict[str, list]:
    """Every concept-graph and link row, raw."""
    queries = {
        "concepts": "SELECT id, slug, name, summary, origin, reviewed_at, updated_at FROM concepts",
        "aliases": "SELECT concept_id, alias FROM concept_aliases",
        "embeddings": "SELECT concept_id, model, text_hash, vector FROM concept_embeddings",
        "links": "SELECT source_id, concept_id, role, resolution, candidate_name FROM source_concepts",
        "fts": "SELECT rowid, name, aliases FROM concept_search",
        "trigram": "SELECT rowid, label FROM concept_trigram",
    }
    with session_scope(settings) as session:
        return {key: sorted(session.execute(text(sql)).all()) for key, sql in queries.items()}


def concept(settings: Settings, slug: str) -> dict[str, Any]:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings).get_by_slug(slug)
        return {
            "name": c.name,
            "summary": c.summary,
            "aliases": [a.alias for a in c.aliases],
            "origin": c.origin,
            "reviewed": c.reviewed_at is not None,
            "hash": c.embedding.text_hash if c.embedding else None,
            "text": concept_text(c.name, [a.alias for a in c.aliases], c.summary),
        }


def judge_payload(call: dict[str, Any]) -> list[dict[str, Any]]:
    prompt = call["messages"][0]["content"][0]["text"]
    return json.loads(prompt.split("<candidates>\n", 1)[1].split("\n</candidates>", 1)[0])


def extractor_for(fake: Any, embedder: Any) -> ConceptExtractor:
    return ConceptExtractor(fake, embedder=embedder, model=MODEL, timeout=33.0)


# -- step 1: the extraction and its checks --------------------------------------------------


def wire(*concepts: dict) -> WireConcepts:
    return WireConcepts.model_validate({"concepts": list(concepts)})


def test_check_candidates_strips_and_keeps_order() -> None:
    result = check_candidates(wire(
        cand("  knowledge distillation ", [" KD "], summary=" Training a student. ",
             role="introduces", evidence=" we distil "),
        cand("prior", role="assumes", evidence=None),
    ))
    assert [(c.number, c.name, c.aliases, c.summary, c.role, c.evidence) for c in result] == [
        (1, "knowledge distillation", ("KD",), "Training a student.", "introduces", "we distil"),
        (2, "prior", (), "The general idea of prior.", "assumes", None),
    ]
    assert result[0].slug == "knowledge-distillation"
    assert result[0].text == "knowledge distillation\nAlso known as: KD\nTraining a student."


REJECTIONS: dict[str, tuple[list[dict], str]] = {
    "no concepts": ([], "Claude listed 0 concepts; between 1 and 40"),
    "too many": ([cand(f"concept {i}") for i in range(MAX_CANDIDATES + 1)],
                 "Claude listed 41 concepts"),
    "blank name": ([cand("  ")], "the name is blank"),
    "multi-line name": ([cand("policy\ngradient")], "spans several lines"),
    "long name": ([cand("x" * (MAX_NAME_CHARS + 1))], f"more than {MAX_NAME_CHARS}"),
    "name without letters": ([cand("—")], "has no letters or digits"),
    "name without a slug": ([cand("∇")], "no slug can be made from the name"),
    "too many aliases": ([cand("a", [f"b{i}" for i in range(MAX_ALIASES + 1)])],
                         f"more than {MAX_ALIASES}"),
    "blank alias": ([cand("attention", ["  "])], "the alias 1 is blank"),
    "alias is the name": ([cand("KL divergence", ["kl  Divergence"])], "repeats its own name"),
    "repeated alias": ([cand("attention", ["ATT", "att"])], "repeats its alias 'ATT'"),
    "alias without letters": ([cand("attention", ["..."])], "has no letters or digits"),
    "same name twice": ([cand("Attention"), cand("attention ")],
                        "'attention' is also a name or alias of concept 1"),
    "name is another's alias": ([cand("Kullback-Leibler divergence", ["KL divergence"]),
                                 cand("KL divergence")],
                                "'KL divergence' is also a name or alias of concept 1"),
    "shared alias": ([cand("policy gradient", ["PG"]), cand("proximal gradient", ["pg"])],
                     "'pg' is also a name or alias of concept 1 ('PG')"),
    "blank summary": ([cand("attention", summary=" ")], "the summary is blank"),
    "long summary": ([cand("attention", summary="s" * (MAX_SUMMARY_CHARS + 1))],
                     f"more than {MAX_SUMMARY_CHARS}"),
    "blank evidence": ([cand("attention", evidence=" ")], "the evidence is blank"),
    "long evidence": ([cand("attention", evidence="e" * (MAX_EVIDENCE_CHARS + 1))],
                      f"more than {MAX_EVIDENCE_CHARS}"),
    "used without evidence": ([cand("attention", evidence=None)], "uses needs evidence"),
    "introduced without evidence": ([cand("attention", role="introduces", evidence=None)],
                                    "introduces needs evidence"),
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_check_candidates_rejects(case: str) -> None:
    concepts, fragment = REJECTIONS[case]
    with pytest.raises(ConceptOutputError) as caught:
        check_candidates(wire(*concepts))
    assert str(caught.value).startswith(ERROR_PREFIX)
    assert fragment in str(caught.value), str(caught.value)


@pytest.mark.parametrize(
    ("make_reply", "error", "fragments"),
    [
        (lambda f: f.reply(text='{"concepts": [', model=MODEL), ConceptOutputError,
         ("concepts reply is not a valid WireConcepts",)),
        (lambda f: f.concepts_reply([{**cand("attention"), "role": "mentions"}]),
         ConceptOutputError, ("not a valid WireConcepts", "concepts.0.role")),
        (lambda f: f.reply(text="", stop_reason="refusal", model=MODEL,
                           stop_details={"type": "refusal", "category": "bio", "explanation": None}),
         ConceptsUnavailableError, ("declined the concepts step", "category bio",
                                    "no concepts were extracted")),
        (lambda f: f.reply(text='{"concepts": [', stop_reason="max_tokens", model=MODEL),
         ConceptsUnavailableError, ("cut off at max_tokens=32000",)),
        (lambda f: f.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
         ConceptsUnavailableError, ("the concepts call to Claude failed", "HTTP 429")),
        (lambda f: f.concepts_reply([cand("x" * 200)]), ConceptOutputError,
         (f"more than {MAX_NAME_CHARS}",)),
    ],
)
def test_extraction_failures_write_nothing(
    settings: Settings, make_source, seed, embedder, fake_anthropic,
    make_reply: Callable[[Any], Any], error: type, fragments: tuple[str, ...],
) -> None:
    make_source()
    seed(("attention",))
    before = snapshot(settings)
    stage = StageHandle()
    # A rejected reply is asked for once more; an unavailable model is not.
    replies = [make_reply(fake_anthropic)] * (2 if error is ConceptOutputError else 1)
    fake = fake_anthropic(*replies)
    with pytest.raises(error) as caught:
        run(settings, extractor_for(fake, embedder), stage=stage)
    assert len(fake.calls) == len(replies)
    message = str(caught.value)
    assert message.startswith(ERROR_PREFIX)
    for fragment in fragments:
        assert fragment in message, (fragment, message)
    assert snapshot(settings) == before
    assert embedder.calls == []  # nothing embedded before the reply was checked


def test_tokens_of_a_rejected_reply_are_recorded(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    stage = StageHandle()
    reply = fake_anthropic.concepts_reply([], input_tokens=70, output_tokens=8)
    fake = fake_anthropic(reply, reply)
    with pytest.raises(ConceptOutputError):
        run(settings, extractor_for(fake, embedder), stage=stage)
    assert (stage.input_tokens, stage.output_tokens) == (140, 16)


def test_a_rejected_reply_is_asked_for_once_more(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    stage = StageHandle()
    bad = fake_anthropic.concepts_reply([cand("Krippendorff's alpha", ["Krippendorff's Alpha"])])
    good = fake_anthropic.concepts_reply([cand("Krippendorff's alpha", ["alpha reliability"])])
    fake = fake_anthropic(bad, good)
    result = run(settings, extractor_for(fake, embedder), stage=stage)

    reason = "the alias \"Krippendorff's Alpha\" repeats its own name"
    first, second = fake.calls
    assert second["messages"][0]["content"][:-1] == first["messages"][0]["content"]
    again = second["messages"][0]["content"][-1]["text"]
    assert reason in again and "Reply again with the complete list" in again
    assert [c.aliases for c in result.candidates] == [("alpha reliability",)]
    assert reason in result.calls[0]["rejected"] and "rejected" not in result.calls[1]
    assert reason in stage.note and "second reply" in stage.note


def test_a_second_rejection_fails_and_names_both(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    before = snapshot(settings)
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("softmax", ["SoftMax"])]),
        fake_anthropic.concepts_reply([cand("softmax", ["soft max", "Soft Max"])]),
    )
    stage = StageHandle()
    with pytest.raises(ConceptOutputError) as caught:
        run(settings, extractor_for(fake, embedder), stage=stage)
    message = str(caught.value)
    assert message.startswith(ERROR_PREFIX) and message.count(ERROR_PREFIX) == 1
    assert "repeats its alias 'soft max' (second attempt" in message
    assert "the alias 'SoftMax' repeats its own name" in message
    assert len(fake.calls) == 2 and stage.note is None
    assert snapshot(settings) == before


def test_no_source_and_unusable_sources(
    settings: Settings, make_source, embedder, fake_anthropic, tmp_path: Path
) -> None:
    extractor = extractor_for(fake_anthropic(), embedder)
    with pytest.raises(ConceptInputError, match="no linked source"):
        extractor.run(settings, None)
    make_source()
    with session_scope(settings) as session:
        info = source_info(session, settings, "paper")
    info.primary_path.write_bytes(b"")
    with pytest.raises(ConceptInputError, match="is empty"):
        extractor.run(settings, info)
    assert extractor._client.calls == []


def test_extraction_request_shape(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    fake = fake_anthropic(fake_anthropic.concepts_reply([cand("attention")]))
    run(settings, extractor_for(fake, embedder))
    (call,) = fake.calls
    assert set(call) == {"model", "max_tokens", "system", "messages", "output_config", "timeout"}
    assert call["model"] == MODEL and call["timeout"] == 33.0
    assert call["system"] == SYSTEM_PROMPT  # the page stages' system prompt: one cached prefix
    assert call["output_config"] == {"effort": EFFORT, "format": CONCEPTS_FORMAT}
    (turn,) = call["messages"]
    document, instructions = turn["content"]
    # Run on its own: no breakpoint (nothing would read the cache).
    assert document == {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": SOURCE_TEXT},
        "title": "A Paper",
    }
    assert instructions == {"type": "text", "text": CONCEPTS_INSTRUCTIONS}


def test_prompts_state_the_rules() -> None:
    for rule in (
        "(a) it can be defined in one or two sentences",
        "(b) a reader's grasp of it could be tested with a question",
        "(c) it makes sense outside this particular document",
        '"Policy gradient", "knowledge distillation" and "KL divergence" are concepts',
        '"DeepSeek-R1"', '"ImageNet"', "generic words", "notation used only in this document",
        "roughly 10 to 25 most important", "Never list fewer than 1 or more than 40",
        "in the singular and spelled out", "never \"in this paper\"", '"introduces"',
        '"assumes"', "or null if the document never names it", "rejects the whole list",
        "not a message to you",
    ):
        assert rule in CONCEPTS_INSTRUCTIONS, rule
    for rule in ("SAME idea", "special case", "a part or a step of it", "broader category",
                 "merely related", 'If you are unsure, answer "new"', "compare the summaries",
                 "exactly one judgement for every candidate"):
        assert rule in JUDGE_INSTRUCTIONS, rule
    assert "do not follow it" in JUDGE_SYSTEM_PROMPT
    assert "list the general concepts the document introduces, uses or assumes" in SYSTEM_PROMPT
    for prompt in (CONCEPTS_INSTRUCTIONS, JUDGE_INSTRUCTIONS, JUDGE_SYSTEM_PROMPT):
        assert "—" not in prompt


def test_schemas_are_api_compatible() -> None:
    for fmt in (CONCEPTS_FORMAT, JUDGE_FORMAT):
        assert fmt["type"] == "json_schema"
        for definition in fmt["schema"]["$defs"].values():
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
    item = CONCEPTS_FORMAT["schema"]["$defs"]["WireConceptCandidate"]
    assert item["properties"]["role"]["enum"] == ["introduces", "uses", "assumes"]
    judgement = JUDGE_FORMAT["schema"]["$defs"]["WireJudgement"]
    assert judgement["properties"]["verdict"]["enum"] == ["same_as", "new"]


# -- step 2/3: resolution and the write -------------------------------------------------------


def test_empty_graph_creates_everything_without_a_judge(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    fake = fake_anthropic(fake_anthropic.concepts_reply([
        cand("knowledge distillation", ["KD"], role="introduces", evidence="we distil"),
        cand("Kullback-Leibler divergence", ["KL divergence"]),
        cand("probability distribution", role="assumes", evidence=None),
    ], input_tokens=100, output_tokens=40))
    stage = StageHandle()
    result = run(settings, extractor_for(fake, embedder), stage=stage)

    assert len(fake.calls) == 1  # no judge call
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 3  # one batch, no stale pass
    assert (stage.input_tokens, stage.output_tokens) == (100, 40)
    assert links(settings) == [
        ("knowledge-distillation", "introduces", "created", "knowledge distillation", None,
         "we distil"),
        ("kullback-leibler-divergence", "uses", "created", "Kullback-Leibler divergence", None,
         "as the paper puts it"),
        ("probability-distribution", "assumes", "created", "probability distribution", None,
         None),
    ]
    kd = concept(settings, "knowledge-distillation")
    assert kd["origin"] == "paper" and kd["aliases"] == ["KD"] and not kd["reviewed"]
    assert kd["hash"] == text_sha256(kd["text"])  # fresh embedding, so the index is not stale
    with session_scope(settings) as session:
        assert ConceptEmbeddingManager(session, settings).stale(embedder.model) == []
    assert [l.resolution for l in result.links] == ["created"] * 3
    assert result.removed_links == 0 and result.stale_embedded == 0
    record = result.record("cli")
    assert record["trigger"] == "cli" and record["model"] == MODEL
    assert record["calls"][0]["usage"]["input_tokens"] == 100
    assert [c["name"] for c in record["candidates"]][0] == "knowledge distillation"
    lines = result.summary_lines()
    assert "Created 3:" in lines and "Matched exactly 0:" in lines
    assert "Links: replaced 0 previous link(s) with 3." in lines


def test_exact_match_links_without_changing_the_concept(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    ids = seed(("KL divergence", ["KL"]), reviewed=True)
    before = concept(settings, "kl-divergence")
    fake = fake_anthropic(fake_anthropic.concepts_reply([
        cand("kl  divergence", ["relative entropy"], role="introduces", evidence="KL loss"),
    ]))
    result = run(settings, extractor_for(fake, embedder))
    assert len(fake.calls) == 1  # exact: no judge
    assert links(settings) == [
        ("kl-divergence", "introduces", "exact", "kl  divergence", None, "KL loss"),
    ]
    assert concept(settings, "kl-divergence") == before  # no alias added, still reviewed
    assert result.resolutions[0].concept.id == ids["kl-divergence"]
    assert result.resolutions[0].matched_text == "kl  divergence"


def test_an_alias_alone_is_not_an_exact_match(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    """"PPO" names both proximal policy optimisation and proximal point optimisation: a hit
    through an alias alone goes to the judge."""
    make_source()
    ids = seed(("proximal point optimisation", ["PPO"]))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("proximal policy optimisation", ["PPO"])]),
        fake_anthropic.judge_reply([new(1, "A policy method, not a point method.")]),
    )
    run(settings, extractor_for(fake, embedder))
    (payload,) = judge_payload(fake.calls[1])
    assert payload["existing"][0]["id"] == ids["proximal-point-optimisation"]
    assert links(settings)[0][:3] == ("proximal-policy-optimisation", "uses", "created")
    assert concept(settings, "proximal-point-optimisation")["aliases"] == ["PPO"]


def test_ambiguous_exact_hits_go_to_the_judge_and_a_judged_match_gains_aliases(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    ids = seed(("attention", [], "attention-psych"), ("soft attention", ["attention mechanism"]),
               reviewed=True)
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("attention", ["attention mechanism", "attn"])]),
        fake_anthropic.judge_reply([same(1, ids["soft-attention"], "Both weight the inputs.")]),
    )
    result = run(settings, extractor_for(fake, embedder))
    judge = fake.calls[1]
    assert judge["system"] == JUDGE_SYSTEM_PROMPT and judge["model"] == MODEL
    assert judge["output_config"] == {"effort": EFFORT, "format": JUDGE_FORMAT}
    (payload,) = judge_payload(judge)
    assert payload["candidate"] == 1 and payload["aliases"] == ["attention mechanism", "attn"]
    assert [e["id"] for e in payload["existing"]][:2] == [
        ids["attention-psych"], ids["soft-attention"]
    ]
    (resolution,) = result.resolutions
    found_by = {e.concept.slug: e.found_by for e in resolution.shortlist}
    assert found_by["attention-psych"][0] == "exact name"
    assert found_by["soft-attention"][0] == "exact alias 'attention mechanism'"

    assert links(settings) == [
        ("soft-attention", "uses", "judged", "attention", "Both weight the inputs.",
         "as the paper puts it"),
    ]
    soft = concept(settings, "soft-attention")
    assert soft["name"] == "soft attention" and soft["summary"] == "Existing: soft attention."
    assert sorted(soft["aliases"]) == ["attention", "attention mechanism", "attn"]
    assert not soft["reviewed"]  # an alias edit clears reviewed_at, as always
    assert soft["hash"] == text_sha256(soft["text"])  # re-embedded with its new aliases
    assert embedder.calls[-1] == [soft["text"]]
    assert concept(settings, "attention-psych")["reviewed"]  # untouched
    assert result.links[0].aliases_added == ("attention", "attn")
    assert any("aliases added: attention, attn" in line for line in result.summary_lines())


def test_shortlist_from_the_search_tiers(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    ids = seed(("policy gradient theorem",), ("entropy",), ("transformer",))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([
            cand("policy gradient"), cand("entorpy"), cand("former"),
        ]),
        fake_anthropic.judge_reply([new(1), new(2), new(3)]),
    )
    result = run(settings, extractor_for(fake, embedder))
    shortlists = {
        r.candidate.name: {e.concept.slug: e.found_by for e in r.shortlist}
        for r in result.resolutions
    }
    assert shortlists["policy gradient"] == {
        "policy-gradient-theorem": ("search word 'policy gradient'",)
    }
    assert shortlists["entorpy"] == {"entropy": ("search fuzzy 'entorpy'",)}
    assert shortlists["former"] == {"transformer": ("search substring 'former'",)}
    assert [p["candidate"] for p in judge_payload(fake.calls[1])] == [1, 2, 3]
    assert {l[0] for l in links(settings)} == {"policy-gradient", "entorpy", "former"}
    assert ids  # the existing concepts are unchanged and unlinked
    with session_scope(settings) as session:
        assert len(ConceptManager(session, settings).list()) == 6


def test_shortlist_from_embeddings_above_the_floor(
    settings: Settings, make_source, fake_embedder, fake_anthropic
) -> None:
    table = {
        "teacher-student training": [1.0, 0.0],
        "knowledge distillation": [0.8, 0.6],  # cosine 0.8 with the one above
        "dropout": [0.4, 0.0, 0.92],  # cosine 0.4 with teacher-student: below the floor
    }
    embedder = fake_embedder(table)
    make_source()
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        existing = concepts.create("teacher-student training", "Train a small model.", "user")
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
        existing_id = existing.id
    embedder.calls.clear()
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("knowledge distillation"), cand("dropout")]),
        fake_anthropic.judge_reply([same(1, existing_id, "Distillation is that training.")]),
    )
    result = run(settings, extractor_for(fake, embedder))
    first, second = result.resolutions
    assert [(e.concept.id, e.found_by) for e in first.shortlist] == [
        (existing_id, ("embedding 0.800",))
    ]
    assert second.verdict == "new" and second.shortlist == () and second.reason is None
    assert [p["candidate"] for p in judge_payload(fake.calls[1])] == [1]  # dropout: no judge
    assert links(settings) == [
        ("dropout", "uses", "created", "dropout", None, "as the paper puts it"),
        ("teacher-student-training", "uses", "judged", "knowledge distillation",
         "Distillation is that training.", "as the paper puts it"),
    ]
    assert concept(settings, "teacher-student-training")["aliases"] == ["knowledge distillation"]


def test_stale_embeddings_are_brought_up_to_date_first(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    with session_scope(settings) as session:
        ConceptManager(session, settings).create("gradient descent", "Step downhill.", "user")
    fake = fake_anthropic(fake_anthropic.concepts_reply([cand("adam")]))
    result = run(settings, extractor_for(fake, embedder))
    assert result.stale_embedded == 1
    assert embedder.calls[0] == ["gradient descent\nStep downhill."]
    assert len(embedder.calls) == 2  # then the candidates
    assert "Embedded 1 stale concept(s) first." in result.summary_lines()


def test_shortlist_is_capped(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    # Search finds the first variants, embeddings the last (all close to the candidate).
    embedder.table["attention"] = [1.0]
    for i in range(12):
        embedder.table[f"attention variant {i}"] = [1.0, 0.0, 0.01 * (12 - i)]
    seed(*[(f"attention variant {i}",) for i in range(12)])
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("attention", ["attention variant"])]),
        fake_anthropic.judge_reply([new(1)]),
    )
    result = run(settings, extractor_for(fake, embedder))
    assert len(result.resolutions[0].shortlist) == SHORTLIST_LIMIT
    assert len(judge_payload(fake.calls[1])[0]["existing"]) == SHORTLIST_LIMIT


def test_two_candidates_resolving_to_one_concept_make_one_link(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    ids = seed(("policy gradient", ["PG"]))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([
            cand("REINFORCE", role="uses", evidence="we use REINFORCE"),
            cand("policy gradient", role="introduces", evidence="our gradient"),
        ]),
        fake_anthropic.judge_reply([same(1, ids["policy-gradient"], "REINFORCE is that.")]),
    )
    # REINFORCE has no lexical hit: put it near policy gradient in embedding space.
    embedder.table["reinforce"] = embedder.vector("policy gradient")
    result = run(settings, extractor_for(fake, embedder))
    assert links(settings) == [
        ("policy-gradient", "introduces", "exact", "REINFORCE; policy gradient", None,
         "our gradient"),
    ]
    assert [r.verdict for r in result.resolutions] == ["judged", "exact"]
    assert concept(settings, "policy-gradient")["aliases"] == ["PG", "REINFORCE"]
    assert result.links[0].candidates == ("REINFORCE", "policy gradient")


def test_slug_collision_is_judged_and_a_new_concept_gets_a_suffix(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    make_source()
    ids = seed(("Q learning (psychology)", [], "q-learning"))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("Q-learning")]),
        fake_anthropic.judge_reply([new(1, "Reinforcement learning, not psychology.")]),
    )
    result = run(settings, extractor_for(fake, embedder))
    (payload,) = judge_payload(fake.calls[1])
    assert payload["existing"][0]["id"] == ids["q-learning"]
    assert "slug" in result.resolutions[0].shortlist[0].found_by
    assert links(settings)[0][:3] == ("q-learning-2", "uses", "created")
    assert result.links[0].slug_note == (
        "slug 'q-learning' belongs to another concept, judged different; created as "
        "'q-learning-2'"
    )
    assert concept(settings, "q-learning-2")["name"] == "Q-learning"


def test_two_new_candidates_with_one_slug_are_an_error(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    fake = fake_anthropic(fake_anthropic.concepts_reply([cand("Q-learning"), cand("Q learning")]))
    with pytest.raises(ConceptOutputError, match="would get the same slug 'q-learning'"):
        run(settings, extractor_for(fake, embedder))
    assert links(settings) == []


JUDGE_ERRORS = {
    "missing answer": (lambda ids: [new(1)], "no answer for candidate(s) [2]"),
    "answered twice": (lambda ids: [new(1), new(1), new(2)], "answered twice for candidate 1"),
    "unknown candidate": (lambda ids: [new(1), new(2), new(3)],
                          "candidate 3, which it was not asked about"),
    "id not on its shortlist": (lambda ids: [same(1, ids["entropy"]), new(2)],
                                "which is not on its shortlist"),
    "same_as without id": (lambda ids: [{**same(1, 0), "concept_id": None}, new(2)],
                           "is 'same_as' concept id None"),
    "new with an id": (lambda ids: [{**new(1), "concept_id": ids["attention"]}, new(2)],
                       "is 'new' but names concept id"),
    "blank reason": (lambda ids: [new(1, "  "), new(2)], "has a blank reason"),
}


@pytest.mark.parametrize("case", sorted(JUDGE_ERRORS))
def test_judge_reply_is_checked(
    settings: Settings, make_source, seed, embedder, fake_anthropic, case: str
) -> None:
    make_source()
    ids = seed(("attention",), ("entropy",))
    make_judgements, fragment = JUDGE_ERRORS[case]
    fake = fake_anthropic(  # an alias hit alone always goes to the judge
        fake_anthropic.concepts_reply([cand("attention layer", ["attention"]),
                                       cand("entropy regularisation", ["entropy"])],
                                      input_tokens=10, output_tokens=5),
        fake_anthropic.judge_reply(make_judgements(ids), input_tokens=3, output_tokens=2),
    )
    before = snapshot(settings)
    stage = StageHandle()
    with pytest.raises(ConceptOutputError) as caught:
        run(settings, extractor_for(fake, embedder), stage=stage)
    assert fragment in str(caught.value), str(caught.value)
    assert snapshot(settings) == before
    assert (stage.input_tokens, stage.output_tokens) == (13, 7)  # both calls are counted


def test_judge_call_failures(settings: Settings, make_source, seed, embedder, fake_anthropic) -> None:
    make_source()
    seed(("attention",))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("attention layer", ["attention"])]),
        fake_anthropic.status_error(anthropic.InternalServerError, 500),
    )
    with pytest.raises(ConceptsUnavailableError, match="the concept judge call to Claude failed"):
        run(settings, extractor_for(fake, embedder))
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("attention layer", ["attention"])]),
        fake_anthropic.reply(text='{"judgements": [', model=MODEL),
    )
    with pytest.raises(ConceptOutputError, match="not a valid WireJudgements"):
        run(settings, extractor_for(fake, embedder))
    assert links(settings) == []


# -- atomicity --------------------------------------------------------------------------------


def judged_setup(settings: Settings, make_source, seed, fake_anthropic) -> tuple[Any, dict]:
    """One judged match (with alias additions) and one new concept, on an existing link."""
    make_source()
    ids = seed(("soft attention",), ("old idea",))
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("paper")
        SourceConceptManager(session, settings).replace(
            source,
            [LinkSpec(ConceptManager(session, settings).get(ids["old-idea"]), "uses", "created",
                      "old idea")],
        )
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([cand("attention", ["attn"]), cand("layer norm")]),
        fake_anthropic.judge_reply([same(1, ids["soft-attention"])]),
    )
    return fake, ids


@pytest.mark.parametrize("failing_call", [1, 2])
def test_an_embedding_failure_writes_nothing(
    settings: Settings, make_source, seed, embedder, fake_anthropic, failing_call: int
) -> None:
    fake, _ids = judged_setup(settings, make_source, seed, fake_anthropic)
    before = snapshot(settings)
    embedder.fail_on = {failing_call}  # 1: the candidates; 2: the changed concept
    with pytest.raises(ConceptsUnavailableError, match=f"fake failure on call {failing_call}"):
        run(settings, extractor_for(fake, embedder))
    assert snapshot(settings) == before


@pytest.mark.parametrize(
    ("target", "method"),
    [
        ("teacher.managers.ConceptManager", "create"),
        ("teacher.managers.ConceptManager", "add_alias"),
        ("teacher.managers.ConceptEmbeddingManager", "store"),
        ("teacher.managers.SourceConceptManager", "replace"),
    ],
)
def test_a_failure_inside_the_write_rolls_everything_back(
    settings: Settings, make_source, seed, embedder, fake_anthropic,
    monkeypatch: pytest.MonkeyPatch, target: str, method: str,
) -> None:
    fake, _ids = judged_setup(settings, make_source, seed, fake_anthropic)
    before = snapshot(settings)
    module_name, class_name = target.rsplit(".", 1)
    cls = getattr(__import__(module_name, fromlist=[class_name]), class_name)
    original = getattr(cls, method)
    calls = []

    def fail_after(self: Any, *args: Any, **kwargs: Any) -> Any:
        original(self, *args, **kwargs)  # the change is made, then the failure
        calls.append(method)
        raise RuntimeError(f"injected failure after {method}")

    monkeypatch.setattr(cls, method, fail_after)
    with pytest.raises(RuntimeError, match=f"injected failure after {method}"):
        run(settings, extractor_for(fake, embedder))
    assert calls == [method]
    assert snapshot(settings) == before


def test_a_concept_deleted_meanwhile_stops_the_write(
    settings: Settings, make_source, seed, embedder, fake_anthropic
) -> None:
    fake, ids = judged_setup(settings, make_source, seed, fake_anthropic)

    def delete_the_match() -> None:
        with session_scope(settings) as session:
            manager = ConceptManager(session, settings)
            manager.delete(manager.get(ids["soft-attention"]))

    embedder.before[2] = delete_the_match  # between the judge and the write
    with pytest.raises(ConceptGraphChangedError, match="'soft-attention' was deleted"):
        run(settings, extractor_for(fake, embedder))
    assert links(settings) == [("old-idea", "uses", "created", "old idea", None, None)]
    with session_scope(settings) as session:
        assert {c.slug for c in ConceptManager(session, settings).list()} == {"old-idea"}


def test_a_concept_named_like_a_new_one_appearing_meanwhile_stops_the_write(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    fake = fake_anthropic(fake_anthropic.concepts_reply([cand("layer norm")]))

    def create_it() -> None:
        with session_scope(settings) as session:
            ConceptManager(session, settings).create("Layer Norm", "Someone else's.", "user")

    embedder.before[1] = create_it
    with pytest.raises(ConceptGraphChangedError, match="a concept named 'layer norm' appeared"):
        run(settings, extractor_for(fake, embedder))
    assert links(settings) == []


# -- re-runs --------------------------------------------------------------------------------


def test_rerun_matches_exactly_and_replaces_the_links(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source()
    concepts = [cand("knowledge distillation", ["KD"], role="introduces", evidence="we"),
                cand("KL divergence")]
    run(settings, extractor_for(fake_anthropic(fake_anthropic.concepts_reply(concepts)), embedder))
    first = snapshot(settings)
    embedder.calls.clear()

    fake = fake_anthropic(fake_anthropic.concepts_reply(concepts))
    result = run(settings, extractor_for(fake, embedder))
    assert len(fake.calls) == 1  # all exact: no judge
    assert len(embedder.calls) == 1  # nothing stale; the candidates only
    assert [l.resolution for l in result.links] == ["exact", "exact"]
    assert result.removed_links == 2
    second = snapshot(settings)
    assert second["concepts"] == first["concepts"] and second["embeddings"] == first["embeddings"]
    assert [row[3] for row in second["links"]] == ["exact", "exact"]

    # A third run that no longer yields KL divergence drops the link but keeps the concept.
    fake = fake_anthropic(fake_anthropic.concepts_reply(concepts[:1]))
    result = run(settings, extractor_for(fake, embedder))
    assert result.removed_links == 2
    assert [l[0] for l in links(settings)] == ["knowledge-distillation"]
    with session_scope(settings) as session:
        assert ConceptManager(session, settings).get_by_slug("kl-divergence") is not None


def test_links_of_other_sources_are_not_touched(
    settings: Settings, make_source, embedder, fake_anthropic
) -> None:
    make_source("first", "first text\n")
    make_source("second", "second text\n")
    reply = [cand("attention")]
    run(settings, extractor_for(fake_anthropic(fake_anthropic.concepts_reply(reply)), embedder),
        "first")
    run(settings, extractor_for(fake_anthropic(fake_anthropic.concepts_reply(reply)), embedder),
        "second")
    assert [l[:3] for l in links(settings, "first")] == [("attention", "uses", "created")]
    assert [l[:3] for l in links(settings, "second")] == [("attention", "uses", "exact")]


# -- availability ---------------------------------------------------------------------------


def test_check_available_names_every_problem(
    fake_embedder: type, fake_anthropic, forbid_real_messages_api: None
) -> None:
    embedder = fake_embedder()
    ConceptExtractor(fake_anthropic(), embedder=embedder).check_available()  # fine
    embedder.available = False
    with pytest.raises(ConceptsUnavailableError) as caught:
        ConceptExtractor(embedder=embedder).check_available()  # no Anthropic credentials either
    message = str(caught.value)
    assert message.startswith(ERROR_PREFIX)
    assert "Anthropic credentials could not be loaded" in message
    assert "no OpenAI API key (fake)" in message


def test_constructor_checks(fake_embedder: type) -> None:
    class NoCheck:
        model = "m"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return []

    with pytest.raises(TypeError, match="check_available"):
        ConceptExtractor(embedder=NoCheck())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="model"):
        ConceptExtractor(embedder=fake_embedder(), model=" ")


def test_manager_errors_become_extraction_errors(
    settings: Settings, make_source, seed, fake_embedder: type, fake_anthropic
) -> None:
    """A stored vector of another length: the index cannot be searched; loud, nothing written."""
    make_source()
    seed(("attention",))
    other = fake_embedder({"entropy": [1.0]})
    other.DIMENSIONS = 8  # same model name, shorter vectors
    fake = fake_anthropic(fake_anthropic.concepts_reply([cand("entropy")]))
    with pytest.raises(ConceptExtractionError, match="8-dimensional vector"):
        run(settings, extractor_for(fake, other))
    assert links(settings) == []
