"""ConceptGraph traversals, the roadmap rule, and the shared cycle detector."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from teacher.concept_graph import ConceptGraph
from teacher.concepts import find_cycle
from teacher.config import Settings
from teacher.db.models import Concept
from teacher.managers import ConceptManager, ConceptRelationManager, CycleError, NotFoundError

WHY = "It is needed first."


@pytest.fixture
def concepts(session: Session, settings: Settings) -> ConceptManager:
    return ConceptManager(session, settings)


@pytest.fixture
def relations(session: Session, settings: Settings) -> ConceptRelationManager:
    return ConceptRelationManager(session, settings)


@pytest.fixture
def build(concepts: ConceptManager, relations: ConceptRelationManager):  # noqa: ANN201
    """`build("t>a t>b a>c", "x~y")`: `t>a` is t builds_on a, `x~y` relates_to, `x=y`
    different_version_of. Concepts are created on first mention; returns slug -> Concept."""

    def make(*specs: str) -> dict[str, Concept]:
        made: dict[str, Concept] = {}

        def get(slug: str) -> Concept:
            if slug not in made:
                made[slug] = concepts.create(slug, f"About {slug}.", "user", slug=slug)
            return made[slug]

        for spec in specs:
            for edge in spec.split():
                for symbol, type in ((">", "builds_on"), ("~", "relates_to"), ("=", "different_version_of")):
                    if symbol in edge:
                        a, b = edge.split(symbol)
                        relations.add(type, get(a), get(b), WHY, "user")
                        break
                else:
                    get(edge)
        return made

    return make


def slugs(items: list[Concept]) -> list[str]:
    return [c.slug for c in items]


# --- find_cycle ---------------------------------------------------------------------------


def test_find_cycle() -> None:
    assert find_cycle({}) is None
    assert find_cycle({1: [2], 2: [3], 3: []}) is None
    assert find_cycle({1: [2, 3], 2: [3]}) is None  # a diamond is not a cycle
    assert find_cycle({1: [1]}) == [1, 1]
    assert find_cycle({3: [1], 1: [2], 2: [3]}) == [1, 2, 3, 1]
    assert find_cycle({1: [2], 2: [3], 3: [2]}) == [2, 3, 2]  # only the cycle, not the lead-in
    assert find_cycle({"b": ["a"], "a": ["b"]}) == ["a", "b", "a"]


def test_find_cycle_handles_deep_graphs() -> None:
    chain = {i: [i + 1] for i in range(20_000)}
    assert find_cycle(chain) is None
    chain[20_000] = [0]
    cycle = find_cycle(chain)
    assert cycle is not None and len(cycle) == 20_002 and cycle[0] == cycle[-1] == 0


# --- traversals ---------------------------------------------------------------------------


def test_prerequisites_are_transitive_in_study_order(build, session: Session) -> None:
    c = build("t>m t>b m>z b>z z>a")
    graph = ConceptGraph.load(session)
    assert slugs(graph.prerequisites(c["t"])) == ["a", "z", "b", "m"]  # ties by slug
    assert graph.prerequisites(c["a"]) == []
    assert slugs(graph.roadmap(c["t"])) == ["a", "z", "b", "m", "t"]


def test_roadmap_stops_at_known_concepts(build, session: Session) -> None:
    c = build("t>k k>below t>u")
    graph = ConceptGraph.load(session)
    assert slugs(graph.roadmap(c["t"], known=[c["k"]])) == ["u", "t"]  # k covers what it builds on
    assert slugs(graph.roadmap(c["t"], known=[c["below"]])) == ["k", "u", "t"]
    assert graph.roadmap(c["t"], known=[c["t"]]) == []


def test_roadmap_keeps_a_prerequisite_also_needed_through_an_unknown_concept(
    build, session: Session
) -> None:
    c = build("t>k t>u k>shared u>shared k>only-k")
    graph = ConceptGraph.load(session)
    assert slugs(graph.roadmap(c["t"], known=[c["k"]])) == ["shared", "u", "t"]


def test_roadmap_orders_through_known_concepts(build, session: Session) -> None:
    # alpha builds on zeta only through the known k; zeta is on the roadmap via t > zeta.
    c = build("t>alpha alpha>k k>zeta t>zeta")
    graph = ConceptGraph.load(session)
    assert slugs(graph.roadmap(c["t"], known=[c["k"]])) == ["zeta", "alpha", "t"]


def test_unlocks_and_neighbours(build, session: Session) -> None:
    c = build("a>base b>base c>a", "base~x base=y z~base")
    graph = ConceptGraph.load(session)
    assert slugs(graph.unlocks(c["base"])) == ["a", "b"]  # direct only
    assert graph.unlocks(c["c"]) == []
    assert [(n.slug, t) for n, t in graph.neighbours(c["base"])] == [
        ("x", "relates_to"), ("y", "different_version_of"), ("z", "relates_to"),
    ]
    assert [n.slug for n, _ in graph.neighbours(c["base"], "relates_to")] == ["x", "z"]
    assert [n.slug for n, _ in graph.neighbours(c["base"], "builds_on")] == ["a", "b"]
    assert [n.slug for n, _ in graph.neighbours(c["a"], "builds_on")] == ["base", "c"]
    assert [(n.slug, t) for n, t in graph.neighbours(c["base"], "all")] == [
        ("a", "builds_on"), ("b", "builds_on"), ("x", "relates_to"),
        ("y", "different_version_of"), ("z", "relates_to"),
    ]
    with pytest.raises(ValueError, match="invalid relation type 'parent'"):
        graph.neighbours(c["base"], "parent")


def test_redundant_builds_on(build, session: Session, relations: ConceptRelationManager) -> None:
    c = build("a>b b>c c>d a>d b>d x>y")
    a_d = relations.between(c["a"], c["d"])
    b_d = relations.between(c["b"], c["d"])
    graph = ConceptGraph.load(session)
    assert graph.redundant_builds_on() == [a_d, b_d]


def test_lookups_and_unknown_concepts(build, concepts: ConceptManager, session: Session) -> None:
    c = build("a>b")
    graph = ConceptGraph.load(session)
    assert slugs(graph.concepts) == ["a", "b"] and graph.by_slug("b") is c["b"]
    with pytest.raises(NotFoundError, match="no concept with slug 'zz'"):
        graph.by_slug("zz")
    late = concepts.create("late", "Created after loading.", "user")
    with pytest.raises(NotFoundError, match="'late' is not in this graph"):
        graph.prerequisites(late)
    with pytest.raises(NotFoundError):
        graph.roadmap(c["a"], known=[late])
    with pytest.raises(TypeError, match="expected a Concept"):
        graph.unlocks("a")  # type: ignore[arg-type]


def test_loading_is_read_only(build, session: Session) -> None:
    c = build("a>b b>c", "a~c")
    session.commit()
    graph = ConceptGraph.load(session)
    graph.roadmap(c["a"])
    graph.neighbours(c["a"], "all")
    graph.redundant_builds_on()
    assert not session.new and not session.dirty and not session.deleted


def test_loading_a_cyclic_graph_raises(build, session: Session) -> None:
    c = build("a>b b>c")
    # Bypass the write path: the database alone cannot refuse a cycle.
    session.execute(
        text(
            "INSERT INTO concept_relations (type, from_id, to_id, low_id, high_id, rationale, "
            "origin) VALUES ('builds_on', :c, :a, :a, :c, 'r', 'user')"
        ),
        {"a": c["a"].id, "c": c["c"].id},
    )
    with pytest.raises(CycleError, match="stored builds_on relations contain a cycle: a -> b -> c -> a") as info:
        ConceptGraph.load(session)
    assert info.value.cycle == ("a", "b", "c", "a")
