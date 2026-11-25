"""ConceptRelater with fake Claude and embedding clients (no network): the proposal checks,
mapping proposals onto existing concepts (exact, search, embedding), unmatched suggestions,
embedding candidates, every filter before judging, the judge's checks, the writes in both
builds_on directions and for the symmetric types, refusals, atomicity, the memo of judged pairs
(and what merge and delete do to it), the request shapes, the preflight and the offline stub."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import anthropic
import pytest
from sqlalchemy import text

from teacher import concept_relating as cr
from teacher.concept_extraction import MAX_ALIASES, MAX_NAME_CHARS, Existing
from teacher.claude_generator import EFFORT
from teacher.concept_relating import (
    ERROR_PREFIX,
    JUDGE_SYSTEM_PROMPT,
    MAX_DESCRIPTION_CHARS,
    MAX_PER_SLOT,
    MAX_RATIONALE_CHARS,
    PROPOSALS_FORMAT,
    PROPOSE_SYSTEM_PROMPT,
    VERDICTS_FORMAT,
    Candidate,
    RelationsGraphChangedError,
    RelationsIndexError,
    RelationsOutputError,
    RelationsUnavailableError,
    stub_relater,
    WireProposals,
    builds_on_reach,
    check_proposals,
    filter_candidates,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.embeddings import text_sha256
from teacher.managers import (
    ConceptEmbeddingManager,
    ConceptJudgementManager,
    ConceptManager,
    ConceptRelationManager,
    EmbeddingIndexError,
    NotFoundError,
)
from teacher.concept_relating import SLOTS, ConceptRelater
from tests.conftest import raw_json

# -- helpers ----------------------------------------------------------------------------------

MODEL = "claude-test-relations"
WHY = "They are connected in a direct way."


def prop(name: str, aliases: Sequence[str] = (), description: str | None = None) -> dict:
    return {
        "name": name,
        "aliases": list(aliases),
        "description": description if description is not None else f"The idea of {name}.",
    }


def proposals(fake: Any, **slots: list[dict]) -> Any:
    """A `WireProposals` reply; slots not given are empty."""
    unknown = set(slots) - set(SLOTS)
    assert not unknown, unknown
    body = {slot: slots.get(slot, []) for slot in SLOTS}
    return fake.reply(text=json.dumps(body), model=MODEL, input_tokens=100, output_tokens=10)


def verdict(candidate: int, value: str, rationale: str = WHY) -> dict:
    return {"candidate": candidate, "verdict": value, "rationale": rationale}


def verdicts(fake: Any, *items: dict) -> Any:
    return fake.reply(
        text=json.dumps({"verdicts": list(items)}), model=MODEL, input_tokens=200, output_tokens=20
    )


def seed(settings: Settings, embedder: Any, *specs: Any) -> dict[str, int]:
    """Concepts from `specs` (a name, or (name, aliases[, summary])), embedded with `embedder`
    (whose calls are then cleared); returns {slug: id}."""
    ids: dict[str, int] = {}
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        for spec in specs:
            name, aliases, summary = (spec, [], None) if isinstance(spec, str) else (
                spec[0], spec[1], spec[2] if len(spec) > 2 else None
            )
            concept = concepts.create(
                name, summary or f"About {name}.", "user", aliases=aliases
            )
            ids[concept.slug] = concept.id
        ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    embedder.calls.clear()
    return ids


def relater(fake: Any, embedder: Any) -> ConceptRelater:
    return ConceptRelater(fake, embedder=embedder, model=MODEL, timeout=33.0)


def prompt(call: dict[str, Any]) -> str:
    return call["messages"][0]["content"][0]["text"]


def tag(call: dict[str, Any], name: str) -> Any:
    body = prompt(call).split(f"<{name}>\n", 1)[1].split(f"\n</{name}>", 1)[0]
    return json.loads(body)


def relations(settings: Settings) -> list[tuple]:
    """(type, from slug, to slug, origin, reviewed_at, rationale), by id."""
    sql = (
        "SELECT r.type, f.slug, t.slug, r.origin, r.reviewed_at, r.rationale "
        "FROM concept_relations r JOIN concepts f ON f.id = r.from_id "
        "JOIN concepts t ON t.id = r.to_id ORDER BY r.id"
    )
    with session_scope(settings) as session:
        return [tuple(row) for row in session.execute(text(sql))]


def memo(settings: Settings) -> list[dict[str, Any]]:
    """Every concept_pair_judgements row as a dict, with slugs for the ids, by id."""
    with session_scope(settings) as session:
        slugs = dict(session.execute(text("SELECT id, slug FROM concepts")).all())
        result = session.execute(text("SELECT * FROM concept_pair_judgements ORDER BY id"))
        out = []
        for row in result.mappings():
            item = dict(row)
            item["pair"] = (slugs[item["low_id"]], slugs[item["high_id"]])
            item["focus"] = slugs[item["focus_id"]]
            item["found_by"] = raw_json(item["found_by"])
            out.append(item)
        return out


def snapshot(settings: Settings) -> dict[str, list]:
    queries = {
        "relations": "SELECT * FROM concept_relations",
        "memo": "SELECT * FROM concept_pair_judgements",
        "concepts": "SELECT id, slug, name, summary FROM concepts",
    }
    with session_scope(settings) as session:
        return {key: sorted(session.execute(text(sql)).all()) for key, sql in queries.items()}


# FakeEmbedder vectors (keyed by the first line of a text, i.e. a name): cosines to
# "attention" are softmax 0.6, dot product 0.5, recurrent network 0.31 (just above the 0.3
# floor), far idea 0.29 (just below it). Names not listed are orthogonal to everything.
TABLE = {
    "attention": [1.0],
    "softmax": [0.6, 0.8],
    "dot product": [0.5, 0.0, 0.8660254],
    "scalar product": [0.5, 0.0, 0.8660254],  # a proposal: the same vector as dot product
    "recurrent network": [0.31, 0.0, 0.0, 0.9507],
    "far idea": [0.29, 0.0, 0.0, 0.0, 0.957],
}


@pytest.fixture
def embedder(fake_embedder: type) -> Any:
    return fake_embedder(TABLE)


def focus(name: str = "attention", aliases: tuple[str, ...] = ("attn",)) -> Existing:
    return Existing(1, "attention", name, aliases, "Weights.", "text")


def wire(**slots: list[dict]) -> WireProposals:
    return WireProposals.model_validate({slot: slots.get(slot, []) for slot in cr.SLOTS})


# -- proposal checks ----------------------------------------------------------------------------


def test_check_proposals_strips_and_keeps_slot_order() -> None:
    result = check_proposals(
        wire(
            relates_to=[prop(" entropy ")],
            builds_on=[prop("  softmax ", [" soft-max "], " Normalised exponentials. "),
                       prop("dot product")],
        ),
        focus(),
    )
    assert [(p.slot, p.number, p.name, p.aliases, p.description) for p in result] == [
        ("builds_on", 1, "softmax", ("soft-max",), "Normalised exponentials."),
        ("builds_on", 2, "dot product", (), "The idea of dot product."),
        ("relates_to", 1, "entropy", (), "The idea of  entropy .".strip()),
    ]
    assert result[0].text == "softmax\nAlso known as: soft-max\nNormalised exponentials."
    assert check_proposals(wire(), focus()) == []  # every slot may be empty


REJECTIONS: dict[str, tuple[dict, str]] = {
    "too many in a slot": (
        {"builds_on": [prop(f"idea {i}") for i in range(MAX_PER_SLOT + 1)]},
        f"Claude proposed {MAX_PER_SLOT + 1} concepts for builds_on; at most {MAX_PER_SLOT}",
    ),
    "blank name": ({"builds_on": [prop("  ")]}, "the name is blank"),
    "multi-line name": ({"relates_to": [prop("policy\ngradient")]}, "spans several lines"),
    "long name": ({"builds_on": [prop("x" * (MAX_NAME_CHARS + 1))]}, f"more than {MAX_NAME_CHARS}"),
    "name without letters": ({"builds_on": [prop("—")]}, "has no letters or digits"),
    "the focus itself": ({"built_on_by": [prop("Attention ")]}, "is the focus concept"),
    "the focus by its alias": ({"relates_to": [prop("ATTN")]}, "is the focus concept"),
    "too many aliases": (
        {"builds_on": [prop("a", [f"b{i}" for i in range(MAX_ALIASES + 1)])]},
        f"more than {MAX_ALIASES}",
    ),
    "blank alias": ({"builds_on": [prop("softmax", ["  "])]}, "the alias 1 is blank"),
    "alias without letters": ({"builds_on": [prop("softmax", ["..."])]}, "no letters or digits"),
    "alias is the name": ({"builds_on": [prop("softmax", ["SoftMax"])]}, "repeats its own name"),
    "repeated alias": ({"builds_on": [prop("softmax", ["SM", "sm"])]}, "repeats its alias 'SM'"),
    "same name in two slots": (
        {"builds_on": [prop("softmax")], "relates_to": [prop("Softmax")]},
        "'Softmax' is also a name or alias of proposal builds_on 1",
    ),
    "shared alias": (
        {"builds_on": [prop("policy gradient", ["PG"])],
         "different_version_of": [prop("proximal gradient", ["pg"])]},
        "'pg' is also a name or alias of proposal builds_on 1 ('PG')",
    ),
    "blank description": ({"builds_on": [prop("softmax", description=" ")]},
                          "the description is blank"),
    "multi-line description": ({"builds_on": [prop("softmax", description="a\nb")]},
                               "spans several lines"),
    "long description": (
        {"builds_on": [prop("softmax", description="d" * (MAX_DESCRIPTION_CHARS + 1))]},
        f"more than {MAX_DESCRIPTION_CHARS}",
    ),
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_check_proposals_rejects(case: str) -> None:
    slots, fragment = REJECTIONS[case]
    with pytest.raises(RelationsOutputError) as caught:
        check_proposals(wire(**slots), focus())
    assert str(caught.value).startswith(ERROR_PREFIX)
    assert fragment in str(caught.value), str(caught.value)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ('{"builds_on": []}', "not a valid WireProposals"),  # slots missing
        ('{"builds_on": [{"name": "x", "aliases": []}], "built_on_by": [], '
         '"different_version_of": [], "relates_to": []}', "builds_on.0.description"),
        ("not json", "not a valid WireProposals"),
    ],
)
def test_a_schema_invalid_proposal_reply_is_an_output_error(
    settings: Settings, embedder, fake_anthropic, text: str, fragment: str
) -> None:
    seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(fake_anthropic.reply(text=text))
    with pytest.raises(RelationsOutputError, match=fragment):
        relater(fake, embedder).relate(settings, "attention")
    assert len(fake.calls) == 1 and memo(settings) == []


# -- mapping proposals onto existing concepts -------------------------------------------------


def test_proposals_find_existing_concepts_by_each_route_and_unmatched_ones_are_reported(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(
        settings, embedder, ("attention", ["attn"]), ("softmax", ["normalised exponential"]),
        "dot product", "layer normalization", "entropy",
    )
    fake = fake_anthropic(
        proposals(
            fake_anthropic,
            builds_on=[
                prop("softmax function", ["softmax"]),  # exact alias -> softmax
                prop("scalar product"),  # embedding only -> dot product (cosine 1)
            ],
            built_on_by=[prop("quantum chromodynamics", description="Strong force theory.")],
            relates_to=[prop("normalization")],  # search (word) -> layer normalization
            different_version_of=[prop("entropy")],  # exact name
        ),
        verdicts(
            fake_anthropic,
            verdict(ids["softmax"], "focus_builds_on_candidate"),
            verdict(ids["dot-product"], "focus_builds_on_candidate"),
            verdict(ids["entropy"], "none", "Entropy measures uncertainty instead."),
            verdict(ids["layer-normalization"], "none", "They do different jobs."),
        ),
    )
    run = relater(fake, embedder).relate(settings, "attention")

    by_name = {m.proposal.name: m for m in run.proposals}
    assert [(m.concept.slug, m.exact, m.found_by) for m in by_name["softmax function"].matches] \
        == [("softmax", True, ("exact alias 'softmax'", "search exact 'softmax'"))]
    [scalar] = by_name["scalar product"].matches
    assert (scalar.concept.slug, scalar.exact, scalar.score) == ("dot-product", False,
                                                                  pytest.approx(1.0))
    assert scalar.found_by == ("embedding 1.000",)
    [norm] = by_name["normalization"].matches
    assert norm.concept.slug == "layer-normalization" and norm.found_by[0].startswith("search ")
    assert by_name["entropy"].matches[0].found_by[0] == "exact name"
    assert by_name["quantum chromodynamics"].matches == ()
    assert [(p.slot, p.name, p.description) for p in run.unmatched] == [
        ("built_on_by", "quantum chromodynamics", "Strong force theory.")
    ]
    # One embedding request for all five proposals (the index was already up to date).
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 5
    assert embedder.calls[0][1] == "scalar product\nThe idea of scalar product."
    # Candidates remember how they were found; the judge's list says so too.
    judged = {c["id"]: c for c in tag(fake.calls[1], "candidates")}
    assert judged[ids["softmax"]]["suggested"] == ["builds_on"]
    assert judged[ids["softmax"]]["similarity"] == 0.6  # also F's embedding neighbour
    assert judged[ids["dot-product"]]["found_by"][0].startswith("text similar to the focus")
    assert judged[ids["entropy"]]["suggested"] == ["different_version_of"]
    assert judged[ids["entropy"]]["similarity"] is None
    assert run.counts()["both"] == 2  # softmax and dot product


def test_a_proposal_keeps_at_most_the_best_few_matches_and_never_the_focus(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cr, "MATCHES_PER_PROPOSAL", 2)
    seed(settings, embedder, ("attention", ["gate"]), ("alpha gate", []), ("beta gate", []),
         ("gamma gate", []))
    fake = fake_anthropic(
        proposals(fake_anthropic, relates_to=[prop("gate mechanism", ["gate"])]),
        verdicts(fake_anthropic, verdict(2, "none", "No."), verdict(3, "none", "No.")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    [mapped] = run.proposals
    assert [m.concept.slug for m in mapped.matches] == ["alpha-gate", "beta-gate"]
    assert mapped.more == 1  # gamma gate found but not kept; attention (exact alias) excluded
    assert all(m.concept.slug != "attention" for m in mapped.matches)


def test_no_proposals_means_no_embedding_request(settings, embedder, fake_anthropic) -> None:
    seed(settings, embedder, "attention", "entropy")
    fake = fake_anthropic(proposals(fake_anthropic))
    run = relater(fake, embedder).relate(settings, "attention")
    assert embedder.calls == [] and run.candidates == () and not run.judge_called
    assert len(fake.calls) == 1  # no candidate, so no judge call


# -- candidates from embeddings -----------------------------------------------------------------


def test_embedding_candidates_respect_the_floor_and_the_limit(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "dot product", "recurrent network",
               "far idea", "entropy")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, *(verdict(ids[s], "none", "No.") for s in
                                   ("softmax", "dot-product", "recurrent-network"))),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert [(c.slug, round(s, 2)) for c, s in run.neighbours] == [
        ("softmax", 0.6), ("dot-product", 0.5), ("recurrent-network", 0.31)
    ]  # far idea (0.29) and entropy (0) are below the floor

    monkeypatch.setattr(cr, "NEIGHBOUR_LIMIT", 2)
    with session_scope(settings) as session:  # change texts so the memo does not hide them
        concepts = ConceptManager(session, settings)
        for slug in ("softmax", "dot-product"):
            concepts.update(concepts.get_by_slug(slug), summary="Changed.")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "none", "No."),
                 verdict(ids["dot-product"], "none", "No.")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert [c.slug for c, _ in run.neighbours] == ["softmax", "dot-product"]
    assert run.stale_embedded == 2  # the edited concepts were re-embedded first


def test_stale_embeddings_are_brought_up_to_date_first(
    settings: Settings, embedder, fake_anthropic
) -> None:
    with session_scope(settings) as session:
        ConceptManager(session, settings).create("attention", "Weights.", "user")
        ConceptManager(session, settings).create("softmax", "Exp.", "user")
    fake = fake_anthropic(proposals(fake_anthropic),
                          verdicts(fake_anthropic, verdict(2, "none", "No.")))
    run = relater(fake, embedder).relate(settings, "attention")
    assert run.stale_embedded == 2 and len(embedder.calls) == 1
    assert "Embedded 2 stale concept(s) first." in run.summary_lines()


# -- the filter ---------------------------------------------------------------------------------


def candidate(id: int, slug: str, *, similarity: float | None = None, exact: bool = False,
              score: float | None = None) -> Candidate:
    concept = Existing(id, slug, slug, (), "S.", slug)
    c = Candidate(concept, similarity=similarity, via_embedding=similarity is not None)
    if exact or score is not None:
        proposal = cr.Proposal("relates_to", 1, slug, (), "D.")
        c.proposals.append((proposal, cr.ProposalMatch(concept, ("x",), exact, score)))
    return c


def test_filter_candidates_applies_every_rule_in_order() -> None:
    f = Existing(1, "f", "f", (), "S.", "f")
    cands = [
        candidate(1, "f", similarity=0.9),
        candidate(2, "related", similarity=0.9),
        candidate(3, "judged", similarity=0.9),
        candidate(4, "implied", similarity=0.9),
        candidate(5, "low", similarity=0.31),
        candidate(6, "exact", exact=True),
        candidate(7, "high", similarity=0.8),
        candidate(8, "matched", score=0.7),
        candidate(9, "also-high", similarity=0.8),
        candidate(10, "path", similarity=0.9),
    ]
    kept, dropped = filter_candidates(
        f, cands,
        related={2: "relation #1 joins them", 3: "ignored: related wins"},
        judged={3: "ignored too", 4: "judged none"},
        implied={4: "ignored: judged wins", 10: "builds_on path f -> x -> path"},
        limit=4,
    )
    # exact proposal matches first, then by score, then by slug; the rest is over the cap
    assert [c.concept.slug for c in kept] == ["exact", "also-high", "high", "matched"]
    assert [(d.candidate.concept.slug, d.reason, d.detail) for d in dropped] == [
        ("f", "self", "it is the focus concept"),
        ("related", "related", "relation #1 joins them"),
        ("judged", "related", "ignored: related wins"),  # the rules apply in order
        ("implied", "judged", "judged none"),
        ("path", "implied", "builds_on path f -> x -> path"),
        ("low", "cap", "over the cap of 4 candidates per concept (score 0.310)"),
    ]


def test_builds_on_reach_gives_shortest_paths() -> None:
    edges = {1: {2, 3}, 2: {4}, 3: {4}, 4: {5}}
    assert builds_on_reach(edges, 1) == {2: [1, 2], 3: [1, 3], 4: [1, 2, 4], 5: [1, 2, 4, 5]}
    assert builds_on_reach(edges, 5) == {}


def test_existing_relations_and_builds_on_paths_are_not_judged(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "dot product", "recurrent network",
               "far idea")
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        r = ConceptRelationManager(session, settings)
        get = c.get_by_slug
        r.add("relates_to", get("attention"), get("softmax"), "They go together.", "user")
        # attention -> far idea -> dot product: dot product is reached through a path
        r.add("builds_on", get("attention"), get("far-idea"), "It needs it.", "user")
        r.add("builds_on", get("far-idea"), get("dot-product"), "It needs it.", "user")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["recurrent-network"], "relates_to")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    dropped = {d.candidate.concept.slug: (d.reason, d.detail) for d in run.dropped}
    assert dropped["softmax"][0] == "related"
    assert "attention relates_to softmax" in dropped["softmax"][1]
    assert dropped["dot-product"] == ("implied",
                                      "builds_on path attention -> far-idea -> dot-product")
    assert [c["id"] for c in tag(fake.calls[1], "candidates")] == [ids["recurrent-network"]]


def test_a_path_towards_the_focus_also_counts(settings, embedder, fake_anthropic) -> None:
    seed(settings, embedder, "attention", "softmax", "entropy")
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        r = ConceptRelationManager(session, settings)
        r.add("builds_on", c.get_by_slug("softmax"), c.get_by_slug("entropy"), "Needs.", "user")
        r.add("builds_on", c.get_by_slug("entropy"), c.get_by_slug("attention"), "Needs.", "user")
    fake = fake_anthropic(proposals(fake_anthropic))  # softmax is dropped: no judge call
    run = relater(fake, embedder).relate(settings, "attention")
    assert [(d.reason, d.detail) for d in run.dropped] == [
        ("implied", "builds_on path softmax -> entropy -> attention")
    ]
    assert not run.judge_called


def test_judged_pairs_are_skipped_until_a_text_changes(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "none", "Not close enough.")),
    )
    relater(fake, embedder).relate(settings, "attention")
    [row] = memo(settings)

    fake = fake_anthropic(proposals(fake_anthropic))  # a judge call would fail the fake
    run = relater(fake, embedder).relate(settings, "attention")
    [(reason, detail)] = [(d.reason, d.detail) for d in run.dropped]
    assert reason == "judged" and detail.startswith("judged none (none) on ")
    assert detail.endswith("with unchanged texts: Not close enough.")
    assert "Judged 0: nothing left to judge, so no judge call was made." in run.summary_lines()

    # The other way round is the same pair.
    fake = fake_anthropic(proposals(fake_anthropic))
    run = relater(fake, embedder).relate(settings, "softmax")
    assert [d.reason for d in run.dropped] == ["judged"]

    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        concepts.add_alias(concepts.get_by_slug("softmax"), "soft-argmax")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert run.dropped == () and len(run.added) == 1
    [new_row] = memo(settings)  # replaced, not added
    assert (new_row["verdict"], row["verdict"]) == ("relates_to", "none")
    assert new_row["high_text_sha256"] != row["high_text_sha256"]


def test_the_cap_cuts_the_lowest_scores(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cr, "MAX_CANDIDATES", 2)
    ids = seed(settings, embedder, "attention", "softmax", "dot product", "recurrent network")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "none", "No."),
                 verdict(ids["dot-product"], "none", "No.")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert [c["id"] for c in tag(fake.calls[1], "candidates")] == [ids["softmax"],
                                                                    ids["dot-product"]]
    assert [(d.candidate.concept.slug, d.reason) for d in run.dropped] == [
        ("recurrent-network", "cap")
    ]
    assert any("1 over the cap of 2" in line for line in run.summary_lines())


# -- the judge's checks -------------------------------------------------------------------------


def judge_case(fake: Any, ids: dict[str, int], *items: dict) -> Any:
    return fake(proposals(fake), verdicts(fake, *items))


VERDICT_REJECTIONS: dict[str, tuple[Callable[[dict[str, int]], list[dict]], str]] = {
    "unknown id": (lambda ids: [verdict(ids["softmax"], "none", "No."), verdict(999, "none", "No.")],
                   "answered for candidate 999, which it was not asked about"),
    "twice": (lambda ids: [verdict(ids["softmax"], "none", "No."),
                           verdict(ids["softmax"], "relates_to")],
              f"answered twice for candidate"),
    "missing": (lambda ids: [], "gave no verdict for candidate(s)"),
    "two sentences": (lambda ids: [verdict(ids["softmax"], "relates_to", "Both. They relate.")],
                      "rationale must be one sentence"),
    "abbreviation": (lambda ids: [verdict(ids["softmax"], "relates_to",
                                          "Both use weights, e.g. Softmax weights.")],
                     "rationale must be one sentence"),
    "multi-line": (lambda ids: [verdict(ids["softmax"], "relates_to", "Both\nrelate.")],
                   "one sentence on one line"),
    "blank": (lambda ids: [verdict(ids["softmax"], "none", "  ")], "non-empty string"),
    "too long": (lambda ids: [verdict(ids["softmax"], "none", "x" * (MAX_RATIONALE_CHARS + 1))],
                 f"more than {MAX_RATIONALE_CHARS}"),
    "unknown verdict": (lambda ids: [verdict(ids["softmax"], "same_as")],
                        "not a valid WireVerdicts"),
}


@pytest.mark.parametrize("case", sorted(VERDICT_REJECTIONS))
def test_judge_reply_is_checked_and_nothing_is_written(
    settings: Settings, embedder, fake_anthropic, case: str
) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    make, fragment = VERDICT_REJECTIONS[case]
    before = snapshot(settings)
    fake = judge_case(fake_anthropic, ids, *make(ids))
    with pytest.raises(RelationsOutputError) as caught:
        relater(fake, embedder).relate(settings, "attention")
    assert str(caught.value).startswith(ERROR_PREFIX)
    assert fragment in str(caught.value), str(caught.value)
    assert snapshot(settings) == before


# -- the writes ---------------------------------------------------------------------------------


def test_verdicts_are_written_in_the_right_direction_and_recorded(
    settings: Settings, embedder, fake_anthropic
) -> None:
    # softmax is created first, so it has the smaller id: a symmetric relation with it is
    # stored softmax -> attention whichever concept the run was for.
    ids = seed(settings, embedder, "softmax", "attention", "dot product", "recurrent network",
               "far idea")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(
            fake_anthropic,
            verdict(ids["softmax"], "relates_to", "Softmax and attention go together."),
            verdict(ids["dot-product"], "focus_builds_on_candidate", "Attention scores are dot products."),
            verdict(ids["recurrent-network"], "candidate_builds_on_focus", "It uses attention."),
        ),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert relations(settings) == [
        ("relates_to", "softmax", "attention", "model", None, "Softmax and attention go together."),
        ("builds_on", "attention", "dot-product", "model", None,
         "Attention scores are dot products."),
        ("builds_on", "recurrent-network", "attention", "model", None, "It uses attention."),
    ]
    assert [(a.other.slug, a.type, a.direction) for a in run.added] == [
        ("softmax", "relates_to", "symmetric"),
        ("dot-product", "builds_on", "out"),
        ("recurrent-network", "builds_on", "in"),
    ]
    lines = run.summary_lines()
    assert "  + attention relates to softmax: Softmax and attention go together." in lines
    assert "  + attention builds on dot-product: Attention scores are dot products." in lines
    assert "  + recurrent-network builds on attention: It uses attention." in lines
    rows = memo(settings)
    assert [(r["pair"], r["focus"], r["verdict"], r["outcome"], r["model"]) for r in rows] == [
        (("softmax", "attention"), "attention", "relates_to", "added", MODEL),
        (("attention", "dot-product"), "attention", "focus_builds_on_candidate", "added", MODEL),
        (("attention", "recurrent-network"), "attention", "candidate_builds_on_focus", "added",
         MODEL),
    ]
    assert [r["relation_id"] for r in rows] == [a.relation_id for a in run.added]
    assert rows[0]["found_by"] == ["embedding"] and rows[0]["similarity"] == pytest.approx(0.6)
    texts = {}
    with session_scope(settings) as session:
        for slug in ("softmax", "attention"):
            texts[slug] = Existing.of(ConceptManager(session, settings).get_by_slug(slug)).text
    assert rows[0]["low_text_sha256"] == text_sha256(texts["softmax"])
    assert rows[0]["high_text_sha256"] == text_sha256(texts["attention"])


def test_a_cycle_is_refused_and_recorded_while_the_other_verdicts_apply(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "dot product", "recurrent network")
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        ConceptRelationManager(session, settings).add(
            "builds_on", c.get_by_slug("softmax"), c.get_by_slug("dot-product"), "Needs.", "user"
        )
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(
            fake_anthropic,
            verdict(ids["softmax"], "focus_builds_on_candidate", "Attention needs softmax."),
            verdict(ids["dot-product"], "candidate_builds_on_focus", "Odd but claimed."),
            verdict(ids["recurrent-network"], "none", "Different jobs."),
        ),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    assert [a.other.slug for a in run.added] == ["softmax"]
    [refused] = run.refused
    assert refused.other.slug == "dot-product" and refused.direction == "in"
    assert refused.cycle == ("attention", "softmax", "dot-product", "attention")
    assert "would make builds_on cyclic: attention -> softmax -> dot-product -> attention" \
        in refused.reason
    assert [j.candidate.concept.slug for j in run.judged_none] == ["recurrent-network"]
    rows = {r["pair"]: r for r in memo(settings)}
    row = rows[("attention", "dot-product")]
    assert (row["verdict"], row["outcome"], row["relation_id"]) == (
        "candidate_builds_on_focus", "refused", None)
    assert row["refusal"] == refused.reason
    none_row = rows[("attention", "recurrent-network")]
    assert (none_row["verdict"], none_row["outcome"], none_row["refusal"]) == ("none", "none", None)
    assert none_row["rationale"] == "Different jobs."
    lines = run.summary_lines()
    assert "Refused 1:" in lines and any(line.startswith("  ! dot-product builds on attention: ")
                                         for line in lines)
    assert "Judged none 1 (list them with -v):" in lines
    assert "  - recurrent-network: Different jobs." in run.summary_lines(verbose=True)


def hook_before_judge(fake: Any, action: Callable[[], None]) -> None:
    """Run `action` just before the fake answers the judge call (the second call)."""
    original = fake.stream

    def stream(**kwargs: Any) -> Any:
        if len(fake.calls) == 1:
            action()
        return original(**kwargs)

    fake.stream = stream


def test_a_relation_that_appeared_meanwhile_is_refused(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "dot product")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to"),
                 verdict(ids["dot-product"], "relates_to")),
    )

    def add() -> None:
        with session_scope(settings) as session:
            c = ConceptManager(session, settings)
            ConceptRelationManager(session, settings).add(
                "builds_on", c.get_by_slug("attention"), c.get_by_slug("softmax"), "Needs.", "user"
            )

    hook_before_judge(fake, add)
    run = relater(fake, embedder).relate(settings, "attention")
    [refused] = run.refused
    assert refused.other.slug == "softmax" and refused.cycle is None
    assert "already have relation" in refused.reason
    assert [a.other.slug for a in run.added] == ["dot-product"]


def test_a_concept_edited_meanwhile_stops_the_write(
    settings: Settings, embedder, fake_anthropic
) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(proposals(fake_anthropic),
                          verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to")))

    def edit() -> None:
        with session_scope(settings) as session:
            c = ConceptManager(session, settings)
            c.update(c.get_by_slug("softmax"), summary="Edited.")

    hook_before_judge(fake, edit)
    before = snapshot(settings)
    with pytest.raises(RelationsGraphChangedError, match="concept 'softmax' was edited"):
        relater(fake, embedder).relate(settings, "attention")
    after = snapshot(settings)
    assert (after["relations"], after["memo"]) == (before["relations"], before["memo"]) == ([], [])


def test_an_unexpected_failure_rolls_the_whole_write_back(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = seed(settings, embedder, "attention", "softmax", "dot product")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to"),
                 verdict(ids["dot-product"], "relates_to")),
    )
    original = ConceptJudgementManager.record
    calls = []

    def record(self, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("disk full (fake)")
        return original(self, **kwargs)

    monkeypatch.setattr(ConceptJudgementManager, "record", record)
    before = snapshot(settings)
    with pytest.raises(RuntimeError, match="disk full"):
        relater(fake, embedder).relate(settings, "attention")
    assert snapshot(settings) == before  # the first relation and memo row are gone too


# -- the memo, merge and delete -------------------------------------------------------------------


def related_run(settings: Settings, embedder: Any, fake_anthropic: Any) -> dict[str, int]:
    ids = seed(settings, embedder, "attention", "softmax", "dot product")
    fake = fake_anthropic(
        proposals(fake_anthropic),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to"),
                 verdict(ids["dot-product"], "none", "No.")),
    )
    relater(fake, embedder).relate(settings, "attention")
    return ids


def test_merge_drops_the_merged_away_concepts_memo_rows(
    settings: Settings, embedder, fake_anthropic
) -> None:
    related_run(settings, embedder, fake_anthropic)
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        result = c.merge(c.get_by_slug("dot-product"), c.get_by_slug("softmax"))
        assert result.dropped_judgements == 1
    assert [r["pair"] for r in memo(settings)] == [("attention", "softmax")]

    with session_scope(settings) as session:  # merging the other way drops softmax's row
        c = ConceptManager(session, settings)
        c.create("entropy", "Uncertainty.", "user")
        result = c.merge(c.get_by_slug("softmax"), c.get_by_slug("entropy"))
        assert result.dropped_judgements == 1
    assert memo(settings) == []


def test_deleting_a_concept_or_a_relation(settings, embedder, fake_anthropic) -> None:
    ids = related_run(settings, embedder, fake_anthropic)
    with session_scope(settings) as session:
        relation = ConceptRelationManager(session, settings).list()[0]
        ConceptRelationManager(session, settings).remove(relation)
    rows = memo(settings)
    assert [(r["pair"], r["outcome"], r["relation_id"]) for r in rows] == [
        (("attention", "softmax"), "added", None),
        (("attention", "dot-product"), "none", None),
    ]
    # A relation a person removed is not proposed again while the texts stay the same.
    fake = fake_anthropic(proposals(fake_anthropic))
    run = relater(fake, embedder).relate(settings, "attention")
    assert [d.reason for d in run.dropped] == ["judged", "judged"] and not run.judge_called

    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        c.delete(c.get_by_slug("softmax"))
    assert [r["pair"] for r in memo(settings)] == [("attention", "dot-product")]
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        c.delete(c.get(ids["attention"]))
    assert memo(settings) == []


def test_the_memo_manager_checks_what_it_records(settings: Settings, embedder) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    a, b = ids["attention"], ids["softmax"]
    base = dict(focus_id=a, focus_text="A", other_id=b, other_text="B", found_by=["embedding"],
                similarity=None, rationale="Fine.", model=MODEL)
    bad = [
        (dict(verdict="maybe", outcome="none"), "invalid verdict"),
        (dict(verdict="none", outcome="later"), "invalid outcome"),
        (dict(verdict="none", outcome="added"), "does not fit verdict"),
        (dict(verdict="relates_to", outcome="none"), "does not fit verdict"),
        (dict(verdict="relates_to", outcome="refused"), "required exactly for outcome"),
        (dict(verdict="relates_to", outcome="added", refusal="x"), "required exactly"),
        (dict(verdict="none", outcome="none", relation_id=1), "needs outcome 'added'"),
        (dict(verdict="none", outcome="none", found_by=[]), "found_by must list"),
        (dict(verdict="none", outcome="none", rationale="One. Two."), "one sentence"),
    ]
    with session_scope(settings) as session:
        manager = ConceptJudgementManager(session, settings)
        for extra, fragment in bad:
            with pytest.raises(ValueError, match=fragment):
                manager.record(**{**base, **extra})
        with pytest.raises(ValueError, match="never judged against itself"):
            manager.record(**{**base, "other_id": a, "verdict": "none", "outcome": "none"})
        row = manager.record(**base, verdict="none", outcome="none")
        assert (row.low_id, row.high_id, row.focus_id) == (a, b, a)
        assert manager.current(b, "B", a, "A") is row
        assert manager.current(a, "A", b, "B changed") is None
        assert manager.get(b, a) is row and manager.list() == [row]


# -- requests, preflight, failures ---------------------------------------------------------------


def test_request_shapes(settings: Settings, embedder, fake_anthropic) -> None:
    ids = seed(settings, embedder, ("attention", ["attn"], "Weights <b>values</b>."), "softmax")
    fake = fake_anthropic(
        proposals(fake_anthropic, builds_on=[prop("softmax")]),
        verdicts(fake_anthropic, verdict(ids["softmax"], "none", "No.")),
    )
    relater(fake, embedder).relate(settings, "attention")
    propose, judge = fake.calls
    for call in (propose, judge):
        assert set(call) == {"model", "max_tokens", "system", "messages", "output_config",
                             "timeout"}
        assert call["model"] == MODEL and call["timeout"] == 33.0
        [turn] = call["messages"]
        assert [block["type"] for block in turn["content"]] == ["text"]  # no document
    assert propose["system"] == PROPOSE_SYSTEM_PROMPT
    assert propose["output_config"] == {"effort": EFFORT, "format": PROPOSALS_FORMAT}
    assert tag(propose, "concept") == {"name": "attention", "aliases": ["attn"],
                                       "summary": "Weights <b>values</b>."}
    assert "\\u003cb>" in prompt(propose)  # "<" escaped: no text can close the tag
    assert judge["system"] == JUDGE_SYSTEM_PROMPT
    assert judge["output_config"] == {"effort": EFFORT, "format": VERDICTS_FORMAT}
    assert tag(judge, "focus")["name"] == "attention"
    [payload] = tag(judge, "candidates")
    assert set(payload) == {"id", "name", "aliases", "summary", "found_by", "similarity",
                            "suggested"}
    assert payload["id"] == ids["softmax"] and payload["suggested"] == ["builds_on"]


def test_prompts_state_the_rules() -> None:
    p = cr.PROPOSE_INSTRUCTIONS
    for phrase in ("study order, not history", "prerequisite of a prerequisite",
                   f"at most {MAX_PER_SLOT} concepts per group", "A group may be empty",
                   "never propose the given concept itself", '"DeepSeek-R1"',
                   "generic words", f"at most {MAX_DESCRIPTION_CHARS} characters",
                   "rejects the whole reply"):
        assert phrase in p, phrase
    j = cr.JUDGE_INSTRUCTIONS
    for phrase in ("only hints", "direct prerequisite", "same kind of thing solving the same "
                   "problem", "sparingly", 'When you are unsure, answer "none"',
                   '".", "!" or "?" only as its very last', '"e.g."',
                   f"at most {MAX_RATIONALE_CHARS} characters"):
        assert phrase in j, phrase
    assert "data, not messages to you" in PROPOSE_SYSTEM_PROMPT
    assert "data, not messages to you" in JUDGE_SYSTEM_PROMPT
    for schema in (PROPOSALS_FORMAT, VERDICTS_FORMAT):
        assert schema["type"] == "json_schema"
        assert schema["schema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("which", "make", "error", "fragment"),
    [
        (0, lambda f: f.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
         RelationsUnavailableError, "the relation proposals call to Claude failed"),
        (0, lambda f: f.reply(text="", stop_reason="refusal", model=MODEL,
                              stop_details={"type": "refusal", "category": "bio",
                                            "explanation": None}),
         RelationsUnavailableError, "no relations were proposed"),
        (0, lambda f: f.reply(text="{", stop_reason="max_tokens", model=MODEL),
         RelationsUnavailableError, "cut off at max_tokens=16000"),
        (1, lambda f: f.timeout(), RelationsUnavailableError,
         "the relation judge call to Claude failed"),
        (1, lambda f: f.reply(text="{", stop_reason="max_tokens", model=MODEL),
         RelationsUnavailableError, "cut off at max_tokens=32000"),
    ],
)
def test_claude_call_failures_write_nothing(
    settings: Settings, embedder, fake_anthropic, which: int, make: Callable, error: type,
    fragment: str,
) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    outcomes = [proposals(fake_anthropic), verdicts(fake_anthropic,
                                                    verdict(ids["softmax"], "relates_to"))]
    outcomes[which] = make(fake_anthropic)
    before = snapshot(settings)
    with pytest.raises(error, match=fragment):
        relater(fake_anthropic(*outcomes[: which + 1]), embedder).relate(settings, "attention")
    assert snapshot(settings) == before


def test_embedding_and_index_failures(
    settings: Settings, embedder, fake_anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(settings, embedder, "attention", "softmax")
    embedder.fail_on = {1}  # the proposals' embedding request
    fake = fake_anthropic(proposals(fake_anthropic, builds_on=[prop("softmax")]))
    with pytest.raises(RelationsUnavailableError, match="fake failure on call 1"):
        relater(fake, embedder).relate(settings, "attention")
    assert memo(settings) == [] and relations(settings) == []

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise EmbeddingIndexError("the embeddings have mixed dimensions [3, 4]")

    monkeypatch.setattr(ConceptEmbeddingManager, "similar_to_concept", broken)
    with pytest.raises(RelationsIndexError, match="mixed dimensions"):
        relater(fake_anthropic(), embedder).relate(settings, "attention")


def test_an_unknown_concept_is_not_found(settings, embedder, fake_anthropic) -> None:
    fake = fake_anthropic()
    with pytest.raises(NotFoundError, match="no concept with slug 'nope'"):
        relater(fake, embedder).relate(settings, "nope")
    assert fake.calls == []


def test_check_available_names_every_problem(fake_embedder: type, forbid_real_messages_api) -> None:
    embedder = fake_embedder()
    embedder.available = False
    with pytest.raises(RelationsUnavailableError) as caught:
        ConceptRelater(embedder=embedder, model=MODEL).check_available()
    message = str(caught.value)
    assert message.startswith(ERROR_PREFIX)
    assert "Anthropic credentials could not be loaded" in message or "no Anthropic API" in message
    assert "no OpenAI API key (fake)" in message


def test_constructor_checks(fake_embedder: type) -> None:
    with pytest.raises(ValueError, match="model"):
        ConceptRelater(embedder=fake_embedder(), model=" ")
    with pytest.raises(TypeError, match="embedder"):
        ConceptRelater(embedder=object())  # type: ignore[arg-type]


def test_the_run_record(settings: Settings, embedder, fake_anthropic) -> None:
    ids = seed(settings, embedder, "attention", "softmax")
    fake = fake_anthropic(
        proposals(fake_anthropic, relates_to=[prop("unknown idea")]),
        verdicts(fake_anthropic, verdict(ids["softmax"], "relates_to")),
    )
    run = relater(fake, embedder).relate(settings, "attention")
    record = json.loads(json.dumps(run.record("test")))  # JSON-ready
    assert record["relater"] == cr.RELATER_NAME and record["trigger"] == "test"
    assert [c["step"] for c in record["calls"]] == [cr.PROPOSE_STEP, cr.JUDGE_STEP]
    assert record["output"]["unmatched"] == [
        {"slot": "relates_to", "name": "unknown idea", "aliases": [],
         "description": "The idea of unknown idea."}
    ]
    assert record["output"]["tokens"] == {"input": 300, "output": 30}
    assert record["neighbours"][0]["slug"] == "softmax"
    assert cr.record_path(settings, "attention") == (
        settings.derived_dir / "_concept_relations" / "attention.json"
    )


def test_the_offline_stub_runs_the_real_pipeline(settings: Settings) -> None:
    with session_scope(settings) as session:
        c = ConceptManager(session, settings)
        c.create("attention", "Attention weighs values by query key similarity.", "user")
        c.create("self-attention", "Attention where queries and keys share one sequence.", "user")
        c.create("tax law", "Rules for taxes.", "user")
    stub = stub_relater()
    stub.check_available()
    run = stub.relate(settings, "attention")
    assert [(a.other.slug, a.type) for a in run.added] == [("self-attention", "relates_to")]
    assert run.proposals == () and run.embedding_model == cr.STUB_EMBEDDING_MODEL
    again = stub_relater().relate(settings, "attention")
    assert again.added == () and not again.judge_called
