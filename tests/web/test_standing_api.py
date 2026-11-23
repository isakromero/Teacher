"""The standing and suggestions endpoints (`/api/history/concepts`, `/concepts/{slug}`,
`/suggestions`), block tags where blocks are read, standing and tags in the export, and the
pages and scripts that show them (the helpers run under node)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.db.engine import session_scope
from teacher.db.models import ArtifactBlock, Concept
from teacher.managers import BlockConceptManager
from teacher.web.routers.pages import STATIC_DIR, TEMPLATES_DIR

JS_DIR = STATIC_DIR / "js"
NODE_TEST = Path(__file__).parent / "js" / "standing_test.mjs"
NEW_SCRIPTS = ("standing.js", "standing_model.js", "history_concepts.js", "home.js")
SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


@pytest.fixture
def tagged(tag_library: Any) -> Any:
    """The tag library with b01/b02 on attention (main), b02 also softmax (incidental), b04 on
    transformer (main) and attention (incidental)."""
    lib = tag_library
    with session_scope(lib.settings) as session:
        manager = BlockConceptManager(session, lib.settings)
        for key, concept, role in [
            ("b01", "attention", "main"), ("b02", "attention", "main"),
            ("b02", "softmax", "incidental"), ("b04", "transformer", "main"),
            ("b04", "attention", "incidental"),
        ]:
            manager.add(session.get(ArtifactBlock, lib.blocks[key]),
                        session.get(Concept, lib.concepts[concept]), role, "model",
                        rationale=f"Tagged {concept} on {key}.", model="m")
    return lib


def answer(client: TestClient, key: str, choice: int) -> Any:
    response = client.post(f"/api/artifacts/study-paper/blocks/{key}/responses",
                           json={"response": {"choice": choice}})
    assert response.status_code == 201, response.text
    return response.json()


def test_standing_of_every_concept(client: TestClient, tagged: Any) -> None:
    body = client.get("/api/history/concepts").json()
    assert body["thresholds"] == {
        "weak_below": 0.5, "strong_from": 0.8, "strong_min_questions": 2,
        "statuses": ["unstudied", "weak", "developing", "strong"],
        "good_standing": ["developing", "strong"],
    }
    assert [c["concept"]["slug"] for c in body["concepts"]] == [
        "attention", "dot-product", "softmax", "transformer",
    ]
    assert {c["status"] for c in body["concepts"]} == {"unstudied"}
    answer(client, "b02", 1)  # wrong
    attention = client.get("/api/history/concepts").json()["concepts"][0]
    assert (attention["status"], attention["questions_answered"], attention["attempts"]) == ("weak", 1, 1)
    answer(client, "b02", 0)  # right, the second time
    attention = client.get("/api/history/concepts").json()["concepts"][0]
    assert (attention["status"], attention["first_attempt_passed"], attention["eventually_passed"],
            attention["attempts"]) == ("weak", 0, 1, 2)


def test_one_concept_names_only_what_the_reader_may_see(client: TestClient, tagged: Any) -> None:
    detail = client.get("/api/history/concepts/attention").json()
    assert detail["status"] == "unstudied" and detail["thresholds"]["strong_min_questions"] == 2
    b02, b04 = detail["tested_by"]  # main first
    assert (b02["block"], b02["role"], b02["visible"]) == ("b02", "main", True)
    assert b02["question"] == "What does attention compute?"
    assert b02["rationale"] is None  # not cleared yet: it could hint at the answer
    assert (b04["block"], b04["role"], b04["visible"], b04["question"]) == ("b04", "incidental", False, None)

    answer(client, "b02", 0)
    b02, b04 = client.get("/api/history/concepts/attention").json()["tested_by"]
    assert b02["rationale"] == "Tagged attention on b02."
    assert (b02["attempts"], b02["first_attempt_passed"], b02["eventually_passed"]) == (1, True, True)
    assert b04["visible"] is True and b04["question"] == "Explain how a transformer uses attention."
    assert client.get("/api/history/concepts/nope").status_code == 404


def test_suggestions_endpoint(client: TestClient, tagged: Any) -> None:
    answer(client, "b02", 1)  # attention: weak
    body = client.get("/api/history/suggestions").json()
    assert body["rules"]["kinds"] == ["fill_gap", "review", "ready_next"]
    assert [(s["kind"], s["concept"]["slug"]) for s in body["suggestions"]] == [
        ("fill_gap", "dot-product"), ("fill_gap", "softmax"), ("review", "attention"),
    ]
    assert body["total"] == 3
    first = body["suggestions"][0]
    assert first["rank"] == 1 and first["action"]["kind"] == "source"
    assert first["evidence"]["supports"][0]["slug"] == "attention"
    limited = client.get("/api/history/suggestions?limit=1").json()
    assert (limited["total"], len(limited["suggestions"])) == (3, 1)
    assert client.get("/api/history/suggestions?limit=0").status_code == 422


def test_blocks_carry_their_concepts(client: TestClient, tagged: Any) -> None:
    b01, b02 = client.get("/api/artifacts/study-paper/blocks").json()["blocks"]
    assert b01["concepts"] == [{"slug": "attention", "name": "attention", "role": "main", "origin": "model"}]
    assert [(c["slug"], c["role"]) for c in b02["concepts"]] == [("attention", "main"), ("softmax", "incidental")]
    assert "rationale" not in b02["concepts"][0]


def test_the_export_has_the_standing_and_the_tags(client: TestClient, tagged: Any) -> None:
    before = client.get("/api/history/export?format=json").json()["block_concepts"]
    # b04 is behind the gate b02, not answered yet: named by its key only
    assert [(t["block"], t["block_label"]) for t in before] == [
        ("b01", "Attention"), ("b02", "What does attention compute?"),
        ("b02", "What does attention compute?"), ("b04", None), ("b04", None),
    ]
    markdown = client.get("/api/history/export?format=markdown").text
    assert "- b04 (blurb, not reached yet): transformer (main), attention (incidental)" in markdown
    assert "Explain how a transformer" not in markdown

    answer(client, "b02", 0)
    data = client.get("/api/history/export?format=json").json()
    assert data["version"] == 4 and data["standing_thresholds"]["weak_below"] == 0.5
    assert data["block_concepts"][-1]["block_label"] == "Explain how a transformer uses attention."
    attention = next(c for c in data["concepts"] if c["concept"]["slug"] == "attention")
    assert (attention["status"], attention["questions_answered"]) == ("developing", 1)
    assert data["block_concepts"][0] == {
        "artifact": {"slug": "study-paper", "title": "Study: The Paper", "kind": "study_page"},
        "block": "b01", "block_label": "Attention", "block_type": "prose",
        "concept": {"slug": "attention", "name": "attention"},
        "role": "main", "origin": "model", "rationale": "Tagged attention on b01.", "model": "m",
        "created_at": data["block_concepts"][0]["created_at"],
    }
    markdown = client.get("/api/history/export?format=markdown").text
    assert "## Concept standing" in markdown
    assert "| attention (`attention`) | developing | 1 of 1 | 1 (100%) | 1 |" in markdown
    assert ("- b02 “What does attention compute?” (multiple_choice): attention (main), softmax "
            "(incidental)") in markdown
    assert "Answered checkpoint b02 “What does attention compute?” (multiple choice)" in markdown
    assert "  - Answer (option 1):\n    > A weighted sum of values\n" in markdown


def test_pages_show_the_standing_and_the_suggestions(client: TestClient) -> None:
    history = client.get("/history").text
    for fragment in ('id="history-suggestions"', 'id="history-concepts"', 'id="history-concepts-all"',
                     '<script type="module" src="/static/js/history_concepts.js"></script>',
                     '<link rel="stylesheet" href="/static/css/standing.css">'):
        assert fragment in history, fragment
    home = client.get("/").text  # the home page shows the first few suggestions
    assert 'id="home-suggestions"' in home
    assert '<script type="module" src="/static/js/home.js"></script>' in home
    assert '<link rel="stylesheet" href="/static/css/standing.css">' in home
    assert "/api/history/suggestions?limit=" in (JS_DIR / "home.js").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/css/standing.css">' in client.get("/graph").text
    for path in [*(f"/static/js/{name}" for name in NEW_SCRIPTS), "/static/css/standing.css"]:
        assert client.get(path).status_code == 200, path
    for name in NEW_SCRIPTS:
        code = (JS_DIR / name).read_text(encoding="utf-8")
        for sink in SINKS:
            assert sink not in code, f"{name}: {sink}"
    graph = (JS_DIR / "graph.js").read_text(encoding="utf-8")
    assert '"/api/history/concepts"' in graph and "view.setStanding(" in graph
    assert "standing-mark" in (JS_DIR / "graph_view.js").read_text(encoding="utf-8")
    assert "standingSection" in (JS_DIR / "graph_inspector.js").read_text(encoding="utf-8")
    for template in ("history.html", "home.html", "library.html", "graph.html"):
        assert not re.search(r"\son[a-z]+\s*=", (TEMPLATES_DIR / template).read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_standing_helpers_under_node_and_the_scripts_parse() -> None:
    result = subprocess.run(["node", str(NODE_TEST)], capture_output=True, text=True, timeout=60,
                            check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "standing ok" in result.stdout
    for name in (*NEW_SCRIPTS, "graph.js", "graph_view.js", "graph_inspector.js"):
        checked = subprocess.run(["node", "--check", str(JS_DIR / name)], capture_output=True,
                                 text=True, timeout=60, check=False)
        assert checked.returncode == 0, f"{name}: {checked.stderr}"
