"""Grounding: whether an outline concept is backed by a source in the library, and the ordered
steps of an exploration as the roadmap (`teacher.exploration.roadmap`) reads them.

The rule. An outline concept is grounded when it has a graph concept (`concept_id`) and at least
one library source is linked to that concept by concept extraction (a `source_concepts` row)
with a role in `GROUNDING_ROLES`, that is `introduces`: the source teaches it. A source that only
`uses` or `assumes` it is listed with it but does not ground it (it builds on the concept
without explaining it). This is the graph match's coverage rule (`teacher.exploration.sources.
coverage_of`) seen from the other side: a grounded concept would now be `covered`, an ungrounded
one with a graph concept `thin`. Any library source counts, whoever added it; the links are read
from the graph as it is now. The enrichment (`teacher.exploration.enrichment`) checks it after
ingesting and stores each concept's sources (`ExplorationConcept.grounding`) and status.

`grounding_of(session, concept_id, ...)` lists a graph concept's library sources (grounding
ones first), each a `GroundingSource`; `is_grounded(sources)` applies the rule.

The roadmap seam: `exploration_roadmap(session, settings, exploration)` returns the outline's
steps in study order (`RoadmapStep`), each with its concepts that are not dropped
(`RoadmapConcept`: the outline concept, its graph concept, its prerequisites' keys, whether it is
grounded, its sources and a recommended `primary_source`). The primary source is a grounding
source (role `introduces`): first the ones this exploration ingested for that concept, by the
triage score they had for it (best first, then by rank), then any other in the library, oldest
first; None when the concept is not grounded. Reads only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    Concept,
    Exploration,
    ExplorationCandidate,
    ExplorationConcept,
    ExplorationLink,
    KnowledgeSource,
    SourceConcept,
)

GROUNDING_ROLES: tuple[str, ...] = ("introduces",)
_ROLE_ORDER = {"introduces": 0, "uses": 1, "assumes": 2}


@dataclass(frozen=True)
class GroundingSource:
    """A library source linked to a graph concept: its role, whether it grounds the concept,
    and, when this exploration ingested it for the outline concept, that candidate and the
    triage score and rank it had for it."""

    source_id: int
    slug: str
    title: str
    role: str
    grounds: bool
    candidate_id: int | None = None
    overall: float | None = None
    rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "role": self.role,
            "grounds": self.grounds,
            "candidate_id": self.candidate_id,
            "overall": self.overall,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class _Found:
    candidate_id: int
    overall: float | None
    rank: int


def _found_for(session: Session, outline_concept_id: int) -> dict[int, _Found]:
    """The ingested candidates found for one outline concept, by the source they became."""
    rows = session.execute(
        select(
            ExplorationCandidate.knowledge_source_id, ExplorationCandidate.id,
            ExplorationLink.overall, ExplorationLink.rank,
        )  # fmt: skip
        .join(ExplorationLink, ExplorationLink.candidate_id == ExplorationCandidate.id)
        .where(
            ExplorationLink.outline_concept_id == outline_concept_id,
            ExplorationCandidate.decision == "ingested",
            ExplorationCandidate.knowledge_source_id.is_not(None),
        )
    ).all()
    return {source_id: _Found(cid, overall, rank) for source_id, cid, overall, rank in rows}


def grounding_of(
    session: Session, concept_id: int, *, outline_concept_id: int | None = None
) -> list[GroundingSource]:
    """Every library source linked to graph concept `concept_id`, grounding ones first (see
    `primary_first`), with the exploration's candidates of `outline_concept_id` marked."""
    found = _found_for(session, outline_concept_id) if outline_concept_id is not None else {}
    rows = session.execute(
        select(SourceConcept.source_id, SourceConcept.role, KnowledgeSource.slug,
               KnowledgeSource.title)  # fmt: skip
        .join(KnowledgeSource, KnowledgeSource.id == SourceConcept.source_id)
        .where(SourceConcept.concept_id == concept_id)
    ).all()
    sources = []
    for source_id, role, slug, title in rows:
        hit = found.get(source_id)
        sources.append(
            GroundingSource(
                source_id=source_id, slug=slug, title=title, role=role,
                grounds=role in GROUNDING_ROLES,
                candidate_id=hit.candidate_id if hit else None,
                overall=hit.overall if hit else None,
                rank=hit.rank if hit else None,
            )  # fmt: skip
        )
    return primary_first(sources)


def primary_first(sources: Iterable[GroundingSource]) -> list[GroundingSource]:
    """`sources` with the grounding ones first, in the order the primary source is chosen: the
    exploration's own (best triage score, then rank), then the others, oldest first; then the
    ones that do not ground it (introduces, uses, assumes; oldest first)."""

    def key(s: GroundingSource) -> tuple[Any, ...]:
        own = s.candidate_id is not None
        return (
            not s.grounds,
            _ROLE_ORDER.get(s.role, 9),
            not own,
            -(s.overall if s.overall is not None else -1.0) if own else 0.0,
            s.rank if s.rank is not None else 0,
            s.source_id,
        )

    return sorted(sources, key=key)


def is_grounded(sources: Sequence[GroundingSource]) -> bool:
    return any(s.grounds for s in sources)


def primary_source(sources: Sequence[GroundingSource]) -> GroundingSource | None:
    """The recommended source to study the concept from (see the module docstring)."""
    ordered = primary_first(sources)
    return ordered[0] if ordered and ordered[0].grounds else None


@dataclass(frozen=True)
class RoadmapConcept:
    """One concept of the roadmap (see the module docstring). `concept_id`, `concept_slug` and
    `concept_name` are its graph concept's (None before an enrichment wrote it, or when it was
    deleted since); `prerequisites` the keys of the concepts (not dropped) it builds on."""

    outline_concept_id: int
    key: str
    position: int
    name: str
    summary: str
    status: str
    prerequisites: tuple[str, ...]
    concept_id: int | None
    concept_slug: str | None
    concept_name: str | None
    grounded: bool
    sources: tuple[GroundingSource, ...] = field(default=())
    primary_source: GroundingSource | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outline_concept_id": self.outline_concept_id,
            "key": self.key,
            "position": self.position,
            "name": self.name,
            "summary": self.summary,
            "status": self.status,
            "prerequisites": list(self.prerequisites),
            "concept": (
                {"id": self.concept_id, "slug": self.concept_slug, "name": self.concept_name}
                if self.concept_id is not None
                else None
            ),
            "grounded": self.grounded,
            "sources": [s.to_dict() for s in self.sources],
            "primary_source": self.primary_source.to_dict() if self.primary_source else None,
        }


@dataclass(frozen=True)
class RoadmapStep:
    number: int
    title: str
    concepts: tuple[RoadmapConcept, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "concepts": [c.to_dict() for c in self.concepts],
        }


def _roadmap_concept(
    session: Session, row: ExplorationConcept, dropped: Mapping[str, bool]
) -> RoadmapConcept:
    concept = session.get(Concept, row.concept_id) if row.concept_id is not None else None
    sources = (
        tuple(grounding_of(session, concept.id, outline_concept_id=row.id)) if concept else ()
    )
    return RoadmapConcept(
        outline_concept_id=row.id,
        key=row.key,
        position=row.position,
        name=row.name,
        summary=row.summary,
        status=row.status,
        prerequisites=tuple(k for k in row.prerequisites if k in dropped and not dropped[k]),
        concept_id=concept.id if concept else None,
        concept_slug=concept.slug if concept else None,
        concept_name=concept.name if concept else None,
        grounded=is_grounded(sources),
        sources=sources,
        primary_source=primary_source(sources),
    )


def exploration_roadmap(
    session: Session, settings: Settings, exploration: Exploration
) -> list[RoadmapStep]:
    """The exploration's steps in order with their concepts that are not dropped (see the
    module docstring). Reads only; a step whose concepts were all dropped is left out."""
    from teacher.managers import ExplorationManager

    rows = ExplorationManager(session, settings, owner=exploration.user_id).concepts(exploration)
    dropped = {r.key: r.status == "dropped" for r in rows}
    steps = []
    for number, title in enumerate(exploration.steps, start=1):
        concepts = tuple(
            _roadmap_concept(session, r, dropped)
            for r in rows
            if r.step == number and r.status != "dropped"
        )
        if concepts:
            steps.append(RoadmapStep(number, title, concepts))
    return steps


__all__ = [
    "GROUNDING_ROLES",
    "GroundingSource",
    "RoadmapConcept",
    "RoadmapStep",
    "exploration_roadmap",
    "grounding_of",
    "is_grounded",
    "primary_first",
    "primary_source",
]
