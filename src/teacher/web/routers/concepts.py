"""Concept graph endpoints for the "explore the concept graph" page (`/graph`). All read-only
except `POST /api/graph/concepts/{slug}/relate`.

- `GET /api/graph?limit=300` -> `{"nodes", "edges", "total_nodes", "total_edges", "limit",
  "truncated"}`. When there are more concepts than `limit`, the most connected ones are kept
  (most relations first, then by slug), `truncated` is true and `total_nodes`/`total_edges`
  give the full counts; `edges` are the relations whose both ends are among `nodes`.
- `GET /api/graph/search?q=...&limit=20` -> `{"query", "limit", "results", "semantic"}`:
  hybrid search (`teacher.concept_hybrid_search`): the inverted-index tiers
  (`ConceptManager.search_hits`: exact, word, prefix, substring, fuzzy) and the query's nearest
  concepts by embedding (`app.state.search_embedder`, one embedding request) merged into one
  list (`merge_hits`). Each result is a node plus a short `summary` and how it was found: the
  lexical match (`match_kind`, `match_field`, `matched`; all null when only found by meaning)
  and/or `similarity` (the query's cosine, null when not found by meaning). `semantic` says
  whether meaning was searched: `{"status": ok | unavailable | stale | failed | skipped,
  "reason", "model", "floor", "stale_count", "concept_count", "fix"}`; for any status but `ok`
  the results are names only and `reason` says why. The search never embeds stale concepts or
  writes to the database.
- `GET /api/graph/concepts/{slug}` -> the concept with its aliases, timestamps, relations
  (each with its `direction` seen from this concept and the concept at the other end),
  `prerequisite_count` (everything it transitively builds on), `unlocks_count` (concepts
  that build directly on it), and `sources`: the knowledge sources it appears in
  (`{"slug", "title", "role"}`, by role then title).
- `GET /api/graph/concepts/{slug}/neighbourhood` -> `{"concept", "nodes", "edges",
  "neighbour_edges"}`: the concept, the concepts it is directly related to, its relations
  (`edges`), and the other relations of those neighbours (`neighbour_edges`), so a client that
  adds the neighbours to a drawing can also connect them to concepts it already shows.
- `GET /api/graph/concepts/{slug}/roadmap` -> `{"concept", "roadmap", "edges"}`: what to study
  to reach the concept in study order, ending with it (`ConceptGraph.roadmap`), and the
  builds_on relations among those concepts.

- `GET /api/graph/relate-models` -> `{"selectable", "default", "models": [{"id", "label"}]}`:
  the models "Find relations" may use (the study-page models plus the relater's default).

- `POST /api/graph/concepts/{slug}/relate` (optional JSON body `{"model", "regenerate"}`; no
  body: the default model, incremental) -> `RelateOut`: finds relations for the concept
  with `app.state.concept_relater` (`teacher.concept_relating`: candidates by embedding and
  from Claude's proposals, one batched judge call, the accepted relations written with origin
  `model`) and returns what it did: the relations `added` (with their edges, and in a
  regenerate run the old relation each `replaces`), the candidates
  judged `none`, the `refused` ones (a builds_on cycle, or a relation that appeared meanwhile)
  with the manager's reason, the `unmatched` suggestions (concepts the graph lacks; none is
  created), the `dropped` candidates with why, counts and tokens, plus `nodes`/`edges` (the
  concept, the concepts its new relations reach, and those relations) for the canvas. With
  `regenerate`, the concept's unreviewed model-made relations are re-decided and replaced
  (`teacher.concept_relating`, Regenerate): `removed` lists the deleted ones not re-added, `kept`
  the reviewed or paper/user ones left alone, and `deleted_edge_ids` every relation id the run
  deleted (removed or replaced), so the canvas can drop those edges. `model` must be one of
  `GET /api/graph/relate-models` (else 422, before anything is spent). It runs
  synchronously (two short Claude calls, no document) with no database session open during
  the calls, like the glossary build; the run's record goes to
  `derived/_concept_relations/<slug>.json`. Unknown slug 404 (before anything is spent), missing
  credentials 503 (checked before any call), a run already in progress for the concept 409,
  an API or embedding failure 503, an unusable reply 502, a graph changed mid-run 409.

A node is `{"id", "slug", "name", "origin", "reviewed"}`; an edge is `{"id", "type", "from_id",
"to_id", "origin", "rationale", "reviewed", "reviewed_at", "redundant"}`. For builds_on,
`from_id` builds on `to_id` (to is the prerequisite); symmetric types are stored with
from_id < to_id. `redundant` marks a builds_on edge implied by a longer builds_on path
(`ConceptGraph.redundant_builds_on`); it is always false for the other types.

Errors use the app's handlers: an unknown slug is a 404, an empty or unusable search query a
422 with the manager's message (before anything is embedded), an unavailable full-text index a
503. A semantic search that cannot run is not an error: see `semantic` above.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher.claude_generator import write_record
from teacher.concept_graph import ALL_TYPES, ConceptGraph
from teacher.concept_hybrid_search import SEMANTIC_STATES, hybrid_search
from teacher.concept_relating import ConceptRelatingError, OldRelation, RelateRun, record_path
from teacher.concepts import BUILDS_ON
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Concept, ConceptRelation
from teacher.managers import (
    ConceptManager,
    ConceptRelationManager,
    SourceConceptManager,
    StudyHistoryManager,
)
from teacher.managers.concepts import DEFAULT_SEARCH_LIMIT
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.artifacts import GenerationModelsOut, ModelChoiceOut

router = APIRouter(prefix="/api/graph", tags=["concepts"])

DEFAULT_NODE_LIMIT = 300
MAX_NODE_LIMIT = 5000
SUMMARY_SNIPPET_CHARS = 160

Direction = Literal["out", "in", "symmetric"]
SemanticState = Literal["ok", "unavailable", "stale", "failed", "skipped"]
assert set(SemanticState.__args__) == set(SEMANTIC_STATES)  # type: ignore[attr-defined]
SIMILARITY_DIGITS = 4


class NodeOut(BaseModel):
    id: int
    slug: str
    name: str
    origin: str
    reviewed: bool


class EdgeOut(BaseModel):
    id: int
    type: str
    from_id: int
    to_id: int
    origin: str
    rationale: str
    reviewed: bool
    reviewed_at: str | None
    redundant: bool


class GraphOut(BaseModel):
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    total_nodes: int
    total_edges: int
    limit: int
    truncated: bool


class RelationOut(BaseModel):
    """A relation seen from one concept. `direction`: "out" when this concept builds on
    `other`, "in" when `other` builds on this concept, "symmetric" for the other types."""

    id: int
    type: str
    direction: Direction
    other: NodeOut
    rationale: str
    origin: str
    reviewed_at: str | None
    redundant: bool


class AppearsInOut(BaseModel):
    """A knowledge source the concept appears in, and how (introduces | uses | assumes)."""

    slug: str
    title: str
    role: str


class ConceptOut(BaseModel):
    id: int
    slug: str
    name: str
    summary: str
    aliases: list[str]
    origin: str
    reviewed_at: str | None
    created_at: str | None
    updated_at: str | None
    prerequisite_count: int
    unlocks_count: int
    relations: list[RelationOut]
    sources: list[AppearsInOut]


class NeighbourhoodOut(BaseModel):
    concept: NodeOut
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    neighbour_edges: list[EdgeOut]


class RoadmapOut(BaseModel):
    concept: NodeOut
    roadmap: list[NodeOut]
    edges: list[EdgeOut]


class SearchResultOut(NodeOut):
    summary: str
    # How the query matched lexically: exact | word | prefix | substring | fuzzy; which text it
    # matched (name | alias | summary) and, for a name or alias, that text as stored. All null
    # when the concept was found only by meaning.
    match_kind: str | None
    match_field: str | None
    matched: str | None
    # The query's cosine similarity to the concept, when found by meaning (else null).
    similarity: float | None


class SemanticOut(BaseModel):
    """Whether the search also searched meaning (`ok`), and if not, why (`reason`); for
    `stale`, how many concepts lack an up-to-date embedding and the command that fixes it."""

    status: SemanticState
    reason: str | None
    model: str
    floor: float
    stale_count: int | None
    concept_count: int | None
    fix: str | None


class SearchOut(BaseModel):
    query: str
    limit: int
    results: list[SearchResultOut]
    semantic: SemanticOut


class RelateIn(BaseModel):
    """The relate request body (all optional): `model`, one of `GET /api/graph/relate-models`
    (default: the relater's); `regenerate`: re-decide the concept's unreviewed model-made
    relations instead of only adding new ones."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    regenerate: StrictBool = False


class ReplacedOut(BaseModel):
    """The old relation a regenerate run deleted on the same pair before adding the new one."""

    relation_id: int
    type: str
    direction: Direction
    rationale: str
    type_changed: bool
    direction_changed: bool


class RelationAddedOut(BaseModel):
    """A relation the run added: its edge, its type and direction seen from the concept
    (`out`: the concept builds on `other`; `in`: `other` builds on it; `symmetric`), and in a
    regenerate run the old relation it `replaces` on the same pair (null if none)."""

    edge: EdgeOut
    type: str
    direction: Direction
    other: NodeOut
    rationale: str
    replaces: ReplacedOut | None


class RelationRemovedOut(BaseModel):
    """An old relation a regenerate run deleted and did not add again. `reason`: none (judged
    unrelated) | refused (the new verdict could not be written) | implied (a builds_on path
    already joins the pair); `detail` in words."""

    relation_id: int
    type: str
    direction: Direction
    other: NodeOut
    rationale: str
    reason: str
    detail: str


class RelationKeptOut(BaseModel):
    """A relation of the concept a regenerate run left alone, and why (`because`: "reviewed"
    and/or "origin paper" / "origin user")."""

    relation_id: int
    type: str
    direction: Direction
    other: NodeOut
    rationale: str
    origin: str
    reviewed_at: str | None
    because: list[str]


class RelationRefusedOut(BaseModel):
    """A relation the judge gave that the graph refused; `reason` is the manager's message and
    `cycle` the builds_on cycle (slugs) it would have closed, if that was why."""

    other: NodeOut
    type: str
    direction: Direction
    rationale: str
    reason: str
    cycle: list[str] | None


class JudgedNoneOut(BaseModel):
    other: NodeOut
    reason: str


class SuggestionOut(BaseModel):
    """A concept the model proposed that matched no concept of the graph (none is created).
    `slot`: builds_on | built_on_by | different_version_of | relates_to, seen from the concept."""

    slot: str
    name: str
    aliases: list[str]
    description: str


class DroppedOut(BaseModel):
    """A candidate that was not judged: `reason` self | related | judged | implied | cap."""

    id: int
    slug: str
    name: str
    reason: str
    detail: str


class RelateCountsOut(BaseModel):
    candidates: int
    embedding: int
    proposal: int
    both: int
    existing: int  # a regenerate run's current neighbours (always judged)
    judged: int
    added: int
    replaced: int  # added relations that replace an old one on the same pair
    none: int
    refused: int
    removed: int
    kept: int
    dropped: dict[str, int]


class RelateTokensOut(BaseModel):
    input: int
    output: int


class RelateOut(BaseModel):
    concept: NodeOut
    model: str
    embedding_model: str
    regenerate: bool
    judge_called: bool
    stale_embedded: int
    added: list[RelationAddedOut]
    removed: list[RelationRemovedOut]
    kept: list[RelationKeptOut]
    deleted_edge_ids: list[int]
    refused: list[RelationRefusedOut]
    judged_none: list[JudgedNoneOut]
    unmatched: list[SuggestionOut]
    dropped: list[DroppedOut]
    counts: RelateCountsOut
    tokens: RelateTokensOut
    nodes: list[NodeOut]
    edges: list[EdgeOut]


# -- helpers --------------------------------------------------------------------------------


class _Loaded:
    """Every concept and relation, read once, with the graph built from them."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.concepts = ConceptManager(session, settings).list()
        self.relations = ConceptRelationManager(session, settings).list()
        self.graph = ConceptGraph(self.concepts, self.relations)
        self.redundant = {relation.id for relation in self.graph.redundant_builds_on()}

    def edge(self, relation: ConceptRelation) -> EdgeOut:
        return EdgeOut(
            id=relation.id,
            type=relation.type,
            from_id=relation.from_id,
            to_id=relation.to_id,
            origin=relation.origin,
            rationale=relation.rationale,
            reviewed=relation.reviewed_at is not None,
            reviewed_at=to_iso(relation.reviewed_at),
            redundant=relation.id in self.redundant,
        )


def node_out(concept: Concept) -> NodeOut:
    return NodeOut(
        id=concept.id,
        slug=concept.slug,
        name=concept.name,
        origin=concept.origin,
        reviewed=concept.reviewed_at is not None,
    )


def snippet(text: str, limit: int = SUMMARY_SNIPPET_CHARS) -> str:
    """`text` on one line, cut at a word boundary to at most `limit` characters (with "…")."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "…"


def _concept(session: Session, settings: Settings, slug: str) -> Concept:
    return ConceptManager(session, settings).get_by_slug(slug)  # NotFoundError -> 404


class RunningSet:
    """The concepts with a relating run in progress: one run per concept at a time, so a
    double click cannot pay for the same run twice."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: set[str] = set()

    @contextmanager
    def claim(self, slug: str) -> Iterator[None]:
        """Hold `slug` for the block; HTTPException 409 if a run already holds it."""
        with self._lock:
            if slug in self._running:
                raise HTTPException(
                    409,
                    f"relations for concept {slug!r} are already being found; wait for that run "
                    "to finish",
                )
            self._running.add(slug)
        try:
            yield
        finally:
            with self._lock:
                self._running.discard(slug)


def relate_out(session: Session, settings: Settings, run: RelateRun) -> RelateOut:
    """The API view of a finished run, with nodes and edges read back from the database."""
    loaded = _Loaded(session, settings)
    ids = {run.focus.id}
    ids |= {a.other.id for a in run.added} | {r.other.id for r in run.refused}
    ids |= {j.candidate.concept.id for j in run.judged_none}
    ids |= {r.old.other.id for r in run.removed} | {k.other.id for k in run.kept}
    nodes = {
        concept.id: node_out(concept)
        for concept in session.scalars(select(Concept).where(Concept.id.in_(ids)))
    }
    relations = {relation.id: relation for relation in loaded.relations}
    gone = sorted(ids - set(nodes))
    lost = sorted(a.relation_id for a in run.added if a.relation_id not in relations)
    if gone or lost:
        raise ConceptRelatingError(
            "The relations were written, but the graph changed before they could be shown "
            f"(concept ids {gone} or relation ids {lost} are gone); reload the page. The run's "
            "record has what was written."
        )
    counts = run.counts()
    edges = [loaded.edge(relations[a.relation_id]) for a in run.added]

    def replaced(a: Any) -> ReplacedOut | None:
        if a.replaces is None:
            return None
        return ReplacedOut(
            relation_id=a.replaces.relation_id,
            type=a.replaces.type,
            direction=a.replaces.direction,
            rationale=a.replaces.rationale,
            type_changed=a.type_changed,
            direction_changed=a.direction_changed,
        )

    def kept(k: OldRelation) -> RelationKeptOut:
        return RelationKeptOut(
            relation_id=k.relation_id,
            type=k.type,
            direction=k.direction,
            other=nodes[k.other.id],
            rationale=k.rationale,
            origin=k.origin,
            reviewed_at=to_iso(k.reviewed_at),
            because=k.kept_because,
        )

    return RelateOut(
        concept=nodes[run.focus.id],
        model=run.model,
        embedding_model=run.embedding_model,
        regenerate=run.regenerate,
        judge_called=run.judge_called,
        stale_embedded=run.stale_embedded,
        added=[
            RelationAddedOut(
                edge=edge,
                type=a.type,
                direction=a.direction,
                other=nodes[a.other.id],
                rationale=a.rationale,
                replaces=replaced(a),
            )
            for a, edge in zip(run.added, edges, strict=True)
        ],
        removed=[
            RelationRemovedOut(
                relation_id=r.old.relation_id,
                type=r.old.type,
                direction=r.old.direction,
                other=nodes[r.old.other.id],
                rationale=r.old.rationale,
                reason=r.reason,
                detail=r.detail,
            )
            for r in run.removed
        ],
        kept=[kept(k) for k in run.kept],
        deleted_edge_ids=[o.relation_id for o in run.replaceable],
        refused=[
            RelationRefusedOut(
                other=nodes[r.other.id],
                type=r.type,
                direction=r.direction,
                rationale=r.rationale,
                reason=r.reason,
                cycle=list(r.cycle) if r.cycle is not None else None,
            )
            for r in run.refused
        ],
        judged_none=[
            JudgedNoneOut(other=nodes[j.candidate.concept.id], reason=j.rationale)
            for j in run.judged_none
        ],
        unmatched=[
            SuggestionOut(
                slot=p.slot, name=p.name, aliases=list(p.aliases), description=p.description
            )
            for p in run.unmatched
        ],
        dropped=[
            DroppedOut(
                id=d.candidate.concept.id,
                slug=d.candidate.concept.slug,
                name=d.candidate.concept.name,
                reason=d.reason,
                detail=d.detail,
            )
            for d in run.dropped
        ],
        counts=RelateCountsOut(
            candidates=counts["candidates"],
            embedding=counts["embedding"],
            proposal=counts["proposal"],
            both=counts["both"],
            existing=counts["existing"],
            judged=counts["judged"],
            added=counts["added"],
            replaced=counts["replaced"],
            none=counts["none"],
            refused=counts["refused"],
            removed=counts["removed"],
            kept=counts["kept"],
            dropped={
                key.removeprefix("dropped_"): n
                for key, n in counts.items()
                if key.startswith("dropped_")
            },
        ),
        tokens=RelateTokensOut(input=run.input_tokens, output=run.output_tokens),
        nodes=[nodes[run.focus.id]] + [nodes[a.other.id] for a in run.added],
        edges=edges,
    )


# -- endpoints ------------------------------------------------------------------------------


@router.get("")
def get_graph(
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_NODE_LIMIT)] = DEFAULT_NODE_LIMIT,
) -> GraphOut:
    """The concept graph, up to `limit` concepts (the most connected first)."""
    loaded = _Loaded(session, settings)
    degree = {concept.id: 0 for concept in loaded.concepts}
    for relation in loaded.relations:
        degree[relation.from_id] += 1
        degree[relation.to_id] += 1
    kept = sorted(loaded.concepts, key=lambda c: (-degree[c.id], c.slug))[:limit]
    kept_ids = {concept.id for concept in kept}
    return GraphOut(
        nodes=[node_out(concept) for concept in sorted(kept, key=lambda c: c.slug)],
        edges=[
            loaded.edge(relation)
            for relation in loaded.relations
            if relation.from_id in kept_ids and relation.to_id in kept_ids
        ],
        total_nodes=len(loaded.concepts),
        total_edges=len(loaded.relations),
        limit=limit,
        truncated=len(loaded.concepts) > limit,
    )


@router.get("/search")
def search_concepts(
    q: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> SearchOut:
    """Search names, aliases and summaries, and meaning; one ranked list (see the module
    docstring and `teacher.concept_hybrid_search.merge_hits`). Each result says how it was
    found; `semantic` says whether meaning was searched, and if not, why. Logs a
    `graph_searched` study event."""
    result = hybrid_search(session, settings, q, limit, request.app.state.search_embedder)
    semantic = result.semantic
    StudyHistoryManager(session, settings).log(
        "graph_searched",
        payload={"query": q, "results": len(result.hits), "semantic": semantic.status},
    )
    return SearchOut(
        query=q,
        limit=limit,
        results=[
            SearchResultOut(
                **node_out(hit.concept).model_dump(),
                summary=snippet(hit.concept.summary),
                match_kind=hit.lexical.kind if hit.lexical else None,
                match_field=hit.lexical.field if hit.lexical else None,
                matched=hit.lexical.matched if hit.lexical else None,
                similarity=round(hit.similarity, SIMILARITY_DIGITS)
                if hit.similarity is not None
                else None,
            )
            for hit in result.hits
        ],
        semantic=SemanticOut(
            status=semantic.status,
            reason=semantic.reason,
            model=semantic.model,
            floor=semantic.floor,
            stale_count=semantic.stale_count,
            concept_count=semantic.concept_count,
            fix=semantic.fix,
        ),
    )


@router.get("/concepts/{slug}")
def get_concept(slug: str, session: SessionDep, settings: SettingsDep) -> ConceptOut:
    """One concept with its aliases, relations and prerequisite/unlock counts."""
    concept = _concept(session, settings, slug)
    loaded = _Loaded(session, settings)
    by_id = {c.id: c for c in loaded.concepts}
    relations: list[RelationOut] = []
    for relation in loaded.relations:
        if concept.id not in (relation.from_id, relation.to_id):
            continue
        other_id = relation.to_id if relation.from_id == concept.id else relation.from_id
        direction: Direction
        if relation.type != BUILDS_ON:
            direction = "symmetric"
        elif relation.from_id == concept.id:
            direction = "out"
        else:
            direction = "in"
        relations.append(
            RelationOut(
                id=relation.id,
                type=relation.type,
                direction=direction,
                other=node_out(by_id[other_id]),
                rationale=relation.rationale,
                origin=relation.origin,
                reviewed_at=to_iso(relation.reviewed_at),
                redundant=relation.id in loaded.redundant,
            )
        )
    relations.sort(key=lambda r: (r.type, r.direction, r.other.slug))
    return ConceptOut(
        id=concept.id,
        slug=concept.slug,
        name=concept.name,
        summary=concept.summary,
        aliases=[alias.alias for alias in concept.aliases],
        origin=concept.origin,
        reviewed_at=to_iso(concept.reviewed_at),
        created_at=to_iso(concept.created_at),
        updated_at=to_iso(concept.updated_at),
        prerequisite_count=len(loaded.graph.prerequisites(concept)),
        unlocks_count=len(loaded.graph.unlocks(concept)),
        relations=relations,
        sources=[
            AppearsInOut(slug=link.source.slug, title=link.source.title, role=link.role)
            for link in SourceConceptManager(session, settings).for_concept(concept)
        ],
    )


@router.get("/concepts/{slug}/neighbourhood")
def get_neighbourhood(slug: str, session: SessionDep, settings: SettingsDep) -> NeighbourhoodOut:
    """The concept's direct relations, the concepts at their other ends, and those concepts'
    other relations."""
    concept = _concept(session, settings, slug)
    loaded = _Loaded(session, settings)
    neighbours = {other.id: other for other, _type in loaded.graph.neighbours(concept, ALL_TYPES)}
    edges: list[EdgeOut] = []
    neighbour_edges: list[EdgeOut] = []
    for relation in loaded.relations:
        ends = (relation.from_id, relation.to_id)
        if concept.id in ends:
            edges.append(loaded.edge(relation))
        elif any(end in neighbours for end in ends):
            neighbour_edges.append(loaded.edge(relation))
    return NeighbourhoodOut(
        concept=node_out(concept),
        nodes=[node_out(other) for other in sorted(neighbours.values(), key=lambda c: c.slug)],
        edges=edges,
        neighbour_edges=neighbour_edges,
    )


@router.get("/concepts/{slug}/roadmap")
def get_roadmap(slug: str, session: SessionDep, settings: SettingsDep) -> RoadmapOut:
    """What to study to reach the concept, in study order (the concept last)."""
    concept = _concept(session, settings, slug)
    loaded = _Loaded(session, settings)
    roadmap = loaded.graph.roadmap(concept)
    ids = {c.id for c in roadmap}
    return RoadmapOut(
        concept=node_out(concept),
        roadmap=[node_out(c) for c in roadmap],
        edges=[
            loaded.edge(relation)
            for relation in loaded.relations
            if relation.type == BUILDS_ON and relation.from_id in ids and relation.to_id in ids
        ],
    )


@router.get("/relate-models")
def list_relate_models(request: Request) -> GenerationModelsOut:
    """The models "Find relations" and "Regenerate relations" may use, and the default."""
    relater = request.app.state.concept_relater
    return GenerationModelsOut(
        selectable=True,
        default=relater.model,
        models=[ModelChoiceOut(id=id_, label=label) for id_, label in relater.model_choices()],
    )


@router.post("/concepts/{slug}/relate")
def relate_concept(
    slug: str,
    request: Request,
    settings: SettingsDep,
    body: Annotated[RelateIn | None, Body()] = None,
) -> RelateOut:
    """Find relations for the concept now and write them, or with `regenerate` re-decide its
    unreviewed model-made ones (see the module docstring)."""
    body = body if body is not None else RelateIn()
    relater = request.app.state.concept_relater
    with session_scope(settings) as session:
        _concept(session, settings, slug)  # 404 before anything is spent
    model = relater.choose_model(body.model)  # ValueError -> 422, before anything is spent
    relater.check_available()  # 503 for missing credentials, before any call
    with request.app.state.relating.claim(slug):
        with session_scope(settings) as session:
            StudyHistoryManager(session, settings).log(
                "relations_requested",
                concept=_concept(session, settings, slug),
                payload={"regenerate": body.regenerate, "model": model},
            )
        # no session is open here
        run = relater.relate(settings, slug, regenerate=body.regenerate, model=model)
    write_record(record_path(settings, slug), run.record("web"))
    with session_scope(settings) as session:
        return relate_out(session, settings, run)


__all__ = [
    "DEFAULT_NODE_LIMIT",
    "AppearsInOut",
    "MAX_NODE_LIMIT",
    "ConceptOut",
    "EdgeOut",
    "GraphOut",
    "NeighbourhoodOut",
    "NodeOut",
    "RelateIn",
    "RelateOut",
    "RoadmapOut",
    "RunningSet",
    "SearchOut",
    "SemanticOut",
    "relate_out",
    "router",
    "snippet",
]
