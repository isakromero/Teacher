"""Concept graph API (`/api/graph`): the graph with its node limit, one concept's details, its
neighbourhood and roadmap, full-text search (here with the default app's embedder, which has no
key, so names only; hybrid search: tests/web/test_search_api.py), and every error path. All
read-only."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import ConceptManager, ConceptRelationManager
from teacher.web.routers.concepts import DEFAULT_NODE_LIMIT, MAX_NODE_LIMIT, router, snippet

Json = dict[str, Any]

LONG_SUMMARY = (
    "Attention weighs every value by how well its key matches the query, using a softmax over "
    "scaled dot products $QK^T/\\sqrt{d}$, so each output mixes the inputs it needs most; the "
    "weights are learned end to end."
)

# slug: (name, origin, reviewed, aliases)
CONCEPTS: dict[str, tuple[str, str, bool, list[str]]] = {
    "vectors": ("Vectors", "user", True, []),
    "dot": ("Dot product", "model", False, ["inner product"]),
    "softmax": ("Softmax", "paper", False, []),
    "attention": ("Attention", "paper", True, ["attention mechanism", "AM"]),
    "transformer": ("Transformer", "paper", False, []),
    "rnn": ("Recurrent network", "paper", False, ["RNN"]),
    "lstm": ("LSTM", "model", True, []),
    "orphan": ("Orphan idea", "user", False, []),
}
# (type, from, to, reviewed)
RELATIONS: list[tuple[str, str, str, bool]] = [
    ("builds_on", "attention", "dot", True),
    ("builds_on", "attention", "softmax", False),
    ("builds_on", "dot", "vectors", True),
    ("builds_on", "attention", "vectors", False),  # redundant: attention -> dot -> vectors
    ("builds_on", "transformer", "attention", False),
    ("relates_to", "rnn", "attention", True),  # stored with from_id < to_id
    ("different_version_of", "lstm", "rnn", False),
]


@pytest.fixture
def seeded(settings: Settings) -> dict[str, int]:
    """The concepts and relations above, committed; returns slug -> concept id."""
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        relations = ConceptRelationManager(session, settings)
        made = {}
        for slug, (name, origin, reviewed, aliases) in CONCEPTS.items():
            summary = LONG_SUMMARY if slug == "attention" else f"About {name.lower()}."
            concept = concepts.create(name, summary, origin, aliases=aliases, slug=slug)
            if reviewed:
                concepts.mark_reviewed(concept)
            made[slug] = concept
        for type, a, b, reviewed in RELATIONS:
            relation = relations.add(type, made[a], made[b], f"{a} and {b} are linked.", "model")
            if reviewed:
                relations.mark_reviewed(relation)
        return {slug: concept.id for slug, concept in made.items()}


def get(client: TestClient, path: str, status: int = 200) -> Json:
    response = client.get(path)
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def edge_key(edge: Json, ids: dict[str, int]) -> tuple[str, str, str]:
    slugs = {v: k for k, v in ids.items()}
    return (edge["type"], slugs[edge["from_id"]], slugs[edge["to_id"]])


# -- the graph --------------------------------------------------------------------------------


def test_graph_empty(client: TestClient) -> None:
    assert get(client, "/api/graph") == {
        "nodes": [],
        "edges": [],
        "total_nodes": 0,
        "total_edges": 0,
        "limit": DEFAULT_NODE_LIMIT,
        "truncated": False,
    }


def test_graph(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph")
    assert data["total_nodes"] == len(CONCEPTS) and data["total_edges"] == len(RELATIONS)
    assert data["truncated"] is False and data["limit"] == 300
    assert [n["slug"] for n in data["nodes"]] == sorted(CONCEPTS)
    attention = next(n for n in data["nodes"] if n["slug"] == "attention")
    assert attention == {
        "id": seeded["attention"], "slug": "attention", "name": "Attention", "origin": "paper",
        "reviewed": True,
    }
    assert {n["slug"]: n["reviewed"] for n in data["nodes"]} == {
        slug: reviewed for slug, (_name, _origin, reviewed, _aliases) in CONCEPTS.items()
    }
    edges = {edge_key(e, seeded): e for e in data["edges"]}
    assert len(edges) == len(RELATIONS)
    redundant = [key for key, e in edges.items() if e["redundant"]]
    assert redundant == [("builds_on", "attention", "vectors")]
    # builds_on keeps its direction (from = dependent, to = prerequisite) ...
    assert ("builds_on", "attention", "dot") in edges
    # ... symmetric relations are stored low id first, whichever way they were given
    assert ("relates_to", "attention", "rnn") in edges
    assert ("different_version_of", "rnn", "lstm") in edges
    reviewed = edges[("builds_on", "attention", "dot")]
    assert reviewed["reviewed"] is True and reviewed["reviewed_at"].endswith("+00:00")
    unreviewed = edges[("builds_on", "attention", "softmax")]
    assert unreviewed["reviewed"] is False and unreviewed["reviewed_at"] is None
    assert unreviewed == {
        "id": unreviewed["id"], "type": "builds_on", "from_id": seeded["attention"],
        "to_id": seeded["softmax"], "origin": "model",
        "rationale": "attention and softmax are linked.", "reviewed": False, "reviewed_at": None,
        "redundant": False,
    }


def test_graph_limit_keeps_the_most_connected_and_says_so(
    client: TestClient, seeded: dict[str, int]
) -> None:
    # Degrees: attention 5; dot, rnn, vectors 2 each (ties by slug); the rest fewer.
    data = get(client, "/api/graph?limit=2")
    assert data["truncated"] is True and data["limit"] == 2
    assert data["total_nodes"] == len(CONCEPTS) and data["total_edges"] == len(RELATIONS)
    assert [n["slug"] for n in data["nodes"]] == ["attention", "dot"]
    assert [edge_key(e, seeded) for e in data["edges"]] == [("builds_on", "attention", "dot")]

    exact = get(client, f"/api/graph?limit={len(CONCEPTS)}")
    assert exact["truncated"] is False and len(exact["nodes"]) == len(CONCEPTS)


@pytest.mark.parametrize("limit", ["0", "-1", str(MAX_NODE_LIMIT + 1), "lots"])
def test_graph_bad_limit_is_422(client: TestClient, limit: str) -> None:
    data = get(client, f"/api/graph?limit={limit}", status=422)
    assert set(data) == {"detail"} and data["detail"].startswith("limit:")


# -- one concept ------------------------------------------------------------------------------


def test_concept_details(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph/concepts/attention")
    assert {k: data[k] for k in ("id", "slug", "name", "summary", "origin")} == {
        "id": seeded["attention"], "slug": "attention", "name": "Attention",
        "summary": LONG_SUMMARY, "origin": "paper",
    }
    assert data["aliases"] == ["AM", "attention mechanism"]  # in the stored (normalised) order
    for key in ("reviewed_at", "created_at", "updated_at"):
        assert data[key].endswith("+00:00"), key
    assert data["prerequisite_count"] == 3  # dot, softmax, vectors (transitively)
    assert data["unlocks_count"] == 1  # transformer
    relations = [
        (r["type"], r["direction"], r["other"]["slug"], r["redundant"], r["reviewed_at"] is not None)
        for r in data["relations"]
    ]
    assert relations == [
        ("builds_on", "in", "transformer", False, False),
        ("builds_on", "out", "dot", False, True),
        ("builds_on", "out", "softmax", False, False),
        ("builds_on", "out", "vectors", True, False),
        ("relates_to", "symmetric", "rnn", False, True),
    ]
    first = data["relations"][0]
    assert first["other"] == {
        "id": seeded["transformer"], "slug": "transformer", "name": "Transformer",
        "origin": "paper", "reviewed": False,
    }
    assert first["rationale"] == "transformer and attention are linked." and first["origin"] == "model"


def test_concept_details_of_a_leaf_and_an_orphan(client: TestClient, seeded: dict[str, int]) -> None:
    vectors = get(client, "/api/graph/concepts/vectors")
    assert vectors["prerequisite_count"] == 0 and vectors["unlocks_count"] == 2  # dot, attention
    assert vectors["aliases"] == []
    assert [(r["direction"], r["other"]["slug"]) for r in vectors["relations"]] == [
        ("in", "attention"), ("in", "dot"),
    ]
    lstm = get(client, "/api/graph/concepts/lstm")
    assert [(r["type"], r["direction"], r["other"]["slug"]) for r in lstm["relations"]] == [
        ("different_version_of", "symmetric", "rnn"),
    ]
    orphan = get(client, "/api/graph/concepts/orphan")
    assert orphan["relations"] == [] and orphan["reviewed_at"] is None
    assert orphan["prerequisite_count"] == 0 and orphan["unlocks_count"] == 0


@pytest.mark.parametrize("part", ["", "/neighbourhood", "/roadmap"])
def test_unknown_concept_is_404(client: TestClient, seeded: dict[str, int], part: str) -> None:
    data = get(client, f"/api/graph/concepts/no-such-thing{part}", status=404)
    assert data == {"detail": "no concept with slug 'no-such-thing'"}


# -- neighbourhood ----------------------------------------------------------------------------


def test_neighbourhood(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph/concepts/dot/neighbourhood")
    assert data["concept"]["slug"] == "dot" and data["concept"]["reviewed"] is False
    assert [n["slug"] for n in data["nodes"]] == ["attention", "vectors"]
    assert sorted(edge_key(e, seeded) for e in data["edges"]) == [
        ("builds_on", "attention", "dot"),
        ("builds_on", "dot", "vectors"),
    ]
    # Every other relation of attention or vectors, so a drawing can connect them to what it has.
    assert sorted(edge_key(e, seeded) for e in data["neighbour_edges"]) == [
        ("builds_on", "attention", "softmax"),
        ("builds_on", "attention", "vectors"),
        ("builds_on", "transformer", "attention"),
        ("relates_to", "attention", "rnn"),
    ]
    redundant = [e for e in data["neighbour_edges"] if e["redundant"]]
    assert [edge_key(e, seeded) for e in redundant] == [("builds_on", "attention", "vectors")]


def test_neighbourhood_of_an_orphan(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph/concepts/orphan/neighbourhood")
    assert data["concept"]["slug"] == "orphan"
    assert data["nodes"] == [] and data["edges"] == [] and data["neighbour_edges"] == []


# -- roadmap ----------------------------------------------------------------------------------


def test_roadmap(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph/concepts/transformer/roadmap")
    assert data["concept"]["slug"] == "transformer"
    # Study order: after every prerequisite, smallest slug first among those ready.
    assert [n["slug"] for n in data["roadmap"]] == [
        "softmax", "vectors", "dot", "attention", "transformer",
    ]
    assert sorted(edge_key(e, seeded) for e in data["edges"]) == [
        ("builds_on", "attention", "dot"),
        ("builds_on", "attention", "softmax"),
        ("builds_on", "attention", "vectors"),
        ("builds_on", "dot", "vectors"),
        ("builds_on", "transformer", "attention"),
    ]
    assert all(e["type"] == "builds_on" for e in data["edges"])


def test_roadmap_of_a_concept_without_prerequisites(
    client: TestClient, seeded: dict[str, int]
) -> None:
    data = get(client, "/api/graph/concepts/rnn/roadmap")  # related to attention, builds on nothing
    assert [n["slug"] for n in data["roadmap"]] == ["rnn"]
    assert data["edges"] == []


# -- search -----------------------------------------------------------------------------------


def test_search(client: TestClient, seeded: dict[str, int]) -> None:
    data = get(client, "/api/graph/search?q=attention")
    assert data["query"] == "attention" and data["limit"] == 20
    assert [r["slug"] for r in data["results"]][0] == "attention"  # the name match ranks first
    first = data["results"][0]
    assert set(first) == {
        "id", "slug", "name", "origin", "reviewed", "summary",
        "match_kind", "match_field", "matched", "similarity",
    }
    assert first["similarity"] is None  # no OpenAI key: names only, and the response says so
    assert data["semantic"]["status"] == "unavailable"
    assert (first["match_kind"], first["match_field"], first["matched"]) == (
        "exact", "name", "Attention",
    )
    assert first["reviewed"] is True and first["origin"] == "paper"
    assert len(first["summary"]) <= 160 and first["summary"].endswith("…")
    assert LONG_SUMMARY.startswith(first["summary"][:-1])
    # aliases are searched too; short summaries are returned whole
    rnn = get(client, "/api/graph/search?q=RNN")["results"]
    assert [(r["slug"], r["summary"]) for r in rnn] == [("rnn", "About recurrent network.")]
    assert (rnn[0]["match_kind"], rnn[0]["match_field"], rnn[0]["matched"]) == (
        "exact", "alias", "RNN",
    )
    # prefix, substring and typo-tolerant matches say how they matched
    kinds = {
        q: [(r["slug"], r["match_kind"]) for r in get(client, f"/api/graph/search?q={q}")["results"]]
        for q in ("atten", "ttentio", "atention")
    }
    assert ("attention", "prefix") in kinds["atten"]
    assert ("attention", "substring") in kinds["ttentio"]
    assert ("attention", "fuzzy") in kinds["atention"]
    assert get(client, "/api/graph/search?q=zebra")["results"] == []
    assert len(get(client, "/api/graph/search?q=about&limit=2")["results"]) == 2


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("q=", "search query must not be empty"),
        ("q=%20%20", "search query must not be empty"),
        ("q=(%20*", "search query '( *' has no letters or digits to search for"),
        ("q=attention&limit=0", "limit must be a positive integer, got 0"),
        ("", "q: Field required"),
        ("q=attention&limit=x", "limit: Input should be a valid integer, unable to parse string as an integer"),
    ],
)
def test_search_rejects_bad_queries(client: TestClient, query: str, detail: str) -> None:
    assert get(client, f"/api/graph/search?{query}", status=422) == {"detail": detail}


def test_search_without_the_index_is_503(
    client: TestClient, settings: Settings, seeded: dict[str, int]
) -> None:
    with session_scope(settings) as session:
        session.execute(text("DROP TABLE concept_search"))
    data = get(client, "/api/graph/search?q=attention", status=503)
    assert "concept_search full-text table is missing" in data["detail"]
    # Nothing else depends on the index.
    assert get(client, "/api/graph")["total_nodes"] == len(CONCEPTS)


def test_snippet() -> None:
    assert snippet("Short.") == "Short."
    assert snippet("  spread\n over   lines ") == "spread over lines"
    cut = snippet("word " * 100)
    assert len(cut) <= 160 and cut.endswith("word…")
    assert snippet("x" * 300) == "x" * 159 + "…"  # no space to cut at: cut mid-word
    assert snippet("a" * 10 + ", tail", limit=12) == "a" * 10 + "…"


# -- read-only ----------------------------------------------------------------------------------


def test_graph_api_only_reads(client: TestClient, seeded: dict[str, int]) -> None:
    """Every graph endpoint is a GET except the relate POST (tests/web/test_relate_api.py)."""
    relate = "/api/graph/concepts/{slug}/relate"
    paths = client.app.openapi()["paths"]
    graph_paths = {path: set(ops) for path, ops in paths.items() if path.startswith("/api/graph")}
    assert len(graph_paths) == 7 and graph_paths.pop(relate) == {"post"}
    assert graph_paths["/api/graph/relate-models"] == {"get"}
    assert all(ops == {"get"} for ops in graph_paths.values()), graph_paths
    methods = {route.path: route.methods for route in router.routes}
    assert methods.pop(f"/api/graph{relate.removeprefix('/api/graph')}") == {"POST"}
    assert methods and all(m == {"GET"} for m in methods.values())
    before = get(client, "/api/graph/concepts/attention")
    for path in ("/api/graph", "/api/graph/concepts/attention/neighbourhood",
                 "/api/graph/concepts/attention/roadmap", "/api/graph/search?q=attention"):
        get(client, path)
    assert get(client, "/api/graph/concepts/attention") == before


def test_concept_details_list_the_sources_it_appears_in(
    client: TestClient, settings: Settings, seeded: dict[str, int], upload_source
) -> None:
    from teacher.managers import KnowledgeSourceManager, LinkSpec, SourceConceptManager

    upload_source(slug="zeta", title="Zeta paper")
    upload_source(slug="alpha", title="Alpha paper")
    assert get(client, "/api/graph/concepts/attention")["sources"] == []
    with session_scope(settings) as session:
        sources = KnowledgeSourceManager(session, settings)
        attention = ConceptManager(session, settings).get_by_slug("attention")
        links = SourceConceptManager(session, settings)
        links.replace(sources.get_by_slug("zeta"), [LinkSpec(attention, "uses", "exact", "A")])
        links.replace(sources.get_by_slug("alpha"),
                      [LinkSpec(attention, "introduces", "created", "attention")])
    assert get(client, "/api/graph/concepts/attention")["sources"] == [
        {"slug": "alpha", "title": "Alpha paper", "role": "introduces"},
        {"slug": "zeta", "title": "Zeta paper", "role": "uses"},
    ]
