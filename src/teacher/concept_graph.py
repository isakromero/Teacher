"""ConceptGraph: the concept graph loaded into memory for traversal (reads only, no writes).

`ConceptGraph.load(session)` reads every concept and relation once and builds adjacency maps.
"A builds_on B" means B is a prerequisite of A. Loading refuses a builds_on cycle (which the
write paths make impossible) with CycleError, using the same `teacher.concepts.find_cycle`.

Study order: wherever a list is in study order, a concept comes after everything it
transitively builds on (also through concepts left out of the list), and among concepts that
could come next the smallest slug goes first, so the order is deterministic.

Roadmap rule (`roadmap(target, known)`): a concept is on the roadmap if it is not known and can
be reached from the target by following builds_on edges through concepts that are not known.
Knowing a concept is taken to cover what it builds on, so the walk stops at a known concept;
a prerequisite below a known concept still appears if some unknown concept on the way also
needs it. The target itself is last; a known target gives an empty roadmap.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.concepts import (
    BUILDS_ON,
    RELATION_TYPES,
    SYMMETRIC_RELATION_TYPES,
    find_cycle,
    format_cycle,
)
from teacher.db.models import Concept, ConceptRelation
from teacher.managers.errors import CycleError, NotFoundError

ALL_TYPES = "all"


class ConceptGraph:
    """In-memory view of the concepts and their relations. Build with `load(session)`."""

    def __init__(self, concepts: Iterable[Concept], relations: Iterable[ConceptRelation]) -> None:
        self._concepts: dict[int, Concept] = {c.id: c for c in concepts}
        self._by_slug: dict[str, Concept] = {c.slug: c for c in self._concepts.values()}
        self._relations: list[ConceptRelation] = sorted(relations, key=lambda r: r.id)
        self._requires: dict[int, set[int]] = {i: set() for i in self._concepts}
        self._unlocks: dict[int, set[int]] = {i: set() for i in self._concepts}
        self._linked: dict[str, dict[int, set[int]]] = {
            t: {i: set() for i in self._concepts} for t in RELATION_TYPES
        }
        self._builds_on: dict[tuple[int, int], ConceptRelation] = {}
        for relation in self._relations:
            a, b = relation.from_id, relation.to_id
            if a not in self._concepts or b not in self._concepts:
                raise NotFoundError(
                    f"relation #{relation.id} refers to a concept that was not loaded"
                )
            if relation.type not in RELATION_TYPES:
                raise ValueError(f"relation #{relation.id} has unknown type {relation.type!r}")
            self._linked[relation.type][a].add(b)
            self._linked[relation.type][b].add(a)
            if relation.type == BUILDS_ON:
                self._requires[a].add(b)
                self._unlocks[b].add(a)
                self._builds_on[(a, b)] = relation
        cycle = find_cycle(self._requires)
        if cycle is not None:
            slugs = tuple(self._concepts[i].slug for i in cycle)
            raise CycleError(
                f"the stored builds_on relations contain a cycle: {format_cycle(slugs)}", slugs
            )

    @classmethod
    def load(cls, session: Session) -> ConceptGraph:
        """Every concept and relation, read once."""
        concepts = session.scalars(select(Concept)).all()
        relations = session.scalars(select(ConceptRelation)).all()
        return cls(concepts, relations)

    # -- lookups ----------------------------------------------------------------------------

    @property
    def concepts(self) -> list[Concept]:
        """All concepts, by slug."""
        return [self._by_slug[slug] for slug in sorted(self._by_slug)]

    def by_slug(self, slug: str) -> Concept:
        try:
            return self._by_slug[slug]
        except KeyError:
            raise NotFoundError(f"no concept with slug {slug!r} in the graph") from None

    def _id(self, concept: Concept) -> int:
        if not isinstance(concept, Concept):
            raise TypeError(f"expected a Concept, got {concept.__class__.__name__}")
        if concept.id not in self._concepts:
            raise NotFoundError(
                f"concept {concept.slug!r} is not in this graph (was it created after loading?)"
            )
        return concept.id

    def _sorted(self, ids: Iterable[int]) -> list[Concept]:
        return sorted((self._concepts[i] for i in ids), key=lambda c: c.slug)

    # -- traversals -------------------------------------------------------------------------

    def _reachable(self, start: int, *, stop: set[int] | None = None) -> set[int]:
        """Everything `start` transitively builds on (not `start`), not walking past `stop`
        (stop nodes are neither included nor expanded)."""
        stop = stop or set()
        seen: set[int] = set()
        pending = [start]
        while pending:
            for prerequisite in self._requires[pending.pop()]:
                if prerequisite not in seen and prerequisite not in stop:
                    seen.add(prerequisite)
                    pending.append(prerequisite)
        return seen

    def _study_order(self, ids: set[int]) -> list[Concept]:
        """`ids` in study order: after all their transitive prerequisites, smallest slug first."""
        waiting = {i: self._reachable(i) & ids for i in ids}
        dependents: dict[int, set[int]] = {i: set() for i in ids}
        for node, prerequisites in waiting.items():
            for prerequisite in prerequisites:
                dependents[prerequisite].add(node)
        ready = [(self._concepts[i].slug, i) for i, before in waiting.items() if not before]
        heapq.heapify(ready)
        order: list[Concept] = []
        while ready:
            _slug, node = heapq.heappop(ready)
            order.append(self._concepts[node])
            for dependent in dependents[node]:
                waiting[dependent].discard(node)
                if not waiting[dependent]:
                    heapq.heappush(ready, (self._concepts[dependent].slug, dependent))
        assert len(order) == len(ids), "builds_on is acyclic (checked on load)"
        return order

    def prerequisites(self, concept: Concept) -> list[Concept]:
        """Everything `concept` transitively builds on, in study order (itself excluded)."""
        return self._study_order(self._reachable(self._id(concept)))

    def roadmap(self, target: Concept, known: Iterable[Concept] = ()) -> list[Concept]:
        """What to study to reach `target`, in study order, ending with `target`; see the module
        docstring for which prerequisites a known concept covers."""
        target_id = self._id(target)
        known_ids = {self._id(concept) for concept in known}
        if target_id in known_ids:
            return []
        needed = self._reachable(target_id, stop=known_ids) | {target_id}
        return self._study_order(needed)

    def unlocks(self, concept: Concept) -> list[Concept]:
        """Concepts that build directly on `concept`, by slug."""
        return self._sorted(self._unlocks[self._id(concept)])

    def neighbours(
        self, concept: Concept, type: str | None = None
    ) -> list[tuple[Concept, str]]:
        """Directly related concepts as `(concept, relation type)`, by slug.

        `type=None` follows the symmetric relations (different_version_of, relates_to); a
        relation type follows only that one (builds_on in both directions); `"all"` follows
        every type. ValueError for anything else.
        """
        node = self._id(concept)
        if type is None:
            types: tuple[str, ...] = SYMMETRIC_RELATION_TYPES
        elif type == ALL_TYPES:
            types = RELATION_TYPES
        elif type in RELATION_TYPES:
            types = (type,)
        else:
            raise ValueError(
                f"invalid relation type {type!r}: expected None, {ALL_TYPES!r} or one of "
                f"{RELATION_TYPES}"
            )
        found = [
            (self._concepts[other], t) for t in types for other in self._linked[t][node]
        ]
        return sorted(found, key=lambda pair: (pair[0].slug, pair[1]))

    def redundant_builds_on(self) -> list[ConceptRelation]:
        """builds_on relations implied by a longer builds_on path (A -> B when also
        A -> C -> ... -> B), by (from slug, to slug). They stay stored; this is for display."""
        redundant = []
        for (a, b), relation in self._builds_on.items():
            if any(b in self._reachable(c) for c in self._requires[a] if c != b):
                redundant.append(relation)
        return sorted(
            redundant,
            key=lambda r: (self._concepts[r.from_id].slug, self._concepts[r.to_id].slug),
        )


__all__ = ["ALL_TYPES", "ConceptGraph"]
