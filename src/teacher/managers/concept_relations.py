"""ConceptRelationManager: typed edges between concepts, with the graph's invariants.

Types are `builds_on` (directed: from = the dependent, to = the prerequisite) and the symmetric
`different_version_of` and `relates_to`, which are stored once with from_id < to_id; the write
path puts a symmetric pair in that order whichever way round it is given. Two concepts have at
most one relation of any type (RelationConflictError names the existing one; the database also
enforces it), and `builds_on` must stay acyclic (CycleError lists the cycle as slugs; SQLite
cannot check this). Any edit clears `reviewed_at`; `mark_reviewed` is the only way to set it.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Iterable, Mapping

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.concepts import (
    BUILDS_ON,
    SYMMETRIC_RELATION_TYPES,
    find_cycle,
    format_cycle,
    validate_origin,
    validate_relation_type,
)
from teacher.config import Settings
from teacher.db.models import Concept, ConceptRelation, utcnow
from teacher.managers.artifacts import _is_unique_violation, _require_persistent
from teacher.managers.errors import CycleError, NotFoundError, RelationConflictError

# A sentence end followed by the start of another sentence ("... works. It also ...").
_SECOND_SENTENCE = re.compile(r"[.!?][\"')\]]*\s+[\"'(\[]*[A-Z]")


def validate_rationale(rationale: object) -> str:
    """`rationale` stripped; it must be one non-empty sentence on one line (ValueError)."""
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("rationale must be a non-empty string")
    stripped = rationale.strip()
    if "\n" in stripped or "\r" in stripped:
        raise ValueError(f"rationale must be one sentence on one line, got {stripped!r}")
    if _SECOND_SENTENCE.search(stripped):
        raise ValueError(f"rationale must be one sentence, got {stripped!r}")
    return stripped


def canonical_endpoints(type: str, from_id: int, to_id: int) -> tuple[int, int]:
    """`(from_id, to_id)` as stored: unchanged for builds_on, ascending for symmetric types."""
    if type in SYMMETRIC_RELATION_TYPES and from_id > to_id:
        return to_id, from_id
    return from_id, to_id


def set_endpoints(relation: ConceptRelation, from_id: int, to_id: int) -> None:
    """Point `relation` at `from_id`/`to_id` (already canonical), keeping low/high in step."""
    relation.from_id, relation.to_id = from_id, to_id
    relation.low_id, relation.high_id = min(from_id, to_id), max(from_id, to_id)


def slugs_by_id(session: Session, ids: Iterable[int]) -> dict[int, str]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = session.execute(select(Concept.id, Concept.slug).where(Concept.id.in_(wanted)))
    return dict(rows.all())


def describe(relation: ConceptRelation, slugs: Mapping[int, str]) -> str:
    """`#3 (attention builds_on softmax)`."""
    return (
        f"#{relation.id} ({slugs[relation.from_id]} {relation.type} {slugs[relation.to_id]})"
    )


def builds_on_edges(
    session: Session, *, exclude: Iterable[int] = ()
) -> dict[int, set[int]]:
    """All stored builds_on edges as `from_id -> {to_id, ...}`, without relations in `exclude`."""
    skip = set(exclude)
    edges: dict[int, set[int]] = {}
    rows = session.execute(
        select(ConceptRelation.id, ConceptRelation.from_id, ConceptRelation.to_id).where(
            ConceptRelation.type == BUILDS_ON
        )
    )
    for relation_id, from_id, to_id in rows:
        if relation_id not in skip:
            edges.setdefault(from_id, set()).add(to_id)
    return edges


def raise_if_cyclic(session: Session, edges: Mapping[int, Iterable[int]], what: str) -> None:
    """CycleError("<what> would make builds_on cyclic: a -> b -> a") if `edges` has a cycle."""
    cycle = find_cycle(edges)
    if cycle is None:
        return
    slugs = slugs_by_id(session, cycle)
    names = tuple(slugs[node] for node in cycle)
    raise CycleError(f"{what} would make builds_on cyclic: {format_cycle(names)}", names)


class ConceptRelationManager:
    """Add, edit, review, remove and list relations between concepts."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- lookups ----------------------------------------------------------------------------

    def get(self, id: int) -> ConceptRelation:
        """The relation with `id`; NotFoundError if there is none."""
        relation = self.session.get(ConceptRelation, id)
        if relation is None:
            raise NotFoundError(f"no concept relation with id {id}")
        return relation

    def between(self, a: Concept, b: Concept) -> ConceptRelation | None:
        """The relation between `a` and `b` (either direction, any type), or None."""
        low, high = min(a.id, b.id), max(a.id, b.id)
        return self.session.scalar(
            select(ConceptRelation).where(
                ConceptRelation.low_id == low, ConceptRelation.high_id == high
            )
        )

    def list(
        self, concept: Concept | None = None, type: str | None = None
    ) -> builtins.list[ConceptRelation]:
        """Relations in id order, optionally only those touching `concept` and/or of `type`."""
        stmt = select(ConceptRelation)
        if concept is not None:
            _require_persistent(concept, "concept")
            stmt = stmt.where(
                or_(ConceptRelation.from_id == concept.id, ConceptRelation.to_id == concept.id)
            )
        if type is not None:
            stmt = stmt.where(ConceptRelation.type == validate_relation_type(type))
        return list(self.session.scalars(stmt.order_by(ConceptRelation.id)))

    # -- changes ----------------------------------------------------------------------------

    def add(
        self,
        type: str,
        from_concept: Concept,
        to_concept: Concept,
        rationale: str,
        origin: str,
    ) -> ConceptRelation:
        """Relate two concepts. For builds_on, `from_concept` builds on `to_concept`; symmetric
        types may be given in either order.

        ValueError for an unknown type or origin, a bad rationale or a concept related to
        itself; RelationConflictError if the pair already has a relation; CycleError if a
        builds_on edge would close a cycle. Nothing changes on error. Starts unreviewed.
        """
        validate_relation_type(type)
        validate_origin(origin)
        rationale = validate_rationale(rationale)
        _require_persistent(from_concept, "from_concept")
        _require_persistent(to_concept, "to_concept")
        if from_concept.id == to_concept.id:
            raise ValueError(f"concept {from_concept.slug!r} cannot be related to itself")
        existing = self.between(from_concept, to_concept)
        if existing is not None:
            self._raise_conflict(existing)
        from_id, to_id = canonical_endpoints(type, from_concept.id, to_concept.id)
        if type == BUILDS_ON:
            edges = builds_on_edges(self.session)
            edges.setdefault(from_id, set()).add(to_id)
            raise_if_cyclic(
                self.session,
                edges,
                f"{from_concept.slug!r} builds_on {to_concept.slug!r}",
            )
        relation = ConceptRelation(type=type, rationale=rationale, origin=origin)
        set_endpoints(relation, from_id, to_id)
        try:
            with self.session.begin_nested():
                self.session.add(relation)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise RelationConflictError(
                    f"concepts {from_concept.slug!r} and {to_concept.slug!r} already have a "
                    "relation"
                ) from err
            raise
        return relation

    def update(
        self,
        relation: ConceptRelation,
        *,
        rationale: str | None = None,
        type: str | None = None,
        from_concept: Concept | None = None,
        to_concept: Concept | None = None,
    ) -> ConceptRelation:
        """Change the rationale and/or type (and, with `from_concept` and `to_concept` -- the
        same two concepts -- the direction of a builds_on edge).

        Turning a symmetric relation into builds_on needs `from_concept`/`to_concept` to say
        which concept builds on which. All invariants are checked again; an actual change clears
        `reviewed_at` (passing the current values changes nothing). ValueError if nothing is
        given; CycleError for a cycle. Nothing changes on error.
        """
        _require_persistent(relation, "relation")
        if rationale is None and type is None and from_concept is None and to_concept is None:
            raise ValueError("nothing to update: give rationale, type or from/to concepts")
        new_type = validate_relation_type(type) if type is not None else relation.type
        new_rationale = (
            validate_rationale(rationale) if rationale is not None else relation.rationale
        )
        slugs = slugs_by_id(self.session, (relation.from_id, relation.to_id))

        if (from_concept is None) != (to_concept is None):
            raise ValueError("give both from_concept and to_concept, or neither")
        if from_concept is not None and to_concept is not None:
            _require_persistent(from_concept, "from_concept")
            _require_persistent(to_concept, "to_concept")
            if {from_concept.id, to_concept.id} != {relation.from_id, relation.to_id} or (
                from_concept.id == to_concept.id
            ):
                raise ValueError(
                    f"relation {describe(relation, slugs)} is between "
                    f"{slugs[relation.from_id]!r} and {slugs[relation.to_id]!r}; to relate other "
                    "concepts, remove it and add a new one"
                )
            new_from, new_to = from_concept.id, to_concept.id
        elif new_type == BUILDS_ON and relation.type != BUILDS_ON:
            raise ValueError(
                f"changing relation {describe(relation, slugs)} to builds_on needs from_concept "
                "and to_concept, to say which concept builds on which"
            )
        else:
            new_from, new_to = relation.from_id, relation.to_id
        new_from, new_to = canonical_endpoints(new_type, new_from, new_to)

        if (new_type, new_from, new_to, new_rationale) == (
            relation.type, relation.from_id, relation.to_id, relation.rationale,
        ):
            return relation
        if new_type == BUILDS_ON:
            edges = builds_on_edges(self.session, exclude=[relation.id])
            edges.setdefault(new_from, set()).add(new_to)
            raise_if_cyclic(
                self.session,
                edges,
                f"changing relation #{relation.id} to {slugs[new_from]!r} builds_on "
                f"{slugs[new_to]!r}",
            )
        with self.session.begin_nested():
            relation.type = new_type
            relation.rationale = new_rationale
            set_endpoints(relation, new_from, new_to)
            relation.reviewed_at = None
            self.session.flush()
        self.session.expire(relation, ["from_concept", "to_concept"])
        return relation

    def mark_reviewed(self, relation: ConceptRelation) -> ConceptRelation:
        """Record that a person checked the relation (reviewed_at = now)."""
        _require_persistent(relation, "relation")
        relation.reviewed_at = utcnow()
        self.session.flush()
        return relation

    def remove(self, relation: ConceptRelation) -> None:
        """Delete the relation."""
        _require_persistent(relation, "relation")
        self.session.delete(relation)
        self.session.flush()

    # -- helpers ----------------------------------------------------------------------------

    def _raise_conflict(self, existing: ConceptRelation) -> None:
        slugs = slugs_by_id(self.session, (existing.from_id, existing.to_id))
        raise RelationConflictError(
            f"concepts {slugs[existing.low_id]!r} and {slugs[existing.high_id]!r} already have "
            f"relation {describe(existing, slugs)}; two concepts can have at most one relation "
            "(update or remove it instead)"
        )


def touching(session: Session, concept_id: int) -> builtins.list[ConceptRelation]:
    """Every relation with `concept_id` at either end, in id order."""
    return list(
        session.scalars(
            select(ConceptRelation)
            .where(
                or_(ConceptRelation.from_id == concept_id, ConceptRelation.to_id == concept_id)
            )
            .order_by(ConceptRelation.id)
        )
    )


__all__ = [
    "ConceptRelationManager",
    "builds_on_edges",
    "canonical_endpoints",
    "describe",
    "raise_if_cyclic",
    "set_endpoints",
    "slugs_by_id",
    "touching",
    "validate_rationale",
]
