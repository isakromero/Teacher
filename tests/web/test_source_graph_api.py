"""The source graph endpoints (`GET /api/sources/{slug}/related`, `GET /api/source-graph`),
concept relations and citations, the "Related sources" section of the source page and its
scripts (the model runs under node)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, Concept, KnowledgeSource
from teacher.managers import (
    BlockConceptManager,
    ConceptManager,
    KnowledgeSourceManager,
    LinkSpec,
    SourceConceptManager,
)
from teacher.managers.source_references import ReferenceSpec, SourceReferenceManager
from teacher.web.routers.pages import STATIC_DIR, TEMPLATES_DIR

JS_DIR = STATIC_DIR / "js"
NODE_TEST = Path(__file__).parent / "js" / "source_related_test.mjs"
NEW_SCRIPTS = ("source_related.js", "source_related_model.js")
SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


@pytest.fixture
def library(tag_library: Any, tmp_path: Path) -> Any:
    """The tag library (`paper` introduces attention, uses softmax and transformer, assumes dot
    product; a ready page `study-paper`, b02 a question tagged attention as main) plus `basics`
    (introduces dot product and softmax, no study page) and `sequel` (assumes attention, uses
    transformer, introduces beam search)."""
    lib = tag_library
    with session_scope(lib.settings) as session:
        sources = KnowledgeSourceManager(session, lib.settings)
        links = SourceConceptManager(session, lib.settings)
        concept = {slug: session.get(Concept, i) for slug, i in lib.concepts.items()}
        concept["beam-search"] = ConceptManager(session, lib.settings).create(
            "beam search", "About beam search.", "paper"
        )
        added = {}
        for slug in ("basics", "sequel"):
            path = tmp_path / f"{slug}.txt"
            path.write_text(f"text of {slug}\n", encoding="utf-8")
            added[slug] = sources.add(path, slug=slug, kind="paper", title=slug.title())
        links.replace(added["basics"], [
            LinkSpec(concept["dot-product"], "introduces", "exact", "dot product"),
            LinkSpec(concept["softmax"], "introduces", "exact", "softmax"),
        ])
        links.replace(added["sequel"], [
            LinkSpec(concept["attention"], "assumes", "exact", "attention"),
            LinkSpec(concept["transformer"], "uses", "exact", "transformer"),
            LinkSpec(concept["beam-search"], "introduces", "created", "beam search"),
        ])
        BlockConceptManager(session, lib.settings).add(
            session.get(ArtifactBlock, lib.blocks["b02"]), concept["attention"], "main", "model",
            rationale="Tagged attention on b02.", model="m",
        )
    return lib


def extract(settings: Any, slug: str, dois: list[str] | None) -> None:
    """Record an extraction run of `slug`'s references: one citing each DOI, or a failed run
    when `dois` is None."""
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
        claim = SourceReferenceManager(session, settings).claim(
            source, trigger="cli", extractor="x", model="m"
        )
    with session_scope(settings) as session:
        manager = SourceReferenceManager(session, settings)
        if dois is None:
            manager.fail(claim, "the model said no")
        else:
            specs = [ReferenceSpec(raw_text=f"A work. doi:{d}", doi=d) for d in dois]
            manager.store(claim, specs, found_in="reference_list" if specs else "none")


def ref(slug: str, title: str) -> dict[str, str]:
    return {"slug": slug, "title": title, "kind": "paper"}


def test_unknown_source_is_a_404(client: TestClient) -> None:
    response = client.get("/api/sources/nope/related")
    assert response.status_code == 404
    assert response.json() == {"detail": "no knowledge source with slug 'nope'"}


def test_a_source_without_concepts_is_not_analysed(client: TestClient, upload_source) -> None:
    upload_source(title="Fresh", slug="fresh")
    upload_source(title="Other", slug="other")
    body = client.get("/api/sources/fresh/related").json()
    assert body["analysed"] is False
    assert body["source"] == {"slug": "fresh", "title": "Fresh", "kind": "note"}
    assert body["concept_counts"] == {"introduces": 0, "uses": 0, "assumes": 0}
    for key in ("builds_on", "built_on_by", "related", "readiness"):
        assert body[key] is None, key  # unknown, not "none"
    assert body["not_analysed"] == [{"slug": "other", "title": "Other", "kind": "note"}]
    assert body["references"] == {"state": "never_run", "stored": False, "reference_count": None}
    assert (body["cites"], body["cited_by"]) == (None, [])
    assert body["references_not_extracted"] == [{"slug": "other", "title": "Other", "kind": "note"}]
    graph = client.get("/api/source-graph").json()
    assert [(n["slug"], n["analysed"]) for n in graph["nodes"]] == [("fresh", False), ("other", False)]
    assert graph["edges"] == []


def test_relations_of_a_source(client: TestClient, library: Any) -> None:
    body = client.get("/api/sources/paper/related").json()
    assert body["analysed"] is True
    assert body["concept_counts"] == {"introduces": 1, "uses": 2, "assumes": 1}
    assert body["builds_on"] == [{
        "source": ref("basics", "Basics"),
        "concepts": [{"slug": "softmax", "name": "softmax", "role": "uses"},
                     {"slug": "dot-product", "name": "dot product", "role": "assumes"}],
    }]
    assert body["built_on_by"] == [{
        "source": ref("sequel", "Sequel"),
        "concepts": [{"slug": "attention", "name": "attention", "role": "assumes"}],
    }]
    related = {r["source"]["slug"]: r for r in body["related"]}
    assert set(related) == {"basics", "sequel"}
    assert [r["source"]["slug"] for r in body["related"]] == sorted(
        related, key=lambda s: -related[s]["weight"]
    )
    # heaviest first: uses * uses = 4 before introduces * assumes = 3
    assert [(s["slug"], s["role"], s["other_role"]) for s in related["sequel"]["shared"]] == [
        ("transformer", "uses", "uses"), ("attention", "introduces", "assumes"),
    ]
    assert body["readiness"]["assumed"] == 1
    assert body["readiness"]["read_first"] == [{
        "concept": {"slug": "dot-product", "name": "dot product"},
        "status": "unstudied",
        "introduced_by": [{"source": ref("basics", "Basics"), "has_ready_page": False,
                           "study_pages": []}],
    }]
    assert body["not_analysed"] == []
    assert body["rules"]["role_weights"] == {"introduces": 3, "uses": 2, "assumes": 1}


def test_readiness_follows_the_readers_standing(client: TestClient, library: Any) -> None:
    before = client.get("/api/sources/sequel/related").json()["readiness"]
    assert before["read_first"] == [{
        "concept": {"slug": "attention", "name": "attention"},
        "status": "unstudied",
        "introduced_by": [{
            "source": ref("paper", "The Paper"), "has_ready_page": True,
            "study_pages": [{"slug": "study-paper", "title": "Study: The Paper", "superseded": False}],
        }],
    }]
    wrong = client.post("/api/artifacts/study-paper/blocks/b02/responses",
                        json={"response": {"choice": 1}})
    assert wrong.status_code == 201, wrong.text
    weak = client.get("/api/sources/sequel/related").json()["readiness"]["read_first"]
    assert [(r["concept"]["slug"], r["status"]) for r in weak] == [("attention", "weak")]


def test_readiness_is_empty_in_good_standing(client: TestClient, library: Any) -> None:
    right = client.post("/api/artifacts/study-paper/blocks/b02/responses",
                        json={"response": {"choice": 0}})
    assert right.status_code == 201, right.text
    readiness = client.get("/api/sources/sequel/related").json()["readiness"]
    assert readiness == {"assumed": 1, "read_first": []}


def test_the_whole_graph(client: TestClient, library: Any, upload_source) -> None:
    upload_source(title="Unread", slug="unread")
    body = client.get("/api/source-graph").json()
    assert [(n["slug"], n["analysed"], n["has_ready_page"]) for n in body["nodes"]] == [
        ("basics", True, False), ("sequel", True, False), ("paper", True, True),
        ("unread", False, False),
    ]  # by title: Basics, Sequel, The Paper, Unread
    assert [(e["from"], e["to"]) for e in body["edges"] if e["type"] == "builds_on"] == [
        ("paper", "basics"), ("sequel", "paper"),
    ]
    related = [e for e in body["edges"] if e["type"] == "related"]
    assert {(e["from"], e["to"]) for e in related} == {("basics", "paper"), ("paper", "sequel")}
    assert all(e["from"] < e["to"] and 0 < e["weight"] <= 1 for e in related)
    assert all("unread" not in (e["from"], e["to"]) for e in body["edges"])
    # not shadowed by GET /api/sources/{slug}, nor taken for a source
    assert client.get("/api/sources/source-graph/related").status_code == 404


def test_deleting_a_source_removes_its_edges(client: TestClient, library: Any) -> None:
    with session_scope(library.settings) as session:
        basics = session.query(KnowledgeSource).filter_by(slug="basics").one()
        KnowledgeSourceManager(session, library.settings).delete(basics)
    body = client.get("/api/sources/paper/related").json()
    assert body["builds_on"] == []
    assert body["readiness"]["read_first"][0]["introduced_by"] == []


def test_the_source_page_has_the_section(client: TestClient, library: Any) -> None:
    html = client.get("/sources/paper").text
    assert '<section id="related-sources-section" aria-labelledby="related-sources-heading">' in html
    assert '<h2 id="related-sources-heading">Related sources</h2>' in html
    assert '<div id="related-sources" aria-live="polite">' in html
    assert '<script type="module" src="/static/js/source_related.js"></script>' in html
    for name in NEW_SCRIPTS:
        assert client.get(f"/static/js/{name}").status_code == 200, name
        code = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in SINKS:
            assert sink not in code, f"{name}: {sink}"
    assert "/related`" in (JS_DIR / "source_related.js").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", (TEMPLATES_DIR / "source.html").read_text(encoding="utf-8"))


def test_citations(client: TestClient, library: Any) -> None:
    """sequel cites the paper (by DOI) and a work outside the library; basics found no
    references; the paper's extraction failed."""
    with session_scope(library.settings) as session:
        paper = KnowledgeSourceManager(session, library.settings).get_by_slug("paper")
        KnowledgeSourceManager(session, library.settings).add_identifier(paper, "doi", "10.1000/paper")
    extract(library.settings, "sequel", ["10.1000/paper", "10.1000/not-here"])
    extract(library.settings, "basics", [])
    extract(library.settings, "paper", None)

    sequel = client.get("/api/sources/sequel/related").json()
    assert sequel["references"] == {"state": "done", "stored": True, "reference_count": 2}
    assert sequel["cites"] == [{"source": ref("paper", "The Paper")}]
    assert sequel["cited_by"] == []
    assert sequel["references_not_extracted"] == [ref("paper", "The Paper")]
    # a citation is its own fact: sequel cites the paper and also builds on it through concepts
    paper = client.get("/api/sources/paper/related").json()
    assert paper["references"]["state"] == "failed" and paper["cites"] is None
    assert paper["cited_by"] == [{"source": ref("sequel", "Sequel")}]
    assert [e["source"]["slug"] for e in paper["built_on_by"]] == ["sequel"]
    basics = client.get("/api/sources/basics/related").json()
    assert (basics["references"]["state"], basics["cites"]) == ("done", [])

    graph = client.get("/api/source-graph").json()
    assert [e for e in graph["edges"] if e["type"] == "cites"] == [
        {"type": "cites", "from": "sequel", "to": "paper"},
    ]
    assert {n["slug"]: n["references"]["state"] for n in graph["nodes"]} == {
        "basics": "done", "sequel": "done", "paper": "failed",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_source_related_model_under_node_and_the_scripts_parse() -> None:
    result = subprocess.run(["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60,
                            check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "source related ok" in result.stdout
    for name in NEW_SCRIPTS:
        checked = subprocess.run(["node", "--check", str(JS_DIR / name)], capture_output=True,
                                 text=True, timeout=60, check=False)
        assert checked.returncode == 0, f"{name}: {checked.stderr}"
