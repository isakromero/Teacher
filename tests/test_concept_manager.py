"""ConceptManager: creation, lookups, edits, review state, aliases, deletion and the word index.

The search tiers and the trigram index are tested in test_concept_search.py."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concepts import normalize_name
from teacher.config import Settings
from teacher.db.fts import ConceptSearchUnavailableError
from teacher.db.models import Concept, ConceptAlias, ConceptEmbedding, ConceptRelation
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    DuplicateError,
    IntegrityCheckError,
    Issue,
    NotFoundError,
)
from teacher.managers.concepts import search_expression

SUMMARY = "A mechanism that weighs every token against every other token."


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def relations(session: Session, settings: Settings) -> ConceptRelationManager:
    return ConceptRelationManager(session, settings)


def fts_rows(session: Session) -> dict[int, tuple[str, str, str]]:
    rows = session.execute(text("SELECT rowid, name, aliases, summary FROM concept_search"))
    return {rowid: (name, aliases, summary) for rowid, name, aliases, summary in rows}


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- normalisation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Self-Attention", "self-attention"),
        ("  multi \t head\n  attention ", "multi head attention"),
        ("ＧＡＮ", "gan"),  # full-width letters (NFKC)
        ("ﬁne-tuning", "fine-tuning"),  # ligature (NFKC)
        ("Straße", "strasse"),  # casefold, not lower
        ("a b", "a b"),  # no-break space
        ("ε-greedy", "ε-greedy"),
        ("Ε", "ε"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_normalize_name_rejects_empty_and_non_strings() -> None:
    with pytest.raises(ValueError, match="empty after normalisation"):
        normalize_name(" \t \n")
    with pytest.raises(ValueError, match="alias '' is empty"):
        normalize_name("", what="alias")
    with pytest.raises(TypeError, match="must be a string"):
        normalize_name(None)  # type: ignore[arg-type]


# --- creation -----------------------------------------------------------------------------


def test_create_stores_an_unreviewed_concept_and_its_search_row(
    concepts: ConceptManager, session: Session
) -> None:
    concept = concepts.create(
        "  Self-Attention (Transformers) ", SUMMARY, "paper", aliases=[" SA ", "intra-attention"]
    )
    session.commit()
    assert concept.slug == "self-attention-transformers"
    assert concept.name == "Self-Attention (Transformers)"
    assert concept.name_norm == "self-attention (transformers)"
    assert concept.summary == SUMMARY and concept.origin == "paper"
    assert concept.reviewed_at is None
    assert isinstance(concept.created_at, datetime) and concept.created_at.tzinfo is None
    assert [(a.alias, a.alias_norm) for a in concept.aliases] == [
        ("intra-attention", "intra-attention"),
        ("SA", "sa"),
    ]
    assert fts_rows(session) == {
        concept.id: ("Self-Attention (Transformers)", "intra-attention\nSA", SUMMARY)
    }
    assert concepts.check() == []


def test_create_with_explicit_slug(concepts: ConceptManager) -> None:
    concept = concepts.create("Attention", SUMMARY, "user", slug="attention-2017")
    assert concept.slug == "attention-2017"
    with pytest.raises(ValueError, match="invalid slug 'Bad Slug'"):
        concepts.create("Attention", SUMMARY, "user", slug="Bad Slug")


def test_slug_collision_is_an_error_not_a_suffix(
    concepts: ConceptManager, session: Session
) -> None:
    concepts.create("Attention", SUMMARY, "paper")
    with pytest.raises(DuplicateError, match=r"slug 'attention' already exists \(concept 'Attention'\)"):
        concepts.create("attention!", "Another field's attention.", "model")
    assert count(session, Concept) == 1 and len(fts_rows(session)) == 1
    assert concepts.create("attention!", "Other.", "model", slug="attention-psych").slug == (
        "attention-psych"
    )


@pytest.mark.parametrize(
    ("kwargs", "error", "fragment"),
    [
        ({"name": " "}, ValueError, "name must be a non-empty string"),
        ({"summary": ""}, ValueError, "summary must be a non-empty string"),
        ({"origin": "llm"}, ValueError, "invalid origin 'llm'"),
        ({"aliases": ["  SELF  attention "]}, ValueError, "is the concept's own name"),
        ({"aliases": ["SA", "sa"]}, DuplicateError, "alias 'sa' repeats alias 'SA'"),
        ({"aliases": ["ok", "  "]}, ValueError, "alias '  ' is empty after normalisation"),
        ({"aliases": "SA"}, TypeError, "not a string"),
        ({"aliases": [3]}, TypeError, "alias must be a string"),
        ({"name": "!!!"}, ValueError, "cannot derive a slug"),
    ],
)
def test_invalid_create_changes_nothing(
    concepts: ConceptManager, session: Session, kwargs: dict, error: type, fragment: str
) -> None:
    args = {"name": "Self Attention", "summary": SUMMARY, "origin": "paper"} | kwargs
    with pytest.raises(error, match=fragment):
        concepts.create(args.pop("name"), args.pop("summary"), args.pop("origin"), **args)
    assert count(session, Concept) == 0 and count(session, ConceptAlias) == 0
    assert fts_rows(session) == {}


# --- lookups ------------------------------------------------------------------------------


def test_get_get_by_slug_and_list(concepts: ConceptManager) -> None:
    b = concepts.create("Beta", "B.", "user")
    a = concepts.create("Alpha", "A.", "user")
    assert concepts.get(b.id) is b and concepts.get_by_slug("alpha") is a
    assert concepts.list() == [a, b]
    with pytest.raises(NotFoundError, match="no concept with id 999"):
        concepts.get(999)
    with pytest.raises(NotFoundError, match="no concept with slug 'gamma'"):
        concepts.get_by_slug("gamma")


def test_find_by_name_matches_names_and_aliases_exactly(concepts: ConceptManager) -> None:
    gan = concepts.create("Generative adversarial network", "Two nets.", "paper", aliases=["GAN"])
    gan_ml = concepts.create("Gated attention network", "Gates.", "paper", aliases=["GAN"])
    named = concepts.create("GAN", "A concept literally named GAN.", "user", slug="gan-name")
    assert concepts.find_by_name("  gan ") == [named, gan_ml, gan]  # by slug
    assert concepts.find_by_name("GENERATIVE   adversarial network") == [gan]
    assert concepts.find_by_name("adversarial") == []  # exact, not a substring search
    with pytest.raises(ValueError, match="empty after normalisation"):
        concepts.find_by_name("   ")


# --- edits and review ---------------------------------------------------------------------


def test_mark_reviewed_and_every_content_edit_clears_it(
    concepts: ConceptManager, session: Session
) -> None:
    concept = concepts.create("Attention", SUMMARY, "model", aliases=["attn"])
    edits = [
        lambda: concepts.update(concept, name="Attention mechanism"),
        lambda: concepts.update(concept, summary="Weights over inputs."),
        lambda: concepts.add_alias(concept, "attention layer"),
        lambda: concepts.remove_alias(concept, "ATTN"),
    ]
    for edit in edits:
        concepts.mark_reviewed(concept)
        reviewed = concept.reviewed_at
        assert reviewed is not None
        before = concept.updated_at
        edit()
        assert concept.reviewed_at is None
        assert concept.updated_at >= before
        session.commit()
    assert concepts.check() == []


def test_update_changes_name_summary_and_search_row_but_never_the_slug(
    concepts: ConceptManager, session: Session
) -> None:
    concept = concepts.create("Attention", SUMMARY, "paper")
    concepts.update(concept, name="Scaled dot-product attention", summary="Softmax of QK^T.")
    session.commit()
    assert concept.slug == "attention"
    assert concept.name_norm == "scaled dot-product attention"
    assert fts_rows(session)[concept.id] == ("Scaled dot-product attention", "", "Softmax of QK^T.")
    assert concepts.search("scaled") == [concept]
    assert concepts.search("weighs") == []  # the old summary is gone from the index


def test_update_with_current_values_keeps_review_and_nothing_is_an_error(
    concepts: ConceptManager,
) -> None:
    concept = concepts.create("Attention", SUMMARY, "paper")
    concepts.mark_reviewed(concept)
    concepts.update(concept, name="Attention", summary=SUMMARY)
    assert concept.reviewed_at is not None
    with pytest.raises(ValueError, match="nothing to update"):
        concepts.update(concept)
    with pytest.raises(ValueError, match="summary must be a non-empty string"):
        concepts.update(concept, summary=" ")


def test_update_name_to_an_alias_is_refused(concepts: ConceptManager) -> None:
    concept = concepts.create("Generative adversarial network", "Two nets.", "paper", aliases=["GAN"])
    with pytest.raises(ValueError, match="equals its alias 'GAN'; remove the alias first"):
        concepts.update(concept, name=" gan ")
    assert concept.name == "Generative adversarial network"


def test_slug_is_immutable_in_the_database(concepts: ConceptManager, session: Session) -> None:
    concept = concepts.create("Attention", SUMMARY, "paper")
    session.commit()
    with pytest.raises(IntegrityError, match="concepts.slug is immutable"):
        session.execute(text("UPDATE concepts SET slug = 'other' WHERE id = :i"), {"i": concept.id})
    session.rollback()
    session.execute(text("UPDATE concepts SET name = 'Renamed' WHERE id = :i"), {"i": concept.id})


def test_aliases_add_and_remove(concepts: ConceptManager, session: Session) -> None:
    concept = concepts.create("Generative adversarial network", "Two nets.", "paper")
    other = concepts.create("Gated attention network", "Gates.", "paper")
    row = concepts.add_alias(concept, "  GAN ")
    concepts.add_alias(other, "gan")  # aliases may repeat across concepts
    assert (row.alias, row.alias_norm) == ("GAN", "gan")
    with pytest.raises(DuplicateError, match="already has alias 'GAN'"):
        concepts.add_alias(concept, "ｇａｎ")
    with pytest.raises(ValueError, match="own name"):
        concepts.add_alias(concept, "generative  ADVERSARIAL network")
    session.commit()
    assert fts_rows(session)[concept.id][1] == "GAN"

    concepts.remove_alias(concept, " Gan ")
    session.commit()
    assert concept.aliases == [] and [a.alias for a in other.aliases] == ["gan"]
    assert fts_rows(session)[concept.id][1] == ""
    with pytest.raises(NotFoundError, match="has no alias 'GAN'"):
        concepts.remove_alias(concept, "GAN")
    assert concepts.check() == []


# --- deletion -----------------------------------------------------------------------------


def test_delete_removes_aliases_relations_embedding_and_search_row(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    keep = concepts.create("Softmax", "Exponentiate and normalise.", "paper")
    gone = concepts.create("Attention", SUMMARY, "paper", aliases=["attn"])
    relations.add("builds_on", gone, keep, "Attention weights are a softmax.", "paper")
    gone.embedding = ConceptEmbedding(model="m", dimensions=1, vector=b"\x00\x00\x80?", text_hash="h")
    session.commit()

    concepts.delete(gone)
    session.commit()
    assert concepts.list() == [keep]
    for model in (ConceptAlias, ConceptRelation, ConceptEmbedding):
        assert count(session, model) == 0, model
    assert list(fts_rows(session)) == [keep.id]
    assert concepts.check() == []


def test_database_cascades_without_the_orm(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    a = concepts.create("A", "a.", "user", aliases=["x"])
    b = concepts.create("B", "b.", "user")
    relations.add("relates_to", a, b, "They are related.", "user")
    a.embedding = ConceptEmbedding(model="m", dimensions=1, vector=b"\x00\x00\x80?", text_hash="h")
    session.commit()
    session.execute(text("DELETE FROM concepts WHERE id = :i"), {"i": a.id})
    for table in ("concept_aliases", "concept_relations", "concept_embeddings"):
        assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table


# --- full-text search ---------------------------------------------------------------------


@pytest.fixture
def library(concepts: ConceptManager) -> dict[str, Concept]:
    return {
        "attn": concepts.create(
            "Self-attention", "Each token attends to all others AND itself.", "paper",
            aliases=["intra-attention"],
        ),
        "eps": concepts.create(
            "ε-greedy exploration", "Pick a random action with probability ε.", "paper"
        ),
        "cafe": concepts.create("Café effect", 'The so-called "café" effect.', "user"),
        "soft": concepts.create(
            "Softmax", "Turns scores into probabilities, used in self-attention.", "paper"
        ),
    }


def test_search_ranks_name_matches_above_summary_matches(
    concepts: ConceptManager, library: dict[str, Concept]
) -> None:
    assert concepts.search("self-attention") == [library["attn"], library["soft"]]
    assert concepts.search("softmax") == [library["soft"]]
    assert concepts.search("intra") == [library["attn"]]  # a token of the alias
    assert concepts.search("intr") == [library["attn"]]  # a prefix of a word of the alias
    assert concepts.search("intr", kinds={"exact", "word"}) == []  # whole words only
    assert concepts.search("intra-attention") == [library["attn"]]  # via the alias
    assert concepts.search("self-attention", limit=1) == [library["attn"]]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ('"café', ["cafe"]),
        ('café"', ["cafe"]),
        ('"so-called "café""', ["cafe"]),
        ("cafe", ["cafe"]),  # diacritics folded
        ("self*", ["attn", "soft"]),  # '*' is not a prefix operator
        ("AND", ["attn"]),  # a word, not an operator
        ("attends AND", ["attn"]),
        ("OR softmax", []),  # 'or' is just a word that nothing contains
        ("NOT softmax", []),
        ("NEAR(self attention)", []),  # the phrase "near self" AND "attention": no syntax error
        ("name:softmax", []),  # no column filters
        (": probabilities", ["soft"]),
        ("ε", ["eps"]),
        ("Ε", ["eps"]),
        ("-softmax", ["soft"]),
        ("^softmax", ["soft"]),
        ("(softmax", ["soft"]),
        ("softmax ***", ["soft"]),  # terms without letters or digits are ignored
    ],
)
def test_search_quotes_query_syntax(
    concepts: ConceptManager, library: dict[str, Concept], query: str, expected: list[str]
) -> None:
    assert {c.id for c in concepts.search(query)} == {library[k].id for k in expected}


@pytest.mark.parametrize(
    ("query", "error", "fragment"),
    [
        ("", ValueError, "must not be empty"),
        ("   \n", ValueError, "must not be empty"),
        ("*** \"\" -", ValueError, "has no letters or digits"),
        (None, TypeError, "must be a string"),
    ],
)
def test_search_rejects_empty_queries(
    concepts: ConceptManager, query: object, error: type, fragment: str
) -> None:
    with pytest.raises(error, match=fragment):
        concepts.search(query)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, True, 2.5])
def test_search_rejects_bad_limits(concepts: ConceptManager, limit: object) -> None:
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        concepts.search("x", limit=limit)  # type: ignore[arg-type]


def test_search_expression_quotes_every_term() -> None:
    assert search_expression('a"b NEAR(x *') == '"a""b" "NEAR(x"'


def test_index_follows_updates_alias_changes_and_deletes(
    concepts: ConceptManager, library: dict[str, Concept], session: Session
) -> None:
    attn, soft = library["attn"], library["soft"]
    concepts.add_alias(soft, "normalised exponential")
    assert concepts.search("exponential") == [soft]
    concepts.remove_alias(attn, "intra-attention")
    assert concepts.search("intra-attention") == []
    concepts.update(soft, summary="Turns scores into probabilities.")
    assert concepts.search("self-attention") == [attn]
    concepts.delete(attn)
    assert concepts.search("attention") == []
    session.commit()
    assert concepts.check() == []


def test_check_reports_and_rebuild_repairs_index_drift(
    concepts: ConceptManager, library: dict[str, Concept], session: Session
) -> None:
    attn, soft = library["attn"], library["soft"]
    session.execute(text("DELETE FROM concept_search WHERE rowid = :i"), {"i": attn.id})
    session.execute(text("UPDATE concept_search SET name = 'x' WHERE rowid = :i"), {"i": soft.id})
    session.execute(
        text("INSERT INTO concept_search (rowid, name, aliases, summary) VALUES (99, 'ghost', '', 'boo')")
    )
    assert concepts.check() == [
        Issue("missing_search_row", "self-attention", "no concept_search row"),
        Issue("stale_search_row", "softmax", "concept_search row differs from the concept"),
        Issue("orphan_search_row", "#99", "concept_search row 99 has no concept"),
    ]
    with pytest.raises(IntegrityCheckError, match=r"rows for concepts that do not exist \(ids \[99\]\)"):
        concepts.search("ghost")
    assert concepts.rebuild_search_index() == 4
    assert concepts.check() == []
    assert concepts.search("ghost") == []


def test_missing_fts_table_is_a_clear_error_and_rolls_back(
    concepts: ConceptManager, session: Session
) -> None:
    session.execute(text("DROP TABLE concept_search"))
    with pytest.raises(ConceptSearchUnavailableError, match="concept_search full-text table is missing"):
        concepts.create("Attention", SUMMARY, "paper")
    assert count(session, Concept) == 0  # the savepoint was rolled back with the failed sync
    with pytest.raises(ConceptSearchUnavailableError, match="upgrade_to_head"):
        concepts.search("attention")
