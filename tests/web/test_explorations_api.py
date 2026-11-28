"""/api/explorations (`teacher.web.routers.explorations`) with an explorer of fakes: starting one
(run in the background, as page generation is), listing and reading it, the edits, accounts
(another account's exploration is a 404), the errors, the Explore pages and their navigation,
and the pages' view model under node (tests/web/js/explore_test.mjs)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.web.app import create_app
from tests.exploration_fakes import (
    brief_reply,
    concept,
    explorer,
    finder,
    hit,
    outline_reply,
    step,
)

STATIC_JS = Path(__file__).resolve().parents[2] / "src" / "teacher" / "web" / "static" / "js"
JS_TESTS = Path(__file__).resolve().parent / "js"
CODE = "an access code"
PASSWORD = "a long enough password"
STEPS = [
    step("One", concept("Modular arithmetic"), concept("Group")),
    step("Two", concept("Discrete logarithm", prerequisites=["Modular arithmetic", "Group"])),
    step("Three", concept("Diffie-Hellman key exchange", prerequisites=["Discrete logarithm"])),
]
URL = "https://a.example/shared"


@pytest.fixture
def make_explorer(fake_anthropic: Any, fake_embedder: Any) -> Any:
    def make(runs: int = 1) -> Any:
        replies = []
        for _ in range(runs):
            replies += [brief_reply(fake_anthropic), outline_reply(fake_anthropic, STEPS)]
        src, _ = finder({
            "Modular arithmetic": [hit("web", 1, URL, "Number theory notes")],
            "Group": [hit("web", 1, URL, "Number theory notes")],
            "Discrete logarithm": [hit("web", 1, "https://b.example/dlog")],
        })  # fmt: skip
        return explorer(fake_anthropic(*replies), fake_embedder(), src)

    return make


def app_with(settings: Settings, the_explorer: Any, **kw: Any) -> Any:
    return create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                      explorer=the_explorer, **kw)  # fmt: skip


@pytest.fixture
def client(settings: Settings, make_explorer: Any) -> Iterator[TestClient]:
    with TestClient(app_with(settings, make_explorer(runs=2))) as test_client:
        yield test_client


def start(client: TestClient, **headers: Any) -> dict[str, Any]:
    r = client.post("/api/explorations", json={"topic": "Diffie-Hellman", "level": "intro"}, **headers)
    assert r.status_code == 202, r.text
    return r.json()


def test_start_list_and_read(client: TestClient) -> None:
    created = start(client)
    # The reply is sent when the run starts; TestClient runs the background job before it
    # returns, so reading it now shows the finished run.
    assert created["status"] == "running" and created["prompt"] == "Diffie-Hellman"
    data = client.get(f"/api/explorations/{created['id']}").json()
    assert data["status"] == "completed", data["error"]
    assert [len(s["concepts"]) for s in data["steps"]] == [2, 1, 1]
    concepts = {c["key"]: c for s in data["steps"] for c in s["concepts"]}
    shared_a = concepts["modular-arithmetic"]["candidates"][0]
    shared_b = concepts["group"]["candidates"][0]
    assert shared_a["id"] == shared_b["id"] and shared_a["concepts"] == ["modular-arithmetic", "group"]
    assert concepts["diffie-hellman-key-exchange"]["discovery"]["log"][1]["attempt"] == 2  # follow-up
    listed = client.get("/api/explorations").json()
    assert [(e["id"], e["status"], e["concepts"], e["candidates"]) for e in listed] == [
        (created["id"], "completed", 4, 2)]
    limits = client.get("/api/explorations/limits").json()
    assert limits["limits"]["max_concepts"] == 12 and limits["limits"]["price_estimate"] is None
    assert [lv["id"] for lv in limits["levels"]] == ["intro", "intermediate", "advanced"]


def test_the_edits(client: TestClient) -> None:
    eid = start(client)["id"]
    data = client.get(f"/api/explorations/{eid}").json()
    concepts = {c["key"]: c for s in data["steps"] for c in s["concepts"]}
    group = concepts["group"]
    base = f"/api/explorations/{eid}"
    r = client.patch(f"{base}/concepts/{group['id']}", json={"name": "Groups"})
    assert r.status_code == 200, r.text
    renamed = {c["key"]: c for s in r.json()["steps"] for c in s["concepts"]}["group"]
    assert (renamed["name"], renamed["drafted_name"], renamed["renamed"]) == ("Groups", "Group", True)
    r = client.patch(f"{base}/concepts/{group['id']}", json={"dropped": True})
    assert {c["key"]: c for s in r.json()["steps"] for c in s["concepts"]}["group"]["status"] == "dropped"
    assert client.patch(f"{base}/concepts/{group['id']}", json={}).status_code == 422
    assert client.patch(f"{base}/concepts/{group['id']}", json={"name": "Modular arithmetic"}).status_code == 422
    assert client.patch(f"{base}/concepts/999999", json={"dropped": True}).status_code == 404
    # Steps: Two builds on One, so they cannot swap; a bad order is a 422.
    bad = client.put(f"{base}/steps", json={"order": [2, 1, 3]})
    assert bad.status_code == 422 and "must stay in an earlier step" in bad.json()["detail"]
    assert client.put(f"{base}/steps", json={"order": [1, 2]}).status_code == 422
    # Candidates: tick, untick, reject.
    candidate = concepts["modular-arithmetic"]["candidates"][0]
    r = client.patch(f"{base}/candidates/{candidate['id']}", json={"decision": "approved"})
    assert r.status_code == 200 and r.json()["approved"] == 1
    after = {c["key"]: c for s in r.json()["steps"] for c in s["concepts"]}
    assert after["group"]["candidates"][0]["decision"] == "approved"  # the same row for both
    r = client.patch(f"{base}/candidates/{candidate['id']}", json={"decision": "proposed"})
    assert r.json()["approved"] == 0
    assert client.patch(f"{base}/candidates/{candidate['id']}", json={"decision": "ingested"}).status_code == 422
    assert client.patch(f"{base}/candidates/999999", json={"decision": "approved"}).status_code == 404
    # Nothing was ingested.
    assert client.get("/api/sources").json() == []


def test_edits_on_a_running_exploration_are_a_409(client: TestClient, settings: Settings) -> None:
    from teacher.db.engine import session_scope
    from teacher.managers import ExplorationManager

    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.create("Running", "intro")
        m.store_outline(row, questions=[], steps=["S"], concepts=[
            {"key": "a", "position": 1, "step": 1, "name": "A", "aliases": [], "summary": "a.",
             "prerequisites": [], "search_query": "a q"}])  # fmt: skip
        eid, cid = row.id, m.concepts(row)[0].id
    r = client.patch(f"/api/explorations/{eid}/concepts/{cid}", json={"dropped": True})
    assert r.status_code == 409 and "only a completed exploration can be edited" in r.json()["detail"]
    assert client.put(f"/api/explorations/{eid}/steps", json={"order": [1]}).status_code == 409


@pytest.mark.parametrize(
    "body", [{}, {"topic": "  "}, {"topic": "x" * 301}, {"topic": "x", "level": "expert"},
             {"topic": "x", "extra": 1}],
)  # fmt: skip
def test_bad_bodies_are_422(client: TestClient, body: dict[str, Any]) -> None:
    r = client.post("/api/explorations", json=body)
    assert r.status_code == 422, r.text
    assert client.get("/api/explorations").json() == []


def test_errors(settings: Settings, client: TestClient) -> None:
    assert client.get("/api/explorations/999999").status_code == 404
    with TestClient(app_with(settings, None)) as without:
        r = without.post("/api/explorations", json={"topic": "DH"})
        assert r.status_code == 503 and "not available" in r.json()["detail"]
        assert without.get("/api/explorations").json() == []


def test_a_stale_index_is_a_503_before_anything_is_spent(
    settings: Settings, session: Any, client: TestClient
) -> None:
    from teacher.managers import ConceptManager

    ConceptManager(session, settings).create("Group", "A group.", "user")
    session.commit()  # no embedding
    r = client.post("/api/explorations", json={"topic": "DH"})
    assert r.status_code == 503 and "teacher concepts reindex" in r.json()["detail"]
    assert client.get("/api/explorations").json() == []


# -- accounts ---------------------------------------------------------------------------------


def register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "access_code": CODE})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_each_account_has_its_own_explorations(settings: Settings, make_explorer: Any) -> None:
    with TestClient(app_with(settings, make_explorer(), access_code=CODE)) as gated:
        assert gated.get("/api/explorations").status_code == 401
        ann, bob = register(gated, "ann@example.com"), register(gated, "bob@example.com")
        eid = start(gated, headers=ann)["id"]
        assert [e["id"] for e in gated.get("/api/explorations", headers=ann).json()] == [eid]
        assert gated.get("/api/explorations", headers=bob).json() == []
        assert gated.get(f"/api/explorations/{eid}", headers=bob).status_code == 404
        data = gated.get(f"/api/explorations/{eid}", headers=ann).json()
        cid = data["steps"][0]["concepts"][0]["id"]
        assert gated.patch(f"/api/explorations/{eid}/concepts/{cid}", json={"dropped": True},
                           headers=bob).status_code == 404  # fmt: skip
        assert gated.put(f"/api/explorations/{eid}/steps", json={"order": [1, 2, 3]},
                         headers=bob).status_code == 404  # fmt: skip
        assert gated.get(f"/explore/{eid}", headers=bob).status_code == 404
        assert gated.get(f"/explore/{eid}", headers=ann).status_code == 200


# -- the pages ----------------------------------------------------------------------------------


def test_the_explore_pages(client: TestClient) -> None:
    html = client.get("/explore").text
    for marker in (
        'id="explore-form"', 'id="explore-topic"', 'id="explore-level"', 'id="explore-list"',
        'id="explore-limits"', "At most 12 concepts", "no reliable price estimate",
        "/static/js/explore.js", "/static/css/explore.css",
        '<option value="intermediate" selected>Intermediate</option>',
        '<a href="/explore" aria-current="page">Explore</a>',
    ):  # fmt: skip
        assert marker in html, marker
    assert '<a href="/explore">Explore</a>' in client.get("/library").text  # in the site's nav
    eid = start(client)["id"]
    page = client.get(f"/explore/{eid}").text
    for marker in (f'data-exploration="{eid}"', 'id="exploration-steps"', 'id="exploration-cost"',
                   'id="exploration-progress"', "/static/js/exploration.js", "Diffie-Hellman"):  # fmt: skip
        assert marker in page, marker
    assert client.get("/explore/999999").status_code == 404
    js = (STATIC_JS / "exploration.js").read_text(encoding="utf-8")
    assert "/api/explorations/" in js and "POLL_MS" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_explore_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "explore_test.mjs")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("explore_model.js", "explore.js", "exploration.js"):
        check = subprocess.run(
            ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True, timeout=60
        )
        assert check.returncode == 0, check.stderr
