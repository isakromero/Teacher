"""How the knowledge sources relate to each other, derived on read from the concepts concept
extraction linked them to (`source_concepts`, `teacher.concept_extraction`; each link has a role
`introduces`, `uses` or `assumes`), the reader's standing on each concept
(`teacher.managers.concept_standing`) and the concept graph's builds_on relations
(`teacher.concept_graph`). Nothing is stored and no model is called.

Analysed. A source is *analysed* when it has at least one concept link. A source without any
has not been through concept extraction (or it found nothing): it is reported as not analysed
(`analysed: false`, its relations `None`), never as a source without relations, and it takes
part in no edge. Every source's relations come with the list of the other sources that are not
analysed (`not_analysed`), so a reader can tell "nothing related" from "not compared yet".

The edges, between two different analysed sources A and B:
- `builds_on` (directed, A -> B): A uses or assumes a concept that B introduces. The edge carries
  every such concept with A's role towards it (`uses` or `assumes`), by role (uses, then
  assumes), then name. Two sources may build on each other (through different concepts).
  `built_on_by` of B is the same edges seen from B.
- `related` (undirected, weighted): A and B are linked to at least one concept in common, in any
  roles. The weight is the cosine similarity of the two sources' role-weighted concept vectors:
  each source is a vector over concepts whose entry is ROLE_WEIGHTS[role] for each concept it
  is linked to (introduces 3, uses 2, assumes 1; 0 when not linked), and
      weight(A, B) = sum over shared concepts c of w_A(c) * w_B(c) / (|w_A| * |w_B|),
  where |w| is the vector's Euclidean length. It lies in (0, 1]: 1 when both have the same
  concepts in the same proportions. Dividing by the lengths keeps a source with many concepts
  from being related to everything more strongly than a small one, and the role weights make a
  concept both introduce count more than one both merely assume. Any shared concept makes an
  edge (no threshold). Each edge carries the shared concepts with both roles, the heaviest
  (w_A * w_B) first, then by name. A pair can be related and also have builds_on edges.
Sources are ranked: builds_on and built_on_by by more justifying concepts first, then title,
then slug; related by weight (highest first), then more shared concepts, then title, then slug.

Readiness (per analysed source): of the concepts the source *assumes* (its background, which it
does not explain), the ones the reader is not in good standing on (status `unstudied` or `weak`;
good standing is `developing` or `strong`, `teacher.managers.concept_standing.GOOD_STANDING`),
in study order (`teacher.concept_graph`: after every listed concept it transitively builds on,
then by slug), each with the library sources that introduce it ("read this first"): sources with
a ready study page first, then by title, then slug, each with its ready study pages (kind other
than `lab`, status `ready`, newest first, `superseded` when a newer page replaces it). A concept
nothing in the library introduces is listed with no sources. `assumed` counts every assumed
concept, so "assumes nothing" and "assumes only what you know" are told apart.

Citations, independent of the concepts (a source can have references without concept links and
the reverse): the resolved citations between library sources
(`SourceReferenceManager.citation_edges()`, `teacher.managers.source_references`; matching is
by DOI, arXiv id or exact title there, never fuzzy).
- `cites` (directed, A -> B): one of A's stored references matched library source B (A != B).
  `cited_by` of B is the same edges seen from B. Both by title, then slug.
- Each source's `references` say what is known: `state` is the latest extraction run's state
  (`never_run`, `running`, `interrupted`, `done` or `failed`, as `extraction_state` gives it),
  `stored` whether references from a successful run are stored (a failed or interrupted run
  keeps the earlier references, as the references endpoint shows them), and `reference_count`
  how many (None when none are stored). A source's `cites` is `None` unless references are
  stored: a never-run or failed extraction is never shown as "cites nothing". `cited_by` is
  always a list, since it comes from the other sources' references; those whose references are
  not stored are listed (`references_not_extracted`), so "cited by nothing" can be told from
  "the others were not read".
A citation is a fact of its own: a pair may be both cited and related through concepts.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.concept_graph import ConceptGraph
from teacher.concepts import SOURCE_ROLES
from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactSource,
    Concept,
    KnowledgeSource,
    SourceConcept,
    SourceReferenceExtraction,
    utcnow,
)
from teacher.labs import LAB_KIND
from teacher.managers.concept_standing import (
    GOOD_STANDING,
    STANDING_STATUSES,
    ConceptStandingManager,
)
from teacher.managers.errors import NotFoundError
from teacher.managers.source_references import (
    EXTRACTION_STATES,
    SourceReferenceManager,
    extraction_state,
)

INTRODUCES, USES, ASSUMES = SOURCE_ROLES
DEPENDENT_ROLES: tuple[str, ...] = (USES, ASSUMES)  # the roles that make a builds_on edge
ROLE_WEIGHTS: Mapping[str, int] = {INTRODUCES: 3, USES: 2, ASSUMES: 1}
WEIGHT_DIGITS = 6  # the JSON's weights are rounded to this many digits (ranking is not)
assert set(ROLE_WEIGHTS) == set(SOURCE_ROLES)


def rules() -> dict[str, Any]:
    """The rules' constants, as JSON (shown with the relations)."""
    return {
        "roles": list(SOURCE_ROLES),
        "builds_on_roles": list(DEPENDENT_ROLES),
        "role_weights": dict(ROLE_WEIGHTS),
        "related_weight": "cosine similarity of the role-weighted concept vectors",
        "readiness_role": ASSUMES,
        "good_standing": sorted(GOOD_STANDING),
        "reference_states": list(EXTRACTION_STATES),
    }


# -- the data ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    id: int
    slug: str
    title: str
    kind: str

    def record(self) -> dict[str, Any]:
        return {"slug": self.slug, "title": self.title, "kind": self.kind}


@dataclass(frozen=True)
class ConceptRef:
    id: int
    slug: str
    name: str

    def record(self) -> dict[str, Any]:
        return {"slug": self.slug, "name": self.name}


@dataclass(frozen=True)
class PageRef:
    """A ready study page of a source."""

    slug: str
    title: str
    superseded: bool

    def record(self) -> dict[str, Any]:
        return {"slug": self.slug, "title": self.title, "superseded": self.superseded}


@dataclass(frozen=True)
class BuildsOn:
    """`dependent` uses or assumes each of `concepts` (with its role), which `foundation`
    introduces."""

    dependent: SourceRef
    foundation: SourceRef
    concepts: tuple[tuple[ConceptRef, str], ...]


@dataclass(frozen=True)
class SharedConcept:
    concept: ConceptRef
    role_a: str
    role_b: str

    @property
    def product(self) -> int:
        return ROLE_WEIGHTS[self.role_a] * ROLE_WEIGHTS[self.role_b]


@dataclass(frozen=True)
class Related:
    """`a` and `b` share `shared` (by slug, `a` before `b`); `weight` as in the module
    docstring."""

    a: SourceRef
    b: SourceRef
    weight: float
    shared: tuple[SharedConcept, ...]


@dataclass(frozen=True)
class ReadFirst:
    """An assumed concept the reader is not in good standing on, and who introduces it."""

    concept: ConceptRef
    status: str
    introduced_by: tuple[tuple[SourceRef, tuple[PageRef, ...]], ...]


@dataclass(frozen=True)
class Readiness:
    assumed: int
    read_first: tuple[ReadFirst, ...]


def _concept_list_key(item: tuple[ConceptRef, str]) -> tuple[Any, ...]:
    concept, role = item
    return (SOURCE_ROLES.index(role), concept.name.casefold(), concept.slug)


def _source_key(source: SourceRef) -> tuple[str, str]:
    return (source.title.casefold(), source.slug)


class SourceGraph:
    """The sources and their concept links, with the edges between them (module docstring).

    `links` are (source id, concept, role); at most one per (source, concept), as the table's
    unique constraint keeps them. A link to a source not in `sources`, an unknown role or a
    second link for the same pair is a ValueError."""

    def __init__(
        self,
        sources: Iterable[SourceRef],
        links: Iterable[tuple[int, ConceptRef, str]],
    ) -> None:
        self._sources: dict[int, SourceRef] = {}
        for source in sources:
            if source.id in self._sources:
                raise ValueError(f"source #{source.id} is given twice")
            self._sources[source.id] = source
        self._by_slug = {s.slug: s for s in self._sources.values()}
        self._roles: dict[int, dict[int, str]] = {i: {} for i in self._sources}
        self._concepts: dict[int, ConceptRef] = {}
        for source_id, concept, role in links:
            if source_id not in self._sources:
                raise ValueError(f"a concept link refers to source #{source_id}, not loaded")
            if role not in SOURCE_ROLES:
                raise ValueError(
                    f"source #{source_id} links concept {concept.slug!r} with unknown role "
                    f"{role!r}: expected one of {SOURCE_ROLES}"
                )
            if concept.id in self._roles[source_id]:
                raise ValueError(
                    f"source #{source_id} is linked to concept {concept.slug!r} twice"
                )
            self._roles[source_id][concept.id] = role
            self._concepts[concept.id] = concept
        self._builds_on, self._related = self._edges()

    @classmethod
    def load(cls, session: Session) -> SourceGraph:
        """Every source and concept link, read once."""
        sources = [
            SourceRef(s.id, s.slug, s.title, s.kind)
            for s in session.scalars(select(KnowledgeSource))
        ]
        links = [
            (source_id, ConceptRef(c.id, c.slug, c.name), role)
            for source_id, role, c in session.execute(
                select(SourceConcept.source_id, SourceConcept.role, Concept).join(
                    Concept, Concept.id == SourceConcept.concept_id
                )
            )
        ]
        return cls(sources, links)

    # -- lookups ----------------------------------------------------------------------------

    @property
    def sources(self) -> list[SourceRef]:
        """Every source, by title, then slug."""
        return sorted(self._sources.values(), key=_source_key)

    def by_slug(self, slug: str) -> SourceRef:
        try:
            return self._by_slug[slug]
        except KeyError:
            raise NotFoundError(f"no knowledge source with slug {slug!r}") from None

    def _id(self, source: SourceRef) -> int:
        if self._sources.get(source.id) != source:
            raise NotFoundError(f"source {source.slug!r} is not in this graph")
        return source.id

    def analysed(self, source: SourceRef) -> bool:
        return bool(self._roles[self._id(source)])

    def not_analysed(self) -> list[SourceRef]:
        """The sources without any concept link, by title, then slug."""
        return [s for s in self.sources if not self._roles[s.id]]

    def concept_counts(self, source: SourceRef) -> dict[str, int]:
        """How many concepts the source has in each role."""
        roles = self._roles[self._id(source)].values()
        return {role: sum(1 for r in roles if r == role) for role in SOURCE_ROLES}

    def concepts_of(self, source: SourceRef, role: str) -> list[ConceptRef]:
        """The source's concepts in `role`, by slug."""
        if role not in SOURCE_ROLES:
            raise ValueError(f"invalid source role {role!r}: expected one of {SOURCE_ROLES}")
        return sorted(
            (self._concepts[c] for c, r in self._roles[self._id(source)].items() if r == role),
            key=lambda c: c.slug,
        )

    def introducers(self, concept: ConceptRef) -> list[SourceRef]:
        """The sources that introduce `concept`, by title, then slug."""
        return [s for s in self.sources if self._roles[s.id].get(concept.id) == INTRODUCES]

    # -- the edges ----------------------------------------------------------------------------

    def _edges(self) -> tuple[list[BuildsOn], list[Related]]:
        linked: dict[int, list[tuple[int, str]]] = defaultdict(list)  # concept -> (source, role)
        for source_id, roles in self._roles.items():
            for concept_id, role in roles.items():
                linked[concept_id].append((source_id, role))

        builds: dict[tuple[int, int], list[tuple[ConceptRef, str]]] = defaultdict(list)
        shared: dict[tuple[int, int], list[SharedConcept]] = defaultdict(list)
        for concept_id, pairs in linked.items():
            concept = self._concepts[concept_id]
            for a, role_a in pairs:
                for b, role_b in pairs:
                    if a == b:
                        continue
                    if role_a in DEPENDENT_ROLES and role_b == INTRODUCES:
                        builds[(a, b)].append((concept, role_a))
                    first, second = self._sources[a], self._sources[b]
                    if first.slug < second.slug:
                        shared[(a, b)].append(SharedConcept(concept, role_a, role_b))

        builds_on = sorted(
            (
                BuildsOn(self._sources[a], self._sources[b],
                         tuple(sorted(items, key=_concept_list_key)))
                for (a, b), items in builds.items()
            ),
            key=lambda e: (e.dependent.slug, e.foundation.slug),
        )
        length = {
            i: math.sqrt(sum(ROLE_WEIGHTS[r] ** 2 for r in roles.values()))
            for i, roles in self._roles.items()
        }
        related = []
        for (a, b), items in shared.items():
            dot = sum(item.product for item in items)
            related.append(
                Related(
                    self._sources[a],
                    self._sources[b],
                    dot / (length[a] * length[b]),
                    tuple(sorted(
                        items, key=lambda s: (-s.product, s.concept.name.casefold(), s.concept.slug)
                    )),
                )
            )
        related.sort(key=lambda e: (-e.weight, e.a.slug, e.b.slug))
        return builds_on, related

    @property
    def builds_on_edges(self) -> list[BuildsOn]:
        """Every builds_on edge, by dependent slug, then foundation slug."""
        return list(self._builds_on)

    @property
    def related_edges(self) -> list[Related]:
        """Every related edge, highest weight first, then by the two slugs."""
        return list(self._related)

    def builds_on(self, source: SourceRef) -> list[BuildsOn]:
        """The edges from `source` to what it builds on, ranked (module docstring)."""
        source_id = self._id(source)
        found = [e for e in self._builds_on if e.dependent.id == source_id]
        return sorted(found, key=lambda e: (-len(e.concepts), *_source_key(e.foundation)))

    def built_on_by(self, source: SourceRef) -> list[BuildsOn]:
        """The edges into `source` from the sources that build on it, ranked."""
        source_id = self._id(source)
        found = [e for e in self._builds_on if e.foundation.id == source_id]
        return sorted(found, key=lambda e: (-len(e.concepts), *_source_key(e.dependent)))

    def related(self, source: SourceRef) -> list[tuple[SourceRef, Related]]:
        """(the other source, the edge) for every related edge of `source`, ranked."""
        source_id = self._id(source)
        found = [
            (e.b if e.a.id == source_id else e.a, e)
            for e in self._related
            if source_id in (e.a.id, e.b.id)
        ]
        return sorted(
            found, key=lambda pair: (-pair[1].weight, -len(pair[1].shared), *_source_key(pair[0]))
        )


# -- citations ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceState:
    """What is known of a source's references (module docstring)."""

    state: str
    stored: bool
    reference_count: int | None

    def __post_init__(self) -> None:
        if self.state not in EXTRACTION_STATES:
            raise ValueError(
                f"unknown reference extraction state {self.state!r}: expected one of "
                f"{EXTRACTION_STATES}"
            )
        if self.stored != (self.reference_count is not None):
            raise ValueError("reference_count is given exactly when references are stored")
        if self.state == "done" and not self.stored:
            raise ValueError("a done extraction has stored references")

    def record(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "stored": self.stored,
            "reference_count": self.reference_count,
        }


NEVER_RUN = ReferenceState("never_run", False, None)


class Citations:
    """The citation edges between library sources, with each source's reference state.

    `states` has an entry per source id with an extraction row; a source without one has never
    had its references extracted (NEVER_RUN). An edge between unknown sources, a self-citation,
    or one from a source without stored references (it has nothing to cite from) is a
    ValueError."""

    def __init__(
        self,
        graph: SourceGraph,
        edges: Iterable[tuple[int, int]],
        states: Mapping[int, ReferenceState],
    ) -> None:
        self._graph = graph
        self._by_id = {s.id: s for s in graph.sources}
        unknown = sorted(set(states) - set(self._by_id))
        if unknown:
            raise ValueError(f"reference states given for sources {unknown}, not loaded")
        self._states = {i: states.get(i, NEVER_RUN) for i in self._by_id}
        self._cites: dict[int, set[int]] = {i: set() for i in self._by_id}
        self._cited_by: dict[int, set[int]] = {i: set() for i in self._by_id}
        for citing, cited in edges:
            if citing not in self._by_id or cited not in self._by_id:
                raise ValueError(f"a citation {citing} -> {cited} refers to a source not loaded")
            if citing == cited:
                raise ValueError(f"source #{citing} cites itself")
            if not self._states[citing].stored:
                raise ValueError(
                    f"source {self._by_id[citing].slug!r} cites another source but has no "
                    "stored references"
                )
            self._cites[citing].add(cited)
            self._cited_by[cited].add(citing)

    @classmethod
    def load(
        cls, session: Session, settings: Settings, graph: SourceGraph, *, now: datetime
    ) -> Citations:
        """The stored citation edges and every extraction row; `now` (naive UTC) decides
        whether a run still `running` was interrupted."""
        states = {
            row.source_id: ReferenceState(
                extraction_state(row, now), row.succeeded_at is not None, row.reference_count
            )
            for row in session.scalars(select(SourceReferenceExtraction))
        }
        edges = SourceReferenceManager(session, settings).citation_edges()
        return cls(graph, edges, states)

    def _sorted(self, ids: Iterable[int]) -> list[SourceRef]:
        return sorted((self._by_id[i] for i in ids), key=_source_key)

    def state(self, source: SourceRef) -> ReferenceState:
        return self._states[self._graph._id(source)]

    def cites(self, source: SourceRef) -> list[SourceRef] | None:
        """The library sources `source` cites, by title, then slug; None when its references
        are not stored (unknown, not "none")."""
        source_id = self._graph._id(source)
        if not self._states[source_id].stored:
            return None
        return self._sorted(self._cites[source_id])

    def cited_by(self, source: SourceRef) -> list[SourceRef]:
        """The library sources citing `source`, by title, then slug."""
        return self._sorted(self._cited_by[self._graph._id(source)])

    def not_extracted(self) -> list[SourceRef]:
        """The sources without stored references, by title, then slug."""
        return [s for s in self._graph.sources if not self._states[s.id].stored]

    @property
    def edges(self) -> list[tuple[SourceRef, SourceRef]]:
        """Every (citing, cited), by citing slug, then cited slug."""
        pairs = [
            (self._by_id[a], self._by_id[b]) for a, cited in self._cites.items() for b in cited
        ]
        return sorted(pairs, key=lambda p: (p[0].slug, p[1].slug))


# -- readiness ----------------------------------------------------------------------------------


def _study_order(
    concepts: Sequence[ConceptRef], prerequisites: Mapping[int, Collection[int]]
) -> list[ConceptRef]:
    """`concepts` after every one of them they transitively build on, then by slug."""
    ids = {c.id for c in concepts}
    by_id = {c.id: c for c in concepts}
    missing = [c.slug for c in concepts if c.id not in prerequisites]
    if missing:
        raise ValueError(f"no prerequisites given for concepts {missing}")
    waiting = {c.id: set(prerequisites[c.id]) & ids - {c.id} for c in concepts}
    ready = [(by_id[i].slug, i) for i, before in waiting.items() if not before]
    heapq.heapify(ready)
    order: list[ConceptRef] = []
    while ready:
        _slug, node = heapq.heappop(ready)
        order.append(by_id[node])
        for other, before in waiting.items():
            if node in before:
                before.discard(node)
                if not before:
                    heapq.heappush(ready, (by_id[other].slug, other))
    if len(order) != len(concepts):
        raise ValueError("the prerequisites given contain a cycle")
    return order


def readiness(
    graph: SourceGraph,
    source: SourceRef,
    statuses: Mapping[int, str],
    prerequisites: Mapping[int, Collection[int]],
    pages: Mapping[int, Sequence[PageRef]],
) -> Readiness:
    """The source's readiness (module docstring). `statuses` is the standing status by concept
    id (every assumed concept must have one: a missing or unknown status is a ValueError);
    `prerequisites` the ids each concept not in good standing transitively builds on; `pages`
    the ready study pages by source id, newest first (a source missing from it has none).
    ValueError for a source that is not analysed: it has no readiness to report."""
    if not graph.analysed(source):
        raise ValueError(f"source {source.slug!r} is not analysed: it has no concept links")
    assumed = graph.concepts_of(source, ASSUMES)
    gaps = []
    for concept in assumed:
        if concept.id not in statuses:
            raise ValueError(f"no standing given for concept {concept.slug!r}")
        status = statuses[concept.id]
        if status not in STANDING_STATUSES:
            raise ValueError(f"concept {concept.slug!r} has unknown standing status {status!r}")
        if status not in GOOD_STANDING:
            gaps.append(concept)
    read_first = []
    for concept in _study_order(gaps, prerequisites):
        introducers = graph.introducers(concept)
        introducers.sort(key=lambda s: (not pages.get(s.id), *_source_key(s)))
        read_first.append(
            ReadFirst(
                concept,
                statuses[concept.id],
                tuple((s, tuple(pages.get(s.id, ()))) for s in introducers),
            )
        )
    return Readiness(len(assumed), tuple(read_first))


def ready_pages(session: Session) -> dict[int, list[PageRef]]:
    """The ready study pages (not labs) of each source id, newest first."""
    superseded = {
        i for i in session.scalars(
            select(Artifact.supersedes_id).where(Artifact.supersedes_id.is_not(None))
        )
    }
    pages: dict[int, list[PageRef]] = defaultdict(list)
    for source_id, artifact_id, slug, title in session.execute(
        select(ArtifactSource.source_id, Artifact.id, Artifact.slug, Artifact.title)
        .join(Artifact, Artifact.id == ArtifactSource.artifact_id)
        .where(Artifact.kind != LAB_KIND, Artifact.status == "ready")
        .order_by(Artifact.id.desc())
    ):
        pages[source_id].append(PageRef(slug, title, artifact_id in superseded))
    return dict(pages)


# -- as JSON ------------------------------------------------------------------------------------


def _concepts_record(items: Iterable[tuple[ConceptRef, str]]) -> list[dict[str, Any]]:
    return [{**concept.record(), "role": role} for concept, role in items]


def readiness_record(value: Readiness) -> dict[str, Any]:
    return {
        "assumed": value.assumed,
        "read_first": [
            {
                "concept": item.concept.record(),
                "status": item.status,
                "introduced_by": [
                    {
                        "source": source.record(),
                        "has_ready_page": bool(source_pages),
                        "study_pages": [p.record() for p in source_pages],
                    }
                    for source, source_pages in item.introduced_by
                ],
            }
            for item in value.read_first
        ],
    }


def _refs(sources: Iterable[SourceRef]) -> list[dict[str, Any]]:
    return [{"source": s.record()} for s in sources]


def source_relations(
    session: Session, settings: Settings, slug: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """One source's relations, as JSON (NotFoundError for an unknown slug):
    `source`, `analysed`, `concept_counts`, `builds_on`, `built_on_by`, `related`, `readiness`
    (the last four `None` when the source is not analysed), `not_analysed` (the other sources,
    never compared), the citations `references` (the source's reference state), `cites` (`None`
    unless its references are stored), `cited_by` and `references_not_extracted` (the other
    sources without stored references), and `rules`. `now` (naive UTC, default the current
    time) decides whether a running extraction was interrupted."""
    graph = SourceGraph.load(session)
    source = graph.by_slug(slug)
    analysed = graph.analysed(source)
    citations = Citations.load(
        session, settings, graph, now=utcnow() if now is None else now
    )
    cites = citations.cites(source)
    record: dict[str, Any] = {
        "source": source.record(),
        "analysed": analysed,
        "concept_counts": graph.concept_counts(source),
        "builds_on": None,
        "built_on_by": None,
        "related": None,
        "readiness": None,
        "not_analysed": [s.record() for s in graph.not_analysed() if s.id != source.id],
        "references": citations.state(source).record(),
        "cites": None if cites is None else _refs(cites),
        "cited_by": _refs(citations.cited_by(source)),
        "references_not_extracted": [
            s.record() for s in citations.not_extracted() if s.id != source.id
        ],
        "rules": rules(),
    }
    if not analysed:
        return record
    standings = ConceptStandingManager(session, settings).standings()
    statuses = {concept_id: s.status for concept_id, s in standings.items()}
    concepts = ConceptGraph.load(session)
    prerequisites = {
        c.id: {p.id for p in concepts.prerequisites(concepts.by_slug(c.slug))}
        for c in graph.concepts_of(source, ASSUMES)
        if statuses.get(c.id) not in GOOD_STANDING
    }
    record["builds_on"] = [
        {"source": e.foundation.record(), "concepts": _concepts_record(e.concepts)}
        for e in graph.builds_on(source)
    ]
    record["built_on_by"] = [
        {"source": e.dependent.record(), "concepts": _concepts_record(e.concepts)}
        for e in graph.built_on_by(source)
    ]
    record["related"] = []
    for other, edge in graph.related(source):
        mine_first = edge.a.id == source.id
        record["related"].append(
            {
                "source": other.record(),
                "weight": round(edge.weight, WEIGHT_DIGITS),
                "shared": [
                    {
                        **s.concept.record(),
                        "role": s.role_a if mine_first else s.role_b,
                        "other_role": s.role_b if mine_first else s.role_a,
                    }
                    for s in edge.shared
                ],
            }
        )
    record["readiness"] = readiness_record(
        readiness(graph, source, statuses, prerequisites, ready_pages(session))
    )
    return record


def source_graph(
    session: Session, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """Every source as a node and every edge, as JSON: `nodes` (by title, then slug: `slug`,
    `title`, `kind`, `analysed`, `concept_counts`, `has_ready_page`, `references`), `edges`
    (the builds_on edges, `from` the dependent `to` the foundation, by those slugs; then the
    cites edges, `from` the citing source `to` the cited one, by those slugs; then the related
    edges, `from` the smaller slug, highest weight first) and `rules`. `now` as in
    `source_relations`."""
    graph = SourceGraph.load(session)
    citations = Citations.load(
        session, settings, graph, now=utcnow() if now is None else now
    )
    pages = ready_pages(session)
    nodes = [
        {
            **s.record(),
            "analysed": graph.analysed(s),
            "concept_counts": graph.concept_counts(s),
            "has_ready_page": bool(pages.get(s.id)),
            "references": citations.state(s).record(),
        }
        for s in graph.sources
    ]
    edges: list[dict[str, Any]] = [
        {
            "type": "builds_on",
            "from": e.dependent.slug,
            "to": e.foundation.slug,
            "concepts": _concepts_record(e.concepts),
        }
        for e in graph.builds_on_edges
    ]
    edges += [
        {"type": "cites", "from": citing.slug, "to": cited.slug}
        for citing, cited in citations.edges
    ]
    edges += [
        {
            "type": "related",
            "from": e.a.slug,
            "to": e.b.slug,
            "weight": round(e.weight, WEIGHT_DIGITS),
            "shared": [
                {**s.concept.record(), "from_role": s.role_a, "to_role": s.role_b}
                for s in e.shared
            ],
        }
        for e in graph.related_edges
    ]
    return {"nodes": nodes, "edges": edges, "rules": rules()}


__all__ = [
    "ASSUMES",
    "DEPENDENT_ROLES",
    "INTRODUCES",
    "ROLE_WEIGHTS",
    "USES",
    "NEVER_RUN",
    "BuildsOn",
    "Citations",
    "ConceptRef",
    "PageRef",
    "ReadFirst",
    "Readiness",
    "ReferenceState",
    "Related",
    "SharedConcept",
    "SourceGraph",
    "SourceRef",
    "readiness",
    "readiness_record",
    "ready_pages",
    "rules",
    "source_graph",
    "source_relations",
]
