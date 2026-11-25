"""ConceptRelationManager: direction, symmetric order, one relation per pair, acyclic builds_on,
review state, and the database constraints behind them."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Concept
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    CycleError,
    DuplicateError,
    NotFoundError,
    RelationConflictError,
)
from teacher.managers.concept_relations import validate_rationale
from tests.conftest import FOREIGN_KEY_VIOLATION

WHY = "It needs the other one first."


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def relations(session: Session, settings: Settings) -> ConceptRelationManager:
    return ConceptRelationManager(session, settings)


@pytest.fixture
def abcd(concepts: ConceptManager) -> tuple[Concept, Concept, Concept, Concept]:
    """Concepts a, b, c, d with ids in that order."""
    return tuple(concepts.create(name, f"Concept {name}.", "user") for name in "abcd")  # type: ignore[return-value]


def test_builds_on_keeps_its_direction(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], session: Session
) -> None:
    a, b, *_ = abcd
    relation = relations.add("builds_on", b, a, WHY, "paper")
    session.commit()
    assert (relation.from_id, relation.to_id) == (b.id, a.id)  # b (dependent) -> a (prerequisite)
    assert (relation.low_id, relation.high_id) == (a.id, b.id)
    assert relation.from_concept is b and relation.to_concept is a
    assert relation.reviewed_at is None and relation.origin == "paper"
    assert relation.rationale == WHY


@pytest.mark.parametrize("type", ["different_version_of", "relates_to"])
def test_symmetric_types_are_stored_low_to_high_either_way(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], type: str
) -> None:
    a, b, c, _ = abcd
    first = relations.add(type, c, a, WHY, "model")
    second = relations.add(type, a, b, WHY, "model")
    assert (first.from_id, first.to_id) == (a.id, c.id)
    assert (second.from_id, second.to_id) == (a.id, b.id)


@pytest.mark.parametrize(
    ("args", "error", "fragment"),
    [
        (("depends_on", 0, 1, WHY, "user"), ValueError, "invalid relation type 'depends_on'"),
        (("builds_on", 0, 1, WHY, "llm"), ValueError, "invalid origin 'llm'"),
        (("relates_to", 0, 0, WHY, "user"), ValueError, "'a' cannot be related to itself"),
        (("builds_on", 0, 1, "  ", "user"), ValueError, "rationale must be a non-empty string"),
        (("builds_on", 0, 1, "One.\nTwo.", "user"), ValueError, "one sentence on one line"),
        (("builds_on", 0, 1, "It uses B. It also adds C.", "user"), ValueError, "one sentence"),
    ],
)
def test_invalid_relations_are_refused(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], session: Session,
    args: tuple, error: type, fragment: str,
) -> None:
    type, i, j, why, origin = args
    with pytest.raises(error, match=fragment):
        relations.add(type, abcd[i], abcd[j], why, origin)
    assert relations.list() == []


@pytest.mark.parametrize(
    "rationale",
    [
        "BERT reuses the Transformer encoder (e.g. its attention layers).",
        "  Trailing spaces are stripped.  ",
        "Vaswani et al. propose it.",
        "Is it a question?",
    ],
)
def test_one_sentence_rationales_pass(rationale: str) -> None:
    assert validate_rationale(rationale) == rationale.strip()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("builds_on", "relates_to"),
        ("relates_to", "different_version_of"),
        ("builds_on", "builds_on"),
        ("relates_to", "relates_to"),
    ],
)
def test_at_most_one_relation_per_pair_in_either_order(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], first: str, second: str
) -> None:
    a, b, *_ = abcd
    existing = relations.add(first, a, b, WHY, "user")
    for x, y in ((a, b), (b, a)):
        with pytest.raises(
            RelationConflictError,
            match=rf"'a' and 'b' already have relation #{existing.id} \(a {first} b\)",
        ):
            relations.add(second, x, y, WHY, "user")
    assert relations.list() == [existing]
    assert issubclass(RelationConflictError, DuplicateError)


def test_builds_on_cycles_are_refused_with_the_cycle_listed(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...]
) -> None:
    a, b, c, d = abcd
    relations.add("builds_on", a, b, WHY, "user")
    relations.add("builds_on", b, c, WHY, "user")
    relations.add("relates_to", c, a, WHY, "user")  # symmetric relations do not count
    relations.add("builds_on", c, d, WHY, "user")
    with pytest.raises(CycleError) as info:
        relations.add("builds_on", d, a, WHY, "user")
    assert info.value.cycle == ("a", "b", "c", "d", "a")
    assert "'d' builds_on 'a' would make builds_on cyclic: a -> b -> c -> d -> a" in str(info.value)
    assert len(relations.list(type="builds_on")) == 3
    relations.add("builds_on", a, d, WHY, "user")  # a shortcut is not a cycle


def test_get_list_remove_and_mark_reviewed(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], session: Session
) -> None:
    a, b, c, _ = abcd
    r1 = relations.add("builds_on", a, b, WHY, "user")
    r2 = relations.add("relates_to", b, c, WHY, "user")
    r3 = relations.add("different_version_of", a, c, WHY, "user")
    assert relations.get(r2.id) is r2
    assert relations.list() == [r1, r2, r3]
    assert relations.list(concept=b) == [r1, r2]
    assert relations.list(concept=a, type="different_version_of") == [r3]
    assert relations.between(c, a) is r3 and relations.between(abcd[3], a) is None
    with pytest.raises(ValueError, match="invalid relation type 'x'"):
        relations.list(type="x")
    relations.mark_reviewed(r1)
    assert r1.reviewed_at is not None
    relations.remove(r2)
    session.commit()
    assert relations.list() == [r1, r3]
    with pytest.raises(NotFoundError, match=f"no concept relation with id {r2.id}"):
        relations.get(r2.id)


# --- update -------------------------------------------------------------------------------


def test_update_rationale_clears_review(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...]
) -> None:
    a, b, *_ = abcd
    relation = relations.add("builds_on", a, b, WHY, "user")
    relations.mark_reviewed(relation)
    relations.update(relation, rationale=WHY)  # unchanged: still reviewed
    assert relation.reviewed_at is not None
    relations.update(relation, rationale="A uses B's output.")
    assert relation.rationale == "A uses B's output." and relation.reviewed_at is None
    with pytest.raises(ValueError, match="nothing to update"):
        relations.update(relation)
    with pytest.raises(ValueError, match="one sentence"):
        relations.update(relation, rationale="A uses B. B is old.")
    assert relation.rationale == "A uses B's output."


def test_update_type_canonicalises_and_needs_a_direction_for_builds_on(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...], session: Session
) -> None:
    a, b, *_ = abcd
    relation = relations.add("builds_on", b, a, WHY, "user")
    relations.update(relation, type="relates_to")
    assert (relation.type, relation.from_id, relation.to_id) == ("relates_to", a.id, b.id)
    relations.update(relation, type="different_version_of")
    assert (relation.from_id, relation.to_id) == (a.id, b.id)
    with pytest.raises(ValueError, match="needs from_concept and to_concept"):
        relations.update(relation, type="builds_on")
    relations.update(relation, type="builds_on", from_concept=b, to_concept=a)
    assert (relation.type, relation.from_id, relation.to_id) == ("builds_on", b.id, a.id)
    relations.update(relation, from_concept=a, to_concept=b)  # flip the direction
    session.commit()
    assert (relation.from_id, relation.to_id, relation.low_id, relation.high_id) == (
        a.id, b.id, a.id, b.id,
    )
    assert relation.from_concept is a and relation.to_concept is b


def test_update_revalidates_everything(
    relations: ConceptRelationManager, abcd: tuple[Concept, ...]
) -> None:
    a, b, c, d = abcd
    relations.add("builds_on", a, b, WHY, "user")
    relations.add("builds_on", b, c, WHY, "user")
    loose = relations.add("relates_to", a, c, WHY, "user")
    with pytest.raises(CycleError, match="a -> b -> c -> a") as info:
        relations.update(loose, type="builds_on", from_concept=c, to_concept=a)
    assert info.value.cycle == ("a", "b", "c", "a")
    assert loose.type == "relates_to"  # unchanged
    with pytest.raises(ValueError, match="invalid relation type 'parent_of'"):
        relations.update(loose, type="parent_of")
    with pytest.raises(ValueError, match="remove it and add a new one"):
        relations.update(loose, from_concept=a, to_concept=d)
    with pytest.raises(ValueError, match="both from_concept and to_concept"):
        relations.update(loose, from_concept=a)
    relations.update(loose, type="builds_on", from_concept=a, to_concept=c)  # fine: a shortcut
    assert (loose.type, loose.from_id, loose.to_id) == ("builds_on", a.id, c.id)


# --- database constraints -----------------------------------------------------------------


def _insert(session: Session, type: str, from_id: int, to_id: int, low: int, high: int) -> None:
    session.execute(
        text(
            "INSERT INTO concept_relations (type, from_id, to_id, low_id, high_id, rationale, "
            "origin) VALUES (:t, :f, :to, :l, :h, 'r', 'user')"
        ),
        {"t": type, "f": from_id, "to": to_id, "l": low, "h": high},
    )


@pytest.mark.parametrize(
    ("row", "constraint"),
    [
        (("relates_to", 2, 1, 1, 2), "ck_concept_relations_symmetric_order"),
        (("builds_on", 1, 1, 1, 1), "ck_concept_relations_not_self"),
        (("builds_on", 2, 1, 2, 1), "ck_concept_relations_pair"),
        (("builds_on", 1, 2, 1, 3), "ck_concept_relations_pair"),
        (("parent_of", 1, 2, 1, 2), "ck_concept_relations_type"),
        (("builds_on", 1, 99, 1, 99), FOREIGN_KEY_VIOLATION),
    ],
)
def test_database_checks(
    session: Session, abcd: tuple[Concept, ...], row: tuple, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _insert(session, *row)


# The UNIQUE over (low_id, high_id), as SQLite and Postgres name it.
PAIR_UNIQUE = (
    r"UNIQUE constraint failed: concept_relations\.low_id|uq_concept_relations_low_id_high_id"
)


def test_database_allows_one_relation_per_unordered_pair(
    session: Session, abcd: tuple[Concept, ...]
) -> None:
    _insert(session, "builds_on", 1, 2, 1, 2)
    for row in [("builds_on", 2, 1, 1, 2), ("relates_to", 1, 2, 1, 2)]:
        with pytest.raises(IntegrityError, match=PAIR_UNIQUE):
            with session.begin_nested():
                _insert(session, *row)
    _insert(session, "builds_on", 3, 1, 1, 3)  # another pair is fine
    assert session.scalar(text("SELECT count(*) FROM concept_relations")) == 2


def test_relation_check_origin_constraint(session: Session, abcd: tuple[Concept, ...]) -> None:
    with pytest.raises(IntegrityError, match="ck_concept_relations_origin"), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO concept_relations (type, from_id, to_id, low_id, high_id, rationale, "
                "origin) VALUES ('builds_on', 1, 2, 1, 2, 'r', 'llm')"
            )
        )
    with pytest.raises(IntegrityError, match="ck_concepts_origin"):
        session.add(Concept(slug="z", name="Z", name_norm="z", summary="z", origin="llm"))
        session.flush()


def test_removing_a_concept_removes_its_relations(
    concepts: ConceptManager, relations: ConceptRelationManager, abcd: tuple[Concept, ...]
) -> None:
    a, b, c, _ = abcd
    keep = relations.add("relates_to", a, c, WHY, "user")
    relations.add("builds_on", b, a, WHY, "user")
    relations.add("builds_on", c, b, WHY, "user")
    concepts.delete(b)
    assert relations.list() == [keep]
