"""teacher.source_graph: the builds_on and related edges between sources, the weight, the
ranking, "not analysed", readiness (with study order and the introducing sources' pages), the
citations with each source's reference state (unknown, never "none", until references are
stored), the checks on the input, and loading it all from the database."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Concept, KnowledgeSource
from teacher.managers import (
    ArtifactManager,
    ConceptManager,
    ConceptRelationManager,
    KnowledgeSourceManager,
    LinkSpec,
    NotFoundError,
    SourceConceptManager,
)
from teacher.managers.source_references import ReferenceSpec, SourceReferenceManager
from teacher.source_graph import (
    NEVER_RUN,
    ROLE_WEIGHTS,
    Citations,
    ConceptRef,
    PageRef,
    SourceGraph,
    SourceRef,
    ReferenceState,
    readiness,
    ready_pages,
    rules,
    source_graph,
    source_relations,
)

A = SourceRef(1, "a", "Alpha paper", "paper")
B = SourceRef(2, "b", "Beta paper", "paper")
C = SourceRef(3, "c", "Gamma notes", "note")
D = SourceRef(4, "d", "Delta (no concepts)", "paper")
X = ConceptRef(10, "x", "X")
Y = ConceptRef(11, "y", "Y")
Z = ConceptRef(12, "z", "Z")
W = ConceptRef(13, "w", "W")


def small_graph() -> SourceGraph:
    """A introduces X, uses Y, assumes Z; B introduces Y and Z, uses X; C assumes X, uses W;
    D has no links."""
    return SourceGraph(
        [A, B, C, D],
        [
            (1, X, "introduces"), (1, Y, "uses"), (1, Z, "assumes"),
            (2, Y, "introduces"), (2, Z, "introduces"), (2, X, "uses"),
            (3, X, "assumes"), (3, W, "uses"),
        ],
    )


def test_builds_on_edges_carry_the_concepts_with_the_dependents_role() -> None:
    graph = small_graph()
    edges = [(e.dependent.slug, e.foundation.slug,
              [(c.slug, role) for c, role in e.concepts]) for e in graph.builds_on_edges]
    assert edges == [
        ("a", "b", [("y", "uses"), ("z", "assumes")]),  # uses before assumes
        ("b", "a", [("x", "uses")]),  # two sources can build on each other
        ("c", "a", [("x", "assumes")]),
    ]
    assert [e.foundation.slug for e in graph.builds_on(A)] == ["b"]
    # ranked: more concepts first, then title
    assert [e.dependent.slug for e in graph.built_on_by(A)] == ["b", "c"]
    assert graph.built_on_by(C) == [] and graph.builds_on(D) == []


def test_related_weight_is_the_cosine_of_role_weighted_vectors() -> None:
    graph = small_graph()
    w = ROLE_WEIGHTS
    length = {
        "a": math.sqrt(w["introduces"] ** 2 + w["uses"] ** 2 + w["assumes"] ** 2),
        "b": math.sqrt(2 * w["introduces"] ** 2 + w["uses"] ** 2),
        "c": math.sqrt(w["assumes"] ** 2 + w["uses"] ** 2),
    }
    ab = (w["introduces"] * w["uses"] + w["uses"] * w["introduces"]
          + w["assumes"] * w["introduces"]) / (length["a"] * length["b"])
    ac = w["introduces"] * w["assumes"] / (length["a"] * length["c"])
    bc = w["uses"] * w["assumes"] / (length["b"] * length["c"])
    edges = {(e.a.slug, e.b.slug): e for e in graph.related_edges}
    assert set(edges) == {("a", "b"), ("a", "c"), ("b", "c")}  # smaller slug first
    assert edges[("a", "b")].weight == pytest.approx(ab)
    assert edges[("a", "c")].weight == pytest.approx(ac)
    assert edges[("b", "c")].weight == pytest.approx(bc)
    assert [e.weight for e in graph.related_edges] == sorted(
        (e.weight for e in graph.related_edges), reverse=True
    )
    # heaviest shared concept first (3*2 for x and y, then 1*3 for z), then by name
    assert [(s.concept.slug, s.role_a, s.role_b) for s in edges[("a", "b")].shared] == [
        ("x", "introduces", "uses"), ("y", "uses", "introduces"), ("z", "assumes", "introduces"),
    ]
    assert [(other.slug, round(e.weight, 6)) for other, e in graph.related(A)] == [
        ("b", round(ab, 6)), ("c", round(ac, 6)),
    ]
    assert all(0 < e.weight <= 1 for e in graph.related_edges)


def test_a_big_source_does_not_dominate() -> None:
    """S shares one introduced concept with a small source T and with a big source U (which has
    many more concepts): T comes out more related."""
    s, t, u = SourceRef(1, "s", "S", "paper"), SourceRef(2, "t", "T", "paper"), SourceRef(3, "u", "U", "paper")
    shared = ConceptRef(1, "shared", "Shared")
    others = [ConceptRef(100 + i, f"c{i}", f"C{i}") for i in range(20)]
    graph = SourceGraph(
        [s, t, u],
        [(1, shared, "introduces"), (2, shared, "introduces"), (3, shared, "introduces")]
        + [(3, c, "introduces") for c in others],
    )
    assert [other.slug for other, _e in graph.related(s)] == ["t", "u"]
    same = SourceGraph([s, t], [(1, shared, "uses"), (2, shared, "uses")])
    assert same.related_edges[0].weight == pytest.approx(1.0)


def test_a_source_without_links_is_not_analysed() -> None:
    graph = small_graph()
    assert graph.analysed(A) and not graph.analysed(D)
    assert graph.not_analysed() == [D]
    assert graph.concept_counts(D) == {"introduces": 0, "uses": 0, "assumes": 0}
    assert graph.concept_counts(A) == {"introduces": 1, "uses": 1, "assumes": 1}
    assert all(D not in (e.a, e.b) for e in graph.related_edges)
    with pytest.raises(ValueError, match="'d' is not analysed"):
        readiness(graph, D, {}, {}, {})


def test_the_input_is_checked() -> None:
    with pytest.raises(ValueError, match="refers to source #9"):
        SourceGraph([A], [(9, X, "uses")])
    with pytest.raises(ValueError, match="unknown role 'mentions'"):
        SourceGraph([A], [(1, X, "mentions")])
    with pytest.raises(ValueError, match="linked to concept 'x' twice"):
        SourceGraph([A], [(1, X, "uses"), (1, X, "assumes")])
    with pytest.raises(ValueError, match="given twice"):
        SourceGraph([A, A], [])
    graph = small_graph()
    with pytest.raises(NotFoundError, match="no knowledge source with slug 'nope'"):
        graph.by_slug("nope")
    with pytest.raises(NotFoundError, match="not in this graph"):
        graph.builds_on(SourceRef(99, "zz", "Z", "paper"))
    with pytest.raises(ValueError, match="invalid source role"):
        graph.concepts_of(A, "mentions")


def test_readiness_lists_the_assumed_gaps_in_study_order_with_who_introduces_them() -> None:
    # E assumes X, Y and Z; A introduces X (with two pages, the newer superseding nothing),
    # B and C introduce Y (only C has a page); nothing introduces Z.
    e = SourceRef(5, "e", "Epsilon", "paper")
    graph = SourceGraph(
        [A, B, C, e],
        [
            (5, X, "assumes"), (5, Y, "assumes"), (5, Z, "assumes"), (5, W, "assumes"),
            (1, X, "introduces"), (2, Y, "introduces"), (3, Y, "introduces"),
        ],
    )
    pages = {1: [PageRef("a-2", "A again", False), PageRef("a-1", "A", True)],
             3: [PageRef("c-1", "C", False)]}
    statuses = {X.id: "weak", Y.id: "unstudied", Z.id: "unstudied", W.id: "strong"}
    # X builds on Z, so Z comes first though its slug is later; W is known (left out).
    prerequisites = {X.id: {Z.id}, Y.id: set(), Z.id: set()}
    value = readiness(graph, e, statuses, prerequisites, pages)
    assert value.assumed == 4
    assert [(r.concept.slug, r.status) for r in value.read_first] == [
        ("y", "unstudied"), ("z", "unstudied"), ("x", "weak"),
    ]
    y, z, x = value.read_first
    # a source with a ready page first, then by title
    assert [(s.slug, [p.slug for p in p_]) for s, p_ in y.introduced_by] == [("c", ["c-1"]), ("b", [])]
    assert z.introduced_by == ()
    assert [(s.slug, [p.slug for p in p_]) for s, p_ in x.introduced_by] == [("a", ["a-2", "a-1"])]

    good = readiness(graph, e, {i: "developing" for i in statuses}, {}, pages)
    assert (good.assumed, good.read_first) == (4, ())
    with pytest.raises(ValueError, match="no standing given for concept 'w'"):
        readiness(graph, e, {X.id: "weak", Y.id: "weak", Z.id: "weak"}, prerequisites, pages)
    with pytest.raises(ValueError, match="unknown standing status 'great'"):
        readiness(graph, e, {**statuses, X.id: "great"}, prerequisites, pages)
    with pytest.raises(ValueError, match="no prerequisites given"):
        readiness(graph, e, statuses, {X.id: set()}, pages)


def test_citations_and_reference_states() -> None:
    graph = small_graph()
    done = ReferenceState("done", True, 4)
    states = {
        A.id: done,
        B.id: ReferenceState("failed", True, 2),  # a failed run keeps the earlier references
        C.id: ReferenceState("failed", False, None),
    }  # D: never run
    citations = Citations(graph, [(1, 2), (1, 3), (2, 1)], states)
    assert [s.slug for s in citations.cites(A)] == ["b", "c"]
    assert [s.slug for s in citations.cites(B)] == ["a"]
    assert citations.cites(C) is None and citations.cites(D) is None  # unknown, not "nothing"
    assert [s.slug for s in citations.cited_by(A)] == ["b"]
    assert [s.slug for s in citations.cited_by(C)] == ["a"]
    assert citations.cited_by(D) == []
    assert citations.state(D) == NEVER_RUN and citations.state(B).state == "failed"
    assert citations.not_extracted() == [D, C]  # by title: "Delta ...", "Gamma notes"
    assert [(a.slug, b.slug) for a, b in citations.edges] == [("a", "b"), ("a", "c"), ("b", "a")]
    # citations do not depend on the concepts: D has none and may still cite
    alone = Citations(graph, [(4, 1)], {D.id: done})
    assert [s.slug for s in alone.cites(D)] == ["a"] and not graph.analysed(D)
    assert alone.cites(A) is None


def test_citation_input_is_checked() -> None:
    graph = small_graph()
    done = ReferenceState("done", True, 1)
    with pytest.raises(ValueError, match="cites itself"):
        Citations(graph, [(1, 1)], {1: done})
    with pytest.raises(ValueError, match="refers to a source not loaded"):
        Citations(graph, [(1, 9)], {1: done})
    with pytest.raises(ValueError, match="'a' cites another source but has no stored references"):
        Citations(graph, [(1, 2)], {})
    with pytest.raises(ValueError, match=r"reference states given for sources \[9\]"):
        Citations(graph, [], {9: done})
    with pytest.raises(ValueError, match="unknown reference extraction state 'maybe'"):
        ReferenceState("maybe", False, None)
    with pytest.raises(ValueError, match="exactly when references are stored"):
        ReferenceState("done", True, None)
    with pytest.raises(ValueError, match="a done extraction has stored references"):
        ReferenceState("done", False, None)


def test_rules() -> None:
    assert rules()["role_weights"] == {"introduces": 3, "uses": 2, "assumes": 1}
    assert rules()["builds_on_roles"] == ["uses", "assumes"]
    assert rules()["good_standing"] == ["developing", "strong"]
    assert rules()["reference_states"] == ["never_run", "running", "interrupted", "done", "failed"]


# -- from the database ------------------------------------------------------------------------


@pytest.fixture
def library(session: Session, settings: Settings, tmp_path: Path):
    """`intro` introduces softmax and dot product (a ready study page, and a lab that must not
    count); `paper` introduces attention (attention builds on softmax and dot product) and
    assumes softmax and dot product; `sequel` assumes attention; `bare` has no links."""
    sources = {}
    for slug in ("intro", "paper", "sequel", "bare"):
        path = tmp_path / f"{slug}.txt"
        path.write_text(f"text of {slug}\n", encoding="utf-8")
        sources[slug] = KnowledgeSourceManager(session, settings).add(
            path, slug=slug, kind="paper", title=slug.title()
        )
    concepts = ConceptManager(session, settings)
    made = {n: concepts.create(n, f"About {n}.", "paper") for n in ("attention", "softmax", "dot product")}
    relations = ConceptRelationManager(session, settings)
    for prerequisite in ("softmax", "dot product"):
        relations.add("builds_on", made["attention"], made[prerequisite], "It needs it.", "paper")
    links = SourceConceptManager(session, settings)
    links.replace(sources["intro"], [
        LinkSpec(made["softmax"], "introduces", "created", "softmax"),
        LinkSpec(made["dot product"], "introduces", "created", "dot product"),
    ])
    links.replace(sources["paper"], [
        LinkSpec(made["attention"], "introduces", "created", "attention"),
        LinkSpec(made["softmax"], "assumes", "exact", "softmax"),
        LinkSpec(made["dot product"], "assumes", "exact", "dot product"),
    ])
    links.replace(sources["sequel"], [LinkSpec(made["attention"], "assumes", "exact", "attention")])
    artifacts = ArtifactManager(session, settings)
    page = artifacts.create(slug="study-intro", kind="study_page", title="Study: Intro",
                            generator="test", sources=[sources["intro"]])
    artifacts.mark_ready(page)
    lab = artifacts.create(slug="lab-intro", kind="lab", title="Lab: Intro", generator="test",
                           sources=[sources["intro"]])
    artifacts.mark_ready(lab)
    session.commit()
    return sources, made


def test_loaded_from_the_database(session: Session, settings: Settings, library) -> None:
    sources, made = library
    graph = SourceGraph.load(session)
    assert [s.slug for s in graph.sources] == ["bare", "intro", "paper", "sequel"]
    assert [(e.dependent.slug, e.foundation.slug, len(e.concepts)) for e in graph.builds_on_edges] == [
        ("paper", "intro", 2), ("sequel", "paper", 1),
    ]
    pages = ready_pages(session)
    assert [p.slug for p in pages[sources["intro"].id]] == ["study-intro"]  # not the lab


def test_source_relations_record(session: Session, settings: Settings, library) -> None:
    body = source_relations(session, settings, "paper")
    assert body["source"] == {"slug": "paper", "title": "Paper", "kind": "paper"}
    assert body["analysed"] is True
    assert body["concept_counts"] == {"introduces": 1, "uses": 0, "assumes": 2}
    assert body["builds_on"] == [{
        "source": {"slug": "intro", "title": "Intro", "kind": "paper"},
        "concepts": [{"slug": "dot-product", "name": "dot product", "role": "assumes"},
                     {"slug": "softmax", "name": "softmax", "role": "assumes"}],
    }]
    assert body["built_on_by"] == [{
        "source": {"slug": "sequel", "title": "Sequel", "kind": "paper"},
        "concepts": [{"slug": "attention", "name": "attention", "role": "assumes"}],
    }]
    # sequel: 3*1 / (sqrt(9+1+1) * 1) = 0.90; intro: (1*3 + 1*3) / (sqrt(11) * sqrt(18)) = 0.43
    assert [(r["source"]["slug"], round(r["weight"], 2)) for r in body["related"]] == [
        ("sequel", 0.9), ("intro", 0.43),
    ]
    assert body["related"][1]["shared"][0] == {
        "slug": "dot-product", "name": "dot product", "role": "assumes", "other_role": "introduces",
    }
    readiness_ = body["readiness"]
    assert readiness_["assumed"] == 2
    assert [(r["concept"]["slug"], r["status"]) for r in readiness_["read_first"]] == [
        ("dot-product", "unstudied"), ("softmax", "unstudied"),
    ]
    assert readiness_["read_first"][0]["introduced_by"] == [{
        "source": {"slug": "intro", "title": "Intro", "kind": "paper"},
        "has_ready_page": True,
        "study_pages": [{"slug": "study-intro", "title": "Study: Intro", "superseded": False}],
    }]
    assert body["not_analysed"] == [{"slug": "bare", "title": "Bare", "kind": "paper"}]

    bare = source_relations(session, settings, "bare")
    assert bare["analysed"] is False
    assert (bare["builds_on"], bare["built_on_by"], bare["related"], bare["readiness"]) == (
        None, None, None, None,
    )
    assert bare["not_analysed"] == []  # the other sources, not itself
    # no references extracted anywhere: unknown, not "cites nothing"
    assert bare["references"] == {"state": "never_run", "stored": False, "reference_count": None}
    assert (bare["cites"], bare["cited_by"]) == (None, [])
    assert [s["slug"] for s in bare["references_not_extracted"]] == ["intro", "paper", "sequel"]
    with pytest.raises(NotFoundError):
        source_relations(session, settings, "nope")


def test_source_graph_record(session: Session, settings: Settings, library) -> None:
    body = source_graph(session, settings)
    assert [(n["slug"], n["analysed"], n["has_ready_page"]) for n in body["nodes"]] == [
        ("bare", False, False), ("intro", True, True), ("paper", True, False), ("sequel", True, False),
    ]
    builds = [(e["from"], e["to"]) for e in body["edges"] if e["type"] == "builds_on"]
    related = [(e["from"], e["to"]) for e in body["edges"] if e["type"] == "related"]
    assert builds == [("paper", "intro"), ("sequel", "paper")]
    assert sorted(related) == [("intro", "paper"), ("paper", "sequel")]
    edge = next(e for e in body["edges"] if e["type"] == "related" and e["to"] == "sequel")
    assert edge["shared"] == [
        {"slug": "attention", "name": "attention", "from_role": "introduces", "to_role": "assumes"},
    ]
    assert edge["weight"] == pytest.approx(3 * 1 / (math.sqrt(9 + 1 + 1) * 1))


def test_deleting_links_makes_a_source_unanalysed(session: Session, settings: Settings, library) -> None:
    sources, _made = library
    SourceConceptManager(session, settings).replace(sources["sequel"], [])
    session.commit()
    body = source_relations(session, settings, "sequel")
    assert body["analysed"] is False and body["related"] is None
    assert source_relations(session, settings, "paper")["built_on_by"] == []
    assert isinstance(session.get(KnowledgeSource, sources["sequel"].id), KnowledgeSource)
    assert session.get(Concept, _made["attention"].id) is not None


def _claim(session: Session, settings: Settings, slug: str, now: datetime | None = None):
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    claim = SourceReferenceManager(session, settings).claim(
        source, trigger="cli", extractor="x", model="m", now=now
    )
    session.commit()
    return claim


def _store(session: Session, settings: Settings, slug: str, specs: list[ReferenceSpec]) -> None:
    claim = _claim(session, settings, slug)
    SourceReferenceManager(session, settings).store(
        claim, specs, found_in="reference_list" if specs else "none"
    )
    session.commit()


def _fail(session: Session, settings: Settings, slug: str) -> None:
    claim = _claim(session, settings, slug)
    SourceReferenceManager(session, settings).fail(claim, "the model said no")
    session.commit()


def cite(doi: str) -> ReferenceSpec:
    return ReferenceSpec(raw_text=f"Somebody. A work. doi:{doi}", doi=doi)


@pytest.fixture
def cited(session: Session, settings: Settings, library):
    """The library with DOIs on intro and paper; paper cites intro (and one work not in the
    library), bare (no concepts) cites paper; sequel's only extraction failed; intro's was
    never run."""
    sources, _made = library
    manager = KnowledgeSourceManager(session, settings)
    manager.add_identifier(sources["intro"], "doi", "10.1000/intro")
    manager.add_identifier(sources["paper"], "doi", "10.1000/paper")
    session.commit()
    _store(session, settings, "paper", [cite("10.1000/intro"), cite("10.1000/elsewhere")])
    _store(session, settings, "bare", [cite("10.1000/paper")])
    _fail(session, settings, "sequel")
    return library


def test_citations_in_the_relations(session: Session, settings: Settings, cited) -> None:
    paper = source_relations(session, settings, "paper")
    assert paper["references"] == {"state": "done", "stored": True, "reference_count": 2}
    assert paper["cites"] == [{"source": {"slug": "intro", "title": "Intro", "kind": "paper"}}]
    assert paper["cited_by"] == [{"source": {"slug": "bare", "title": "Bare", "kind": "paper"}}]
    assert [s["slug"] for s in paper["references_not_extracted"]] == ["intro", "sequel"]

    bare = source_relations(session, settings, "bare")  # references without concepts
    assert bare["analysed"] is False and bare["related"] is None
    assert [c["source"]["slug"] for c in bare["cites"]] == ["paper"]

    sequel = source_relations(session, settings, "sequel")  # failed, nothing stored
    assert sequel["references"] == {"state": "failed", "stored": False, "reference_count": None}
    assert sequel["cites"] is None and sequel["analysed"] is True

    intro = source_relations(session, settings, "intro")  # never run, but cited
    assert intro["references"]["state"] == "never_run" and intro["cites"] is None
    assert [c["source"]["slug"] for c in intro["cited_by"]] == ["paper"]

    _store(session, settings, "sequel", [])  # a run that found no references at all
    sequel = source_relations(session, settings, "sequel")
    assert (sequel["references"]["state"], sequel["cites"]) == ("done", [])  # now known: none

    _fail(session, settings, "paper")  # a later run fails: the stored references still count
    paper = source_relations(session, settings, "paper")
    assert paper["references"] == {"state": "failed", "stored": True, "reference_count": 2}
    assert [c["source"]["slug"] for c in paper["cites"]] == ["intro"]


def test_a_stale_run_is_interrupted(session: Session, settings: Settings, library) -> None:
    started = datetime(2026, 1, 1, 12, 0)
    _claim(session, settings, "intro", now=started)
    running = source_relations(session, settings, "intro", now=started + timedelta(minutes=1))
    assert running["references"]["state"] == "running" and running["cites"] is None
    later = source_relations(session, settings, "intro", now=started + timedelta(hours=2))
    assert later["references"]["state"] == "interrupted" and later["cites"] is None


def test_citations_in_the_graph(session: Session, settings: Settings, cited) -> None:
    body = source_graph(session, settings)
    states = {n["slug"]: (n["references"]["state"], n["references"]["stored"]) for n in body["nodes"]}
    assert states == {
        "bare": ("done", True), "intro": ("never_run", False), "paper": ("done", True),
        "sequel": ("failed", False),
    }
    assert [e for e in body["edges"] if e["type"] == "cites"] == [
        {"type": "cites", "from": "bare", "to": "paper"},
        {"type": "cites", "from": "paper", "to": "intro"},
    ]
    types = [e["type"] for e in body["edges"]]
    assert types == sorted(types, key=["builds_on", "cites", "related"].index)
