"""ConceptManager.merge: aliases, retargeting, every conflict rule, cycles, and all-or-nothing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Concept, ConceptEmbedding, ConceptRelation, utcnow
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    CycleError,
    DroppedRelation,
    NotFoundError,
)

WHY = "They are connected."


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def relations(session: Session, settings: Settings) -> ConceptRelationManager:
    return ConceptRelationManager(session, settings)


def make(concepts: ConceptManager, slug: str, **kw: Any) -> Concept:
    return concepts.create(kw.pop("name", slug.upper()), f"About {slug}.", "user", slug=slug, **kw)


def snapshot(session: Session) -> dict[str, list[Any]]:
    """Every concept-graph row, read with raw SQL."""
    queries = {
        "concepts": "SELECT id, slug, name, name_norm, summary, reviewed_at, updated_at FROM concepts",
        "aliases": "SELECT id, concept_id, alias, alias_norm FROM concept_aliases",
        "relations": "SELECT id, type, from_id, to_id, low_id, high_id, rationale, reviewed_at "
        "FROM concept_relations",
        "embeddings": "SELECT concept_id, model, text_hash FROM concept_embeddings",
        "fts": "SELECT rowid, name, aliases, summary FROM concept_search",
    }
    return {key: sorted(session.execute(text(sql)).all()) for key, sql in queries.items()}


def test_merge_moves_aliases_and_deletes_the_source(
    concepts: ConceptManager, session: Session
) -> None:
    source = concepts.create(
        "Scaled attention", "Attention scaled by sqrt(d).", "paper",
        aliases=["attention", "attn", "SDPA"],
    )
    into = concepts.create("Attention", "Weights over inputs.", "user", aliases=["ATTN"])
    source.embedding = ConceptEmbedding(model="m", dimensions=1, vector=b"\x00\x00\x80?", text_hash="h")
    concepts.mark_reviewed(into)
    reviewed = into.reviewed_at
    session.commit()

    result = concepts.merge(source, into)
    session.commit()

    assert result.into is into and result.source_slug == "scaled-attention"
    assert result.moved_aliases == ("Scaled attention", "SDPA")
    assert result.skipped_aliases == ("attention", "attn")
    assert result.retargeted == () and result.dropped == ()
    assert [a.alias for a in into.aliases] == ["ATTN", "Scaled attention", "SDPA"]
    assert into.reviewed_at == reviewed  # a merge is not an edit of the summary
    assert into.summary == "Weights over inputs."
    with pytest.raises(NotFoundError):
        concepts.get_by_slug("scaled-attention")
    assert session.scalar(text("SELECT count(*) FROM concept_embeddings")) == 0
    assert concepts.search("SDPA") == [into] and concepts.search("scaled") == [into]
    assert concepts.find_by_name("scaled attention") == [into]
    assert concepts.check() == []


def test_source_name_equal_to_an_alias_of_into_is_not_added(concepts: ConceptManager) -> None:
    source = concepts.create("GAN", "Short name.", "paper", slug="gan-short")
    into = concepts.create("Generative adversarial network", "Two nets.", "paper", aliases=["gan"])
    result = concepts.merge(source, into)
    assert result.moved_aliases == () and result.skipped_aliases == ("GAN",)
    assert [a.alias for a in into.aliases] == ["gan"]


def test_merge_retargets_relations_in_both_directions(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    source = make(concepts, "s")
    z = make(concepts, "z")  # id between source and into
    into = make(concepts, "i")
    x, y = make(concepts, "x"), make(concepts, "y")
    prereq = relations.add("builds_on", source, x, WHY, "paper")
    dependent = relations.add("builds_on", y, source, WHY, "paper")
    symmetric = relations.add("relates_to", source, z, WHY, "paper")
    relations.mark_reviewed(prereq)
    assert (symmetric.from_id, symmetric.to_id) == (source.id, z.id)
    session.commit()

    result = concepts.merge(source, into)
    session.commit()

    assert (prereq.from_id, prereq.to_id) == (into.id, x.id)
    assert prereq.from_concept is into and prereq.reviewed_at is not None  # same claim, kept
    assert (dependent.from_id, dependent.to_id) == (y.id, into.id)
    assert (symmetric.from_id, symmetric.to_id) == (z.id, into.id)  # re-ordered low -> high
    assert (symmetric.low_id, symmetric.high_id) == (z.id, into.id)
    assert [(r.relation, r.old_from_slug, r.old_to_slug) for r in result.retargeted] == [
        (prereq, "s", "x"), (dependent, "y", "s"), (symmetric, "s", "z"),
    ]
    assert relations.list(concept=into) == [prereq, dependent, symmetric]


@pytest.mark.parametrize("type", ["relates_to", "builds_on", "different_version_of"])
def test_a_relation_between_the_two_is_dropped(
    concepts: ConceptManager, relations: ConceptRelationManager, type: str
) -> None:
    source, into = make(concepts, "s"), make(concepts, "i")
    relation = relations.add(type, source, into, WHY, "model")
    relation_id = relation.id
    result = concepts.merge(source, into)
    assert result.dropped == (
        DroppedRelation(
            id=relation_id, type=type, from_slug="s", to_slug="i", rationale=WHY,
            code="self_relation",
            reason="it related 's' and 'i', which are now one concept",
            kept_id=None,
        ),
    )
    assert relations.list() == []


# (source's relation to x, into's relation to x, who is reviewed, winner, why)
# A builds_on direction "out" means the concept builds on x; "in" means x builds on it.
CONFLICTS = [
    (("builds_on", "out"), ("relates_to", None), (), "source", "builds_on beats relates_to"),
    (("relates_to", None), ("builds_on", "out"), (), "into", "builds_on beats relates_to"),
    (("builds_on", "in"), ("different_version_of", None), ("into",), "source",
     "builds_on beats different_version_of"),
    (("different_version_of", None), ("relates_to", None), ("into",), "source",
     "different_version_of beats relates_to"),
    (("relates_to", None), ("different_version_of", None), ("source",), "into",
     "different_version_of beats relates_to"),
    (("relates_to", None), ("relates_to", None), (), "into", "the merge target's own relation"),
    (("relates_to", None), ("relates_to", None), ("source",), "source", "the other one is reviewed"),
    (("relates_to", None), ("relates_to", None), ("into",), "into", "the other one is reviewed"),
    (("relates_to", None), ("relates_to", None), ("source", "into"), "into",
     "the merge target's own relation"),
    (("builds_on", "out"), ("builds_on", "out"), (), "into", "the merge target's own relation"),
    (("builds_on", "in"), ("builds_on", "in"), ("source",), "source", "the other one is reviewed"),
    (("different_version_of", None), ("different_version_of", None), ("into",), "into",
     "the other one is reviewed"),
]


@pytest.mark.parametrize(("src", "dst", "reviewed", "winner", "why"), CONFLICTS)
def test_conflicting_relations_on_one_pair(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session,
    src: tuple[str, str | None], dst: tuple[str, str | None], reviewed: tuple[str, ...],
    winner: str, why: str,
) -> None:
    source, into, x = make(concepts, "s"), make(concepts, "i"), make(concepts, "x")

    def relate(concept: Concept, spec: tuple[str, str | None]) -> ConceptRelation:
        type, direction = spec
        a, b = (x, concept) if direction == "in" else (concept, x)
        return relations.add(type, a, b, f"{concept.slug} and x.", "paper")

    by = {"source": relate(source, src), "into": relate(into, dst)}
    for who in reviewed:
        relations.mark_reviewed(by[who])
    kept, lost = by[winner], by["into" if winner == "source" else "source"]
    kept_id, lost_id, lost_type = kept.id, lost.id, lost.type
    session.commit()

    result = concepts.merge(source, into)
    session.commit()

    survivor = relations.between(into, x)
    assert survivor is not None and survivor.id == kept_id
    assert relations.list() == [survivor]
    expected_type, direction = src if winner == "source" else dst
    expected = (x.id, into.id) if direction == "in" else (into.id, x.id)
    if expected_type != "builds_on":
        expected = tuple(sorted(expected))
    assert (survivor.type, survivor.from_id, survivor.to_id) == (expected_type, *expected)
    assert survivor.reviewed_at is not None if winner in reviewed else survivor.reviewed_at is None
    [dropped] = result.dropped
    assert (dropped.id, dropped.type, dropped.code, dropped.kept_id) == (
        lost_id, lost_type, "conflict", kept_id,
    )
    assert "'x' was related to both concepts" in dropped.reason and why in dropped.reason
    assert [r.relation.id for r in result.retargeted] == ([kept_id] if winner == "source" else [])


def test_opposite_builds_on_edges_refuse_the_merge_and_change_nothing(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    source = make(concepts, "s", aliases=["S1"])
    into, x = make(concepts, "i"), make(concepts, "x")
    relations.add("builds_on", source, x, WHY, "paper")  # s builds on x
    relations.add("builds_on", x, into, WHY, "paper")  # x builds on i
    session.commit()
    before = snapshot(session)

    with pytest.raises(CycleError, match=r"merging 's' into 'i' would make builds_on cyclic: x -> i -> x") as info:
        concepts.merge(source, into)

    assert info.value.cycle == ("x", "i", "x")
    assert snapshot(session) == before
    session.rollback()
    assert snapshot(session) == before


def test_a_longer_cycle_through_retargeting_refuses_the_merge(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session
) -> None:
    source, into = make(concepts, "s"), make(concepts, "i")
    x, y = make(concepts, "x"), make(concepts, "y")
    relations.add("builds_on", into, x, WHY, "paper")
    relations.add("builds_on", x, y, WHY, "paper")
    relations.add("builds_on", y, source, WHY, "paper")  # after the merge: y builds on i
    relations.add("relates_to", source, x, WHY, "paper")  # a conflict that would be resolved
    session.commit()
    before = snapshot(session)

    with pytest.raises(CycleError, match="merging 's' into 'i' would make builds_on cyclic") as info:
        concepts.merge(source, into)

    assert info.value.cycle == ("i", "x", "y", "i")
    assert snapshot(session) == before


def test_merge_into_itself_is_an_error(concepts: ConceptManager) -> None:
    concept = make(concepts, "s")
    with pytest.raises(ValueError, match="cannot merge concept 's' into itself"):
        concepts.merge(concept, concept)


def test_failure_while_applying_rolls_back_everything(
    concepts: ConceptManager, relations: ConceptRelationManager, session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make(concepts, "s", aliases=["S1"])
    into, x = make(concepts, "i"), make(concepts, "x")
    relations.add("builds_on", source, x, WHY, "paper")
    relations.add("relates_to", into, x, WHY, "paper")
    session.commit()
    before = snapshot(session)

    def broken(_concept: Concept) -> None:
        raise RuntimeError("index write failed")

    monkeypatch.setattr(concepts, "_sync_search", broken)
    with pytest.raises(RuntimeError, match="index write failed"):
        concepts.merge(source, into)
    session.expire_all()
    assert snapshot(session) == before


def test_merged_concept_keeps_review_but_relations_moved_keep_their_own(
    concepts: ConceptManager, relations: ConceptRelationManager
) -> None:
    source, into, x = make(concepts, "s"), make(concepts, "i"), make(concepts, "x")
    relation = relations.add("builds_on", x, source, WHY, "paper")
    into.reviewed_at = utcnow()
    concepts.merge(source, into)
    assert into.reviewed_at is not None and relation.reviewed_at is None
