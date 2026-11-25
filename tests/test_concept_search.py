"""Concept search: the five match tiers, their order, the trigram index and its upkeep.

Pure helpers (`teacher.concept_search`) first, then `ConceptManager.search_hits` against a small
library of real concept names, then index sync, drift and missing-feature errors. Everything
runs on the test database of the run (SQLite, or Postgres with TEACHER_TEST_DB=postgres), with
the same expectations: the two backends (`teacher.db.search`) must agree. At the end: the
Postgres backend's own parts (FTS5's tokenizer and bm25 in Python, checked against this
SQLite's FTS5 on every run), its Postgres-only failure modes, and a parity test that runs the
same queries on a SQLite and a Postgres database and compares the hits exactly.
"""

from __future__ import annotations

import math
import random
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from teacher.concept_search import (
    KINDS,
    compact,
    edit_budget,
    fold,
    fuzzy_score,
    osa_distance,
    parse_query,
    substring_expression,
    trigram_expression,
    word_expression,
    words,
)
from teacher.config import Settings
from teacher.db import fts
from teacher.db.engine import dispose_engine, session_scope
from teacher.db.fts import TRIGRAM_SLOTS, ConceptSearchUnavailableError
from teacher.db.migrate import upgrade_to_head
from teacher.db.models import Concept
from teacher.db.search import _unicode61_data as unicode61_data
from teacher.db.search import postgres, unicode61
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    IntegrityCheckError,
    Issue,
)
from teacher.managers.concepts import SearchHit


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def relations(session: Session, settings: Settings) -> ConceptRelationManager:
    return ConceptRelationManager(session, settings)


def trigram_rows(session: Session) -> list[tuple[int, str, str, str]]:
    rows = session.execute(
        text("SELECT rowid, text, compact, label FROM concept_trigram ORDER BY rowid")
    )
    return [tuple(row) for row in rows]


def brief(hits: list[SearchHit]) -> list[tuple[str, str, str, str | None]]:
    return [(h.concept.slug, h.kind, h.field, h.matched) for h in hits]


# --- pure helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("Café  Effect", "cafe effect"),
        ("ＧＡＮ", "gan"),
        ("Straße", "strasse"),  # casefold, which the trigram tokenizer alone would not do
        ("ε-Greedy", "ε-greedy"),
        ("Ἀλφα", "αλφα"),
        ("ﬁne-tuning", "fine-tuning"),
    ],
)
def test_fold(raw: str, folded: str) -> None:
    assert fold(raw) == folded


def test_compact_and_words() -> None:
    assert compact("k-nn") == "knn" and compact("r1-zero (v2)") == "r1zerov2"
    assert words("k-nearest neighbours, β-vae") == ["k", "nearest", "neighbours", "β", "vae"]
    assert words("∇ --") == []


def test_parse_query() -> None:
    parsed = parse_query('  "Self-Attention"  (CAFÉ) ∇ ')
    assert parsed.norm == '"self-attention" (café) ∇'
    assert parsed.word_terms == ('"Self-Attention"', "(CAFÉ)")  # the symbol-only term is dropped
    assert parsed.terms == ("self-attention", "cafe", "∇")
    assert parsed.words == ("self", "attention", "cafe")
    assert parse_query("∇").word_terms == () and parse_query("∇").norm == "∇"


@pytest.mark.parametrize(
    ("query", "error", "fragment"),
    [
        ("", ValueError, "must not be empty"),
        (" \t\n", ValueError, "must not be empty"),
        ('( * "" -', ValueError, "has no letters or digits to search for"),
        (None, TypeError, "must be a string"),
        (b"x", TypeError, "must be a string"),
    ],
)
def test_parse_query_rejects(query: object, error: type, fragment: str) -> None:
    with pytest.raises(error, match=fragment):
        parse_query(query)


def test_fts_expressions_quote_everything() -> None:
    assert word_expression(['"a', "NEAR(x", "AND"]) == '"""a" "NEAR(x" "AND"'
    assert word_expression(["grad", "k-nn", "q"], prefix=True) == '"grad"* "k-nn"* "q"'
    assert substring_expression(["dient", "k-nn", "q", 'a"b']) == (
        '"dient" AND ("k-nn" OR "knn") AND "a""b"'
    )
    assert substring_expression(["ab", "x"]) is None  # too short for trigrams
    assert trigram_expression(["polcy", "ab"]) == '"lcy" OR "olc" OR "pol"'
    assert trigram_expression(["ab"]) is None
    with pytest.raises(ValueError, match="no terms"):
        word_expression([])


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [
        ("policy", "policy", 0),
        ("polcy", "policy", 1),  # deletion
        ("graident", "gradient", 1),  # adjacent swap counts once
        ("reinforcment", "reinforcement", 1),
        ("transfomer", "transformer", 1),
        ("kitten", "sitting", 3),
    ],
)
def test_osa_distance(a: str, b: str, distance: int) -> None:
    assert osa_distance(a, b, 5) == distance
    assert osa_distance(b, a, 5) == distance


def test_osa_distance_stops_past_the_cutoff() -> None:
    assert osa_distance("kitten", "sitting", 1) == 2
    assert osa_distance("a", "abcdef", 2) == 3


@pytest.mark.parametrize(
    ("word", "budget"),
    [("gan", 0), ("rnn", 0), ("gpt4", 0), ("resnet101", 0), ("adam", 1), ("gradent", 1),
     ("attention", 2), ("reinforcment", 2)],
)
def test_edit_budget(word: str, budget: int) -> None:
    assert edit_budget(word) == budget


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("polcy gradent", "policy gradient"),
        ("reinforcment learning", "reinforcement learning"),
        ("transfomer", "transformer"),
        ("graident descent", "gradient descent"),
        ("backpropogation", "backpropagation"),
        ("convolutonal neural network", "convolutional neural network"),
        ("pretrainig", "pre-training"),  # the compact comparison
        ("q-lerning", "q-learning"),
        ("embeding", "embedding"),
    ],
)
def test_fuzzy_score_accepts_typos(query: str, text: str) -> None:
    scored = fuzzy_score(words(fold(query)), fold(text))
    assert scored is not None and 0.8 < scored.score < 1.0


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("cat", "car"),  # 3 letters: no edits allowed
        ("rnn", "cnn"),
        ("gan", "gas"),
        ("gpt4", "gpt-3"),  # digits are never corrected
        ("resnet101", "resnet-50"),
        ("qolicy", "policy"),  # nor the first letter
        ("attention", "retention network"),
        ("polcy gradent", "gradient descent"),  # every query word must match
        ("xyzzy", "policy gradient"),
        ("lsmt", "lstm"),  # one swap, but "lsmt" is 4 letters: 1 edit is allowed ... see below
    ],
)
def test_fuzzy_score_rejects_unrelated(query: str, text: str) -> None:
    if query == "lsmt":
        # ... so it scores, but shares no trigram with "lstm": the index never offers it as a
        # candidate (a documented limit, tested in test_search_tiers).
        assert fuzzy_score([query], text) is not None
        assert set(trigram_expression([query]).split(" OR ")).isdisjoint({'"lst"', '"stm"'})
        return
    assert fuzzy_score(words(fold(query)), fold(text)) is None


def test_two_edits_are_a_weaker_match_than_one() -> None:
    one = fuzzy_score(["transfomer"], "transformer")  # 1 edit
    two = fuzzy_score(["transfomer"], "transfer learning")  # 2 edits, allowed from 9 letters
    assert one is not None and two is not None and two.score < one.score


def test_fuzzy_score_prefers_close_and_complete_names() -> None:
    exact = fuzzy_score(["transfomer"], "transformer")
    longer = fuzzy_score(["transfomer"], "vision transformer")
    assert exact is not None and longer is not None
    assert exact.score == longer.score  # the same word matched, so the same score ...
    assert (exact.extra, longer.extra) == (0, 1)  # ... and the tie-break is the extra word


# --- the search tiers ---------------------------------------------------------------------


LIBRARY: list[tuple[str, str, str, list[str]]] = [
    ("policy-gradient", "Policy gradient",
     "Ascend the gradient of expected return in the policy parameters.", ["PG", "REINFORCE"]),
    ("reinforcement-learning", "Reinforcement learning", "Learning to act from rewards.", ["RL"]),
    ("transformer", "Transformer", "A sequence model built from attention layers.", []),
    ("vision-transformer", "Vision transformer", "A transformer over image patches.", ["ViT"]),
    ("gradient-descent", "Gradient descent", "Repeatedly step against the gradient.",
     ["steepest descent"]),
    ("k-nn", "k-NN", "Classify by the majority of the closest training points.",
     ["k-nearest neighbours", "kNN"]),
    ("r1-zero", "R1-Zero", "A reasoning model trained with reinforcement learning only.", []),
    ("beta-vae", "β-VAE", "A variational autoencoder with a weighted KL term.", ["beta-VAE"]),
    ("epsilon-greedy", "ε-greedy exploration", "Pick a random action with probability ε.", []),
    ("cafe-effect", "Café effect", "A perceptual illusion of tilted lines.", []),
    ("nabla", "∇", "The vector of partial derivatives.", ["nabla"]),
    ("attention", "Attention", "Weigh inputs by relevance.", []),
    ("attentional-blink", "Attentional blink", "Missing a second target shortly after a first.",
     []),
    ("attentive-pooling", "Attentive pooling", "Pool a sequence with learned weights.", []),
]


@pytest.fixture
def library(concepts: ConceptManager, session: Session) -> dict[str, Concept]:
    made = {
        slug: concepts.create(name, summary, "paper", aliases=aliases, slug=slug)
        for slug, name, summary, aliases in LIBRARY
    }
    session.commit()
    return made


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # 1. exact: the name or an alias, normalised
        ("pg", [("policy-gradient", "exact", "alias", "PG")]),
        ("  POLICY   gradient ", [("policy-gradient", "exact", "name", "Policy gradient")]),
        ("knn", [("k-nn", "exact", "alias", "kNN")]),
        ("β-VAE", [("beta-vae", "exact", "name", "β-VAE")]),
        ("∇", [("nabla", "exact", "name", "∇")]),  # no letters or digits, still findable
        ("CAFÉ EFFECT", [("cafe-effect", "exact", "name", "Café effect")]),
        # 2. word: whole words of name, aliases or summary (case and diacritics ignored)
        ("steepest", [("gradient-descent", "word", "alias", "steepest descent")]),
        ("cafe", [("cafe-effect", "word", "name", "Café effect")]),
        ("k nn", [("k-nn", "word", "name", "k-NN")]),
        ("r1", [("r1-zero", "word", "name", "R1-Zero")]),
        ("β", [("beta-vae", "word", "name", "β-VAE")]),
        ("ε", [("epsilon-greedy", "word", "name", "ε-greedy exploration")]),
        ("patches", [("vision-transformer", "word", "summary", None)]),
        # 3. prefix
        ("neighbour", [("k-nn", "prefix", "alias", "k-nearest neighbours")]),
        ("grad", [("gradient-descent", "prefix", "name", "Gradient descent"),
                  ("policy-gradient", "prefix", "name", "Policy gradient")]),
        # 4. substring: infix or suffix of a name or alias, folded, or compact
        ("dient", [("policy-gradient", "substring", "name", "Policy gradient"),  # 5/14 covered
                   ("gradient-descent", "substring", "name", "Gradient descent")]),  # 5/15
        ("former", [("transformer", "substring", "name", "Transformer"),
                    ("vision-transformer", "substring", "name", "Vision transformer")]),
        ("earest", [("k-nn", "substring", "alias", "k-nearest neighbours")]),
        ("afe eff", [("cafe-effect", "substring", "name", "Café effect")]),
        ("r1zero", [("r1-zero", "substring", "name", "R1-Zero")]),
        ("betavae", [("beta-vae", "substring", "alias", "beta-VAE")]),
        ("nforcement", [("reinforcement-learning", "substring", "name",
                         "Reinforcement learning")]),  # R1-Zero's summary is not searched
        # 5. fuzzy
        ("polcy gradent", [("policy-gradient", "fuzzy", "name", "Policy gradient")]),
        ("reinforcment learning", [("reinforcement-learning", "fuzzy", "name",
                                    "Reinforcement learning")]),
        ("transfomer", [("transformer", "fuzzy", "name", "Transformer"),
                        ("vision-transformer", "fuzzy", "name", "Vision transformer")]),
        ("graident", [("policy-gradient", "fuzzy", "name", "Policy gradient"),
                      ("gradient-descent", "fuzzy", "name", "Gradient descent")]),
        # nothing
        ("qolicy", []),  # the first letter is never corrected
        ("lsmt", []),
        ("xyzzy", []),
        ("zz", []),
        ("ff", []),  # inside "effect", but 2 characters cannot use the trigram index ...
        ("ffe", [("cafe-effect", "substring", "name", "Café effect")]),  # ... 3 can
    ],
)
def test_search_tiers(
    concepts: ConceptManager, library: dict[str, Concept], query: str, expected: list[Any]
) -> None:
    assert brief(concepts.search_hits(query)) == expected


def test_tiers_are_ranked_in_order_and_whole_words_beat_prefixes(
    concepts: ConceptManager, library: dict[str, Concept]
) -> None:
    hits = concepts.search_hits("attention")
    assert brief(hits) == [
        ("attention", "exact", "name", "Attention"),
        ("transformer", "word", "summary", None),  # a whole word, if only in the summary, ...
        ("attentional-blink", "prefix", "name", "Attentional blink"),  # ... beats a name prefix
        ("attentive-pooling", "fuzzy", "name", "Attentive pooling"),  # and typos come last
    ]
    assert hits[0].score == 1.0 and hits[1].score > 0 and 0 < hits[3].score < 1
    assert concepts.search("attention") == [hit.concept for hit in hits]


@pytest.mark.parametrize(
    "query",
    ["attention", "transformer", "policy gradient", "gradient", "grad", "dient", "transfomer",
     "learning", "a", "ion", "k-nn", "rl"],
)
def test_every_concept_appears_once_at_its_best_tier(
    concepts: ConceptManager, library: dict[str, Concept], query: str
) -> None:
    hits = concepts.search_hits(query, limit=100)
    ids = [hit.concept.id for hit in hits]
    assert len(ids) == len(set(ids))
    ranks = [KINDS.index(hit.kind) for hit in hits]
    assert ranks == sorted(ranks)
    for kind in KINDS:  # within a kind, best score first
        scores = [hit.score for hit in hits if hit.kind == kind]
        assert scores == sorted(scores, reverse=True), (query, kind)
    for hit in hits:  # and each hit is the best kind that finds that concept
        for better in KINDS[: KINDS.index(hit.kind)]:
            assert hit.concept.id not in {
                h.concept.id for h in concepts.search_hits(query, limit=100, kinds={better})
            }, (query, hit.concept.slug, better)


def test_one_concept_matching_every_tier_is_reported_once_as_exact(
    concepts: ConceptManager, library: dict[str, Concept]
) -> None:
    pg = library["policy-gradient"]
    for kind in KINDS:
        assert pg in concepts.search("policy gradient", kinds={kind}), kind
    assert brief(concepts.search_hits("policy gradient")) == [
        ("policy-gradient", "exact", "name", "Policy gradient")
    ]


def test_kinds_restricts_the_tiers(concepts: ConceptManager, library: dict[str, Concept]) -> None:
    assert concepts.search("transfomer", kinds={"exact", "word", "prefix", "substring"}) == []
    assert concepts.search("grad", kinds=("exact", "word")) == []
    # without the word tier, whole words are reported by the prefix tier
    assert brief(concepts.search_hits("steepest", kinds=["prefix"])) == [
        ("gradient-descent", "prefix", "alias", "steepest descent")
    ]
    assert [h.kind for h in concepts.search_hits("former", kinds={"substring", "fuzzy"})] == [
        "substring", "substring"
    ]


@pytest.mark.parametrize(
    ("kinds", "error", "fragment"),
    [
        ("fuzzy", TypeError, "collection of match kinds"),
        (None, TypeError, "collection of match kinds"),
        ({"typo"}, ValueError, r"unknown match kind\(s\) \['typo'\]"),
        ((), ValueError, "at least one"),
    ],
)
def test_bad_kinds(concepts: ConceptManager, kinds: object, error: type, fragment: str) -> None:
    with pytest.raises(error, match=fragment):
        concepts.search_hits("attention", kinds=kinds)  # type: ignore[arg-type]


def test_limit_keeps_the_best_hits(concepts: ConceptManager, library: dict[str, Concept]) -> None:
    full = concepts.search_hits("attention")
    for limit in range(1, len(full) + 1):
        assert concepts.search_hits("attention", limit=limit) == full[:limit]
    assert concepts.search("a", limit=3) == [h.concept for h in concepts.search_hits("a")[:3]]


def test_short_terms_and_acronyms(concepts: ConceptManager, library: dict[str, Concept]) -> None:
    assert concepts.search("RL") == [library["reinforcement-learning"]]
    assert concepts.search("vit") == [library["vision-transformer"]]
    # 2-letter prefixes are matched, 1-letter ones only as whole words
    assert concepts.search("st") == [library["gradient-descent"]]  # "steepest", "step"
    assert concepts.search("p") == []  # no word "p"; 1 letter is not a prefix
    # a short term next to a long one is still required, on the rows the long one finds
    assert concepts.search("k nearest") == [library["k-nn"]]
    assert concepts.search("x nearest") == []


def test_diacritics_and_case_are_ignored_by_every_tier(
    concepts: ConceptManager, session: Session
) -> None:
    concept = concepts.create("Ångström exponent", "How aerosol extinction varies.", "paper")
    assert brief(concepts.search_hits("angstrom")) == [
        ("angstrom-exponent", "word", "name", "Ångström exponent")
    ]
    assert brief(concepts.search_hits("NGSTRÖ")) == [
        ("angstrom-exponent", "substring", "name", "Ångström exponent")
    ]
    assert concepts.search("angstorm exponent") == [concept]  # fuzzy, folded
    assert trigram_rows(session) == [
        (concept.id * TRIGRAM_SLOTS, "angstrom exponent", "angstromexponent", "Ångström exponent")
    ]


# --- query syntax can never reach FTS5 ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        '"', '"policy', 'policy"', '"policy gradient"', '""""', "*", "grad*", "* grad",
        "-gradient", "NOT gradient", "gradient OR descent", "gradient AND", "AND", "OR", "NEAR",
        "NEAR(gradient descent)", "NEAR(gradient descent, 2)", "text:gradient", "compact:knn",
        "label:PG", "name:policy", "{text compact}: grad", "(gradient", "gradient)", "^gradient",
        "gradient +descent", "a'b", "\\", "%", "_grad_", "k-NN", "R1-Zero", "ε", "β-VAE", "∇",
        "∇·F", "x" * 300, "pol" * 50, "ab\x00cd",
    ],
)
def test_no_query_causes_an_fts_syntax_error(
    concepts: ConceptManager, library: dict[str, Concept], query: str
) -> None:
    try:
        parse_query(query)
    except ValueError:
        with pytest.raises(ValueError, match="no letters or digits"):
            concepts.search_hits(query)
        return
    for kind in KINDS:  # each tier on its own, so each index is queried
        concepts.search_hits(query, kinds={kind})
    concepts.search_hits(query)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("NOT gradient", []),  # "not" is a word to find, not an operator
        ("gradient OR descent", []),  # likewise "or"
        ("text:gradient", []),  # no column filters, in either index
        ("compact:knn", []),
        ("label:PG", []),
        ("grad*", ["gradient-descent", "policy-gradient"]),  # the star is not ours: "grad" prefix
        ("-gradient", ["gradient-descent", "policy-gradient"]),
        ("NEAR(gradient descent)", []),
        ('"steepest descent"', ["gradient-descent"]),  # quotes are just punctuation
    ],
)
def test_operators_in_the_query_are_text(
    concepts: ConceptManager, library: dict[str, Concept], query: str, expected: list[str]
) -> None:
    exact_word_substring = {"exact", "word", "prefix", "substring"}
    assert [c.slug for c in concepts.search(query, kinds=exact_word_substring)] == expected


# --- keeping the trigram index in sync ----------------------------------------------------


def test_trigram_rows_hold_the_folded_name_and_aliases(
    concepts: ConceptManager, session: Session
) -> None:
    concept = concepts.create("Policy Gradient", "S.", "paper", aliases=["REINFORCE", "PG"])
    base = concept.id * TRIGRAM_SLOTS
    assert trigram_rows(session) == [
        (base, "policy gradient", "policygradient", "Policy Gradient"),
        (base + 1, "pg", "pg", "PG"),  # aliases in alias order (normalised, then as stored)
        (base + 2, "reinforce", "reinforce", "REINFORCE"),
    ]


def test_trigram_index_follows_every_write(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    pg = concepts.create("Policy gradeint", "Ascend the return.", "paper", aliases=["PG"])
    reinforce = concepts.create(
        "REINFORCE", "Monte Carlo policy gradient.", "paper", aliases=["Williams' algorithm"]
    )
    relations.add("builds_on", reinforce, pg, "It is one.", "paper")
    substring = {"substring"}
    assert concepts.search("deint", kinds=substring) == [pg]

    concepts.update(pg, name="Policy gradient")
    assert concepts.search("deint", kinds=substring) == []
    assert concepts.search("dient", kinds=substring) == [pg]
    assert concepts.search("polcy gradient", kinds={"fuzzy"}) == [pg]

    concepts.add_alias(pg, "Score-function estimator")
    assert brief(concepts.search_hits("unction estim")) == [
        ("policy-gradeint", "substring", "alias", "Score-function estimator")
    ]
    concepts.remove_alias(pg, "score-function estimator")
    assert concepts.search("unction estim") == []

    result = concepts.merge(reinforce, pg)  # REINFORCE's name and aliases become pg's aliases
    assert result.moved_aliases == ("REINFORCE", "Williams' algorithm")
    assert brief(concepts.search_hits("illiams")) == [
        ("policy-gradeint", "substring", "alias", "Williams' algorithm")
    ]
    assert brief(concepts.search_hits("reinforse")) == [
        ("policy-gradeint", "fuzzy", "alias", "REINFORCE")  # the slug never changes
    ]
    assert {row[0] // TRIGRAM_SLOTS for row in trigram_rows(session)} == {pg.id}
    session.commit()
    assert concepts.check() == []

    concepts.delete(pg)
    assert trigram_rows(session) == []
    assert concepts.search("illiams") == []
    session.commit()
    assert concepts.check() == []


def test_a_failed_index_write_rolls_back_the_whole_change(
    concepts: ConceptManager, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    concept = concepts.create("Policy gradient", "S.", "paper", aliases=["PG"])
    session.commit()
    before = (trigram_rows(session), session.execute(text("SELECT * FROM concept_search")).all())

    def broken(self: ConceptManager, concept: Concept) -> list[Any]:
        raise RuntimeError("disk full")  # after both old rows are deleted and the word row added

    with monkeypatch.context() as patch:
        patch.setattr(ConceptManager, "_trigram_values", broken)
        with pytest.raises(RuntimeError, match="disk full"):
            concepts.add_alias(concept, "REINFORCE")
    session.expire_all()
    assert [a.alias for a in concept.aliases] == ["PG"]
    assert (
        trigram_rows(session), session.execute(text("SELECT * FROM concept_search")).all()
    ) == before
    assert concepts.check() == []


def test_too_many_aliases_for_the_rowid_scheme_is_an_error(
    concepts: ConceptManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("teacher.managers.concepts.TRIGRAM_SLOTS", 2)  # name + 1 alias
    concepts.create("Attention", "S.", "paper", aliases=["attn"])
    with pytest.raises(ValueError, match="has 2 aliases; the trigram index holds at most 1"):
        concepts.create("Softmax", "S.", "paper", aliases=["a", "b"])
    assert [c.slug for c in concepts.list()] == ["attention"]


def test_check_reports_and_rebuild_repairs_trigram_drift(
    concepts: ConceptManager, library: dict[str, Concept], session: Session
) -> None:
    pg, knn, gd = library["policy-gradient"], library["k-nn"], library["gradient-descent"]
    session.execute(
        text("DELETE FROM concept_trigram WHERE rowid >= :low AND rowid < :high"),
        {"low": pg.id * TRIGRAM_SLOTS, "high": (pg.id + 1) * TRIGRAM_SLOTS},
    )
    session.execute(
        text("UPDATE concept_trigram SET label = 'kNN!' WHERE rowid = :r"),
        {"r": knn.id * TRIGRAM_SLOTS + 2},
    )
    session.execute(  # an alias row missing is drift too
        text("DELETE FROM concept_trigram WHERE rowid = :r"), {"r": gd.id * TRIGRAM_SLOTS + 1}
    )
    session.execute(
        text(
            "INSERT INTO concept_trigram (rowid, text, compact, label) "
            "VALUES (:r, 'ghost concept', 'ghostconcept', 'Ghost concept')"
        ),
        {"r": 99 * TRIGRAM_SLOTS},
    )
    detail = "concept_trigram rows differ from the concept's name and aliases"
    assert concepts.check() == [  # by slug, then the orphans
        Issue("stale_trigram_rows", "gradient-descent", detail),
        Issue("stale_trigram_rows", "k-nn", detail),
        Issue("missing_trigram_rows", "policy-gradient", "no concept_trigram rows"),
        Issue(
            "orphan_trigram_rows", "#99",
            "concept_trigram has rows for concept id 99, which does not exist",
        ),
    ]
    with pytest.raises(
        IntegrityCheckError,
        match=r"the concept_trigram index has rows for concepts that do not exist \(ids \[99\]\)",
    ):
        concepts.search("ghost")
    assert concepts.search("dient", kinds={"substring"}) == [gd]  # pg's rows are gone

    assert concepts.rebuild_search_index() == len(LIBRARY)
    assert concepts.check() == []
    assert concepts.search("ghost") == []
    assert concepts.search("dient", kinds={"substring"}) == [pg, gd]


def test_missing_trigram_table_is_a_clear_error_and_rolls_back(
    concepts: ConceptManager, session: Session, test_db: str
) -> None:
    session.execute(text("DROP TABLE concept_trigram"))
    session.commit()
    missing = "concept_trigram trigram table is missing"
    with pytest.raises(ConceptSearchUnavailableError, match=missing):
        concepts.create("Attention", "S.", "paper")
    assert concepts.list() == []  # the savepoint was rolled back with the failed sync
    session.execute(text(  # a word-index row so the word tiers find something
        "INSERT INTO concepts (id, slug, name, name_norm, summary, origin) "
        "VALUES (1, 'attention', 'Attention', 'attention', 'S.', 'user')"
    ))
    session.execute(text(
        "INSERT INTO concept_search (rowid, name, aliases, summary) "
        "VALUES (1, 'Attention', '', 'S.')"
    ))
    if test_db == "postgres":  # the row's word
        session.execute(text("INSERT INTO concept_search_tokens VALUES (1, 0, 0, 'attention')"))
    session.commit()
    assert concepts.search("attention", kinds={"exact", "word"}) == concepts.list()
    for query in ("attention", "tention", "atention"):
        with pytest.raises(ConceptSearchUnavailableError, match="upgrade_to_head"):
            concepts.search(query)
        session.rollback()  # on Postgres a failed statement ends the transaction
    with pytest.raises(ConceptSearchUnavailableError):
        concepts.check()


@pytest.mark.sqlite_only("SQLite's FTS5 trigram tokenizer")
def test_missing_trigram_tokenizer_at_use_is_a_clear_error(
    concepts: ConceptManager, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a database made with SQLite >= 3.34 does when opened by an older SQLite."""
    concepts.create("Attention", "S.", "paper")
    real_execute = session.execute

    def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "concept_trigram" in str(statement):
            raise OperationalError(
                str(statement), {}, sqlite3.OperationalError("no such tokenizer: trigram")
            )
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", execute)
    with pytest.raises(ConceptSearchUnavailableError, match=r"no FTS5 trigram tokenizer.*3\.34"):
        concepts.search("tention")
    with pytest.raises(ConceptSearchUnavailableError, match="no FTS5 trigram tokenizer"):
        concepts.create("Softmax", "S.", "paper")
    with pytest.raises(ConceptSearchUnavailableError, match="no FTS5 trigram tokenizer"):
        concepts.rebuild_search_index()
    # tiers that do not use the trigram index still work: nothing silently replaces it
    assert [c.slug for c in concepts.search("attention", kinds={"exact", "word"})] == ["attention"]


# --- the Postgres backend's parts, checked against this SQLite's FTS5 ----------------------


def test_unicode61_tables_match_this_sqlite() -> None:
    """The tokenizer's tables are what this SQLite's FTS5 does with every code point."""
    connection = sqlite3.connect(":memory:")
    try:
        ranges, folds = unicode61.probe_fts5(connection)
    finally:
        connection.close()
    assert unicode61.render_tables(ranges, folds) == (
        unicode61_data.SEPARATOR_RANGES.strip(),
        unicode61_data.FOLDS.strip(),
    ), (
        f"FTS5's unicode61 tokenizer in SQLite {sqlite3.sqlite_version} differs from the tables "
        f"probed from SQLite {unicode61_data.SQLITE_VERSION}: regenerate them with "
        "`python -m teacher.db.search.unicode61 src/teacher/db/search/_unicode61_data.py`"
    )


def _fts5_tokens(connection: sqlite3.Connection, value: str) -> list[str]:
    connection.execute("DELETE FROM t")
    connection.execute("INSERT INTO t (rowid, x) VALUES (1, ?)", (value,))
    return [term for (term,) in connection.execute("SELECT term FROM v ORDER BY offset")]


def test_unicode61_tokenizer_matches_fts5() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE VIRTUAL TABLE t USING fts5(x, tokenize = 'unicode61 remove_diacritics 2')"
    )
    connection.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
    rng = random.Random(7)
    pool = (
        list("abcXYZ019 -_.,'\"\n\t()*:") + [chr(c) for c in range(0xC0, 0x250)]
        + [chr(c) for c in range(0x300, 0x370)] + list("αβΓΔάἀ∇ε①ßﬁİǅ😀中文한‍॑ि")
    )
    samples = [name for _, name, _, _ in LIBRARY] + [s for *_, aliases in LIBRARY for s in aliases]
    samples += ["", " ", "́", "é", "a" * 40000, "é" * 20000]
    samples += ["".join(rng.choice(pool) for _ in range(rng.randint(0, 30))) for _ in range(2000)]
    for value in samples:
        assert list(unicode61.tokens(value)) == _fts5_tokens(connection, value), value
    connection.close()
    assert unicode61.tokenize("k-NN, Café\nβ-VAE") == [
        ("k", 0), ("nn", 2), ("cafe", 6), ("β", 11), ("vae", 13)
    ]


def _unfused_bm25(freqs: list[float], idfs: list[float], size: int, avgdl: float) -> float:
    score = 0.0
    for f, idf in zip(freqs, idfs, strict=True):
        score += idf * ((f * 2.2) / (f + 1.2 * (1 - 0.75 + 0.75 * size / avgdl)))
    return -1.0 * score


def test_bm25_is_fts5s() -> None:
    """`postgres.bm25` and `postgres.idf` give FTS5's bm25(t, 10, 5, 1) for every row of a
    random corpus: bit for bit when this SQLite build fuses multiply-adds as the backend does
    (clang/arm64 builds), else the unfused formula bit for bit and ours within rounding."""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE t USING fts5(a, b, c)")
    rng = random.Random(3)
    vocabulary = [f"w{i}" for i in range(10)]
    docs = {}
    for rowid in range(1, 300):
        cols = [[rng.choice(vocabulary) for _ in range(rng.randint(0, 8))] for _ in range(3)]
        docs[rowid] = cols
        connection.execute(
            "INSERT INTO t (rowid, a, b, c) VALUES (?, ?, ?, ?)",
            (rowid, *(" ".join(col) for col in cols)),
        )
    avgdl = float(sum(len(c) for d in docs.values() for c in d)) / float(len(docs))
    ours, unfused, theirs = [], [], []
    for _ in range(200):
        query = rng.sample(vocabulary, rng.randint(1, 3))
        idfs = [
            postgres.idf(len(docs), sum(1 for d in docs.values() if any(w in c for c in d)))
            for w in query
        ]
        for rowid, rank in connection.execute(
            "SELECT rowid, bm25(t, 10.0, 5.0, 1.0) FROM t WHERE t MATCH ?",
            (" ".join(f'"{w}"' for w in query),),
        ):
            cols = docs[rowid]
            freqs = [sum(wt * c.count(w) for wt, c in zip((10.0, 5.0, 1.0), cols)) for w in query]
            size = sum(len(c) for c in cols)
            ours.append(postgres.bm25(freqs, idfs, size, avgdl))
            unfused.append(_unfused_bm25(freqs, idfs, size, avgdl))
            theirs.append(rank)
    connection.close()
    assert len(theirs) > 5000
    if ours == theirs:
        return
    assert unfused == theirs, "FTS5's bm25 is neither the fused nor the unfused formula"
    assert all(math.isclose(a, b, rel_tol=1e-14) for a, b in zip(ours, theirs, strict=True))


def test_fma_is_exact() -> None:
    rng = random.Random(0)
    for _ in range(20000):
        a, b, c = (rng.uniform(-50, 50) * 10 ** rng.randint(-6, 6) for _ in range(3))
        assert postgres.fma(a, b, c) == float(Fraction(a) * Fraction(b) + Fraction(c))


class _FakeConnection:
    """Answers require_pg_trgm's queries: whether pg_trgm is installed, offered, and whether
    there is an `extensions` schema; CREATE EXTENSION raises `create_error` if set."""

    def __init__(self, *, installed: str | None, offered: bool, extensions_schema: bool,
                 create_error: Exception | None = None) -> None:
        self.installed, self.offered, self.extensions_schema = installed, offered, extensions_schema
        self.create_error = create_error
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> Any:
        self.statements.append(statement)
        if statement.startswith("CREATE EXTENSION"):
            if self.create_error is not None:
                raise self.create_error
            self.installed = "extensions" if "WITH SCHEMA extensions" in statement else "public"
            return None
        if "FROM pg_extension" in statement:
            value: object = self.installed
        elif "pg_available_extensions" in statement:
            value = int(self.offered)
        else:
            value = int(self.extensions_schema)
        return type("Result", (), {"scalar": staticmethod(lambda: value)})()


def test_require_pg_trgm() -> None:
    assert fts.require_pg_trgm(
        _FakeConnection(installed="public", offered=True, extensions_schema=True)
    ) == "public"  # an installed pg_trgm is used where it is
    supabase = _FakeConnection(installed=None, offered=True, extensions_schema=True)
    assert fts.require_pg_trgm(supabase) == "extensions"
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions" in supabase.statements
    plain = _FakeConnection(installed=None, offered=True, extensions_schema=False)
    assert fts.require_pg_trgm(plain) == "public"
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in plain.statements
    with pytest.raises(ConceptSearchUnavailableError, match="does not offer the pg_trgm"):
        fts.require_pg_trgm(_FakeConnection(installed=None, offered=False, extensions_schema=True))
    denied = DBAPIError(
        "CREATE EXTENSION", {}, Exception("permission denied to create extension \"pg_trgm\"")
    )
    with pytest.raises(
        ConceptSearchUnavailableError,
        match=r"could not install the pg_trgm extension \(permission denied.*CREATE privilege",
    ):
        fts.require_pg_trgm(
            _FakeConnection(
                installed=None, offered=True, extensions_schema=True, create_error=denied
            )
        )


def test_words_are_fts5s_tokens_and_aliases_are_one_column(
    concepts: ConceptManager, session: Session, test_db: str
) -> None:
    concept = concepts.create("k-NN Café", "Ångström's R1-Zero, β-VAE.", "paper",
                              aliases=["ε-greedy", "AB"], slug="knn-cafe")
    if test_db == "postgres":  # the words the Postgres backend stores
        rows = session.execute(
            text("SELECT col, pos, token FROM concept_search_tokens WHERE rowid = :r "
                 "ORDER BY col, pos"), {"r": concept.id}
        ).all()
        assert [tuple(row) for row in rows] == [
            (0, 0, "k"), (0, 1, "nn"), (0, 2, "cafe"),
            (1, 0, "ab"), (1, 1, "ε"), (1, 2, "greedy"),  # aliases in alias order
            (2, 0, "angstrom"), (2, 1, "s"), (2, 2, "r1"), (2, 3, "zero"), (2, 4, "β"),
            (2, 5, "vae"),
        ]
    # a phrase may run across two aliases (one column, as FTS5 has it); the first alias
    # holding a matched word is named
    assert brief(concepts.search_hits("ab-ε", kinds={"word"})) == [
        ("knn-cafe", "word", "alias", "AB")
    ]
    assert brief(concepts.search_hits("greedy ab", kinds={"word"})) == [
        ("knn-cafe", "word", "alias", "AB")
    ]
    assert brief(concepts.search_hits("s-r1", kinds={"word"})) == [
        ("knn-cafe", "word", "summary", None)
    ]
    assert concepts.search("cafe-angstrom", kinds={"word", "prefix"}) == []  # not one column


# --- Postgres only --------------------------------------------------------------------------


@pytest.mark.postgres_only("the Postgres trigram indexes")
def test_missing_trigram_index_is_a_clear_error(
    concepts: ConceptManager, library: dict[str, Concept], session: Session
) -> None:
    session.execute(text("DROP INDEX ix_concept_trigram_compact"))
    session.commit()
    for kinds in ({"word"}, {"substring"}, {"fuzzy"}):
        with pytest.raises(
            ConceptSearchUnavailableError,
            match=r"ix_concept_trigram_compact are missing.*pg_trgm.*CREATE INDEX "
            r"ix_concept_trigram_compact ON concept_trigram USING gin \(compact gin_trgm_ops\)",
        ):
            concepts.search("dient", kinds=kinds)
        session.rollback()
    session.execute(
        text("CREATE INDEX ix_concept_trigram_compact ON concept_trigram USING gin "
             "(compact gin_trgm_ops)")
    )
    session.commit()
    assert concepts.search("dient", kinds={"substring"}) == [
        library["policy-gradient"], library["gradient-descent"]
    ]


@pytest.mark.postgres_only("the Postgres word table")
def test_check_reports_word_table_drift(
    concepts: ConceptManager, library: dict[str, Concept], session: Session
) -> None:
    pg, knn = library["policy-gradient"], library["k-nn"]
    session.execute(text("DELETE FROM concept_search_tokens WHERE rowid = :r AND pos = 0"),
                    {"r": pg.id})
    session.execute(text("UPDATE concept_search_tokens SET token = 'x' WHERE rowid = :r"),
                    {"r": knn.id})
    session.execute(text("INSERT INTO concept_search_tokens VALUES (99, 0, 0, 'ghost')"))
    detail = "concept_search row differs from the concept"
    assert concepts.check() == [
        Issue("stale_search_row", "k-nn", detail),
        Issue("stale_search_row", "policy-gradient", detail),
        Issue("orphan_search_row", "#99", "concept_search row 99 has no concept"),
    ]
    with pytest.raises(IntegrityCheckError, match="has no concept_search row"):
        concepts.search("ghost", kinds={"word"})
    assert concepts.rebuild_search_index() == len(LIBRARY)
    assert concepts.check() == []
    assert concepts.search("ghost") == []


# --- parity: the same queries on SQLite and on Postgres ------------------------------------


PARITY_QUERIES = [
    "attention", "transformer", "policy gradient", "gradient", "grad", "dient", "transfomer",
    "learning", "a", "ion", "k-nn", "rl", "st", "k nearest", "steepest descent", "former",
    "reinforcment learning", "graident", "cafe", "café effect", "β", "ε", "∇", "nabla",
    "r1", "r1zero", "betavae", "vae", "the", "a sequence", "polcy gradent", "atention",
    '"steepest descent"', "grad*", "-gradient", "NOT gradient", "model", "weights",
    "learned weights", "partial", "lines", "x" * 50, "pre", "pretrainig", "neighbour",
    "variational", "autoencoder", "kl", "image patches", "first target", "trained",
]


@pytest.mark.postgres_only("compares this Postgres database with a SQLite one")
def test_sqlite_and_postgres_give_the_same_hits(
    concepts: ConceptManager, library: dict[str, Concept], session: Session, tmp_path: Path
) -> None:
    """Every tier on its own and all together, with the same scores (bm25 bit for bit when
    the local SQLite fuses multiply-adds; see test_bm25_is_fts5s)."""
    extra = [  # more rows, so that bm25's corpus statistics matter
        ("Pre-training", "Train a model on unlabeled data first.", ["pretraining"]),
        ("Fine-tuning", "Adapt a pre-trained model with learned weights.", ["finetuning"]),
        ("Variational autoencoder", "An autoencoder with a KL term.", ["VAE"]),
        ("Learning rate", "How far a gradient step goes.", ["step size", "LR"]),
        ("Self-attention", "Attention of a sequence to itself.", ["intra-attention"]),
        ("Gradient clipping", "Cap the gradient norm.", []),
        ("Natural gradient", "Gradient in the Fisher metric; learning faster.", ["NG"]),
    ]
    for name, summary, aliases in extra:
        concepts.create(name, summary, "paper", aliases=aliases)
    session.commit()
    sqlite_settings = Settings(root=tmp_path / "sqlite")
    upgrade_to_head(sqlite_settings)
    try:
        with session_scope(sqlite_settings) as sqlite_session:
            sqlite_concepts = ConceptManager(sqlite_session, sqlite_settings)
            for concept in sorted(concepts.list(), key=lambda c: c.id):
                created = sqlite_concepts.create(
                    concept.name, concept.summary, "paper",
                    aliases=[a.alias for a in concept.aliases], slug=concept.slug,
                )
                assert created.id == concept.id
            sqlite_session.flush()
            compared = 0
            for query in PARITY_QUERIES:
                for kinds in [KINDS, *({kind} for kind in KINDS)]:
                    want = sqlite_concepts.search_hits(query, limit=100, kinds=kinds)
                    got = concepts.search_hits(query, limit=100, kinds=kinds)
                    assert brief(got) == brief(want), (query, kinds)
                    for a, b in zip(got, want, strict=True):
                        assert math.isclose(a.score, b.score, rel_tol=1e-14), (query, a, b)
                    compared += len(want)
            assert compared > 300
            assert sqlite_concepts.check() == [] and concepts.check() == []
    finally:
        dispose_engine(sqlite_settings)
