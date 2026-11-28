"""The roadmap over HTTP (`teacher.web.routers.explorations`): reading it, the explorations list's
progress, the reader's choices (another primary source, "I know this", both undone) with their
validation, "Generate page" (the source page's generation, started for the concept's primary
source, once per source), accounts (another account's exploration is a 404 for every route and
page), the pages and the view model under node (tests/web/js/roadmap_test.mjs). The app runs the
offline `StubGenerator`; nothing reaches a model."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Artifact, GenerationRun, StudyEvent
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers import ArtifactManager, ExplorationManager, UserManager
from teacher.web.app import create_app
from tests.roadmap_fakes import World, build_world, study_page

STATIC_JS = Path(__file__).resolve().parents[2] / "src" / "teacher" / "web" / "static" / "js"
JS_TESTS = Path(__file__).resolve().parent / "js"
CODE = "an access code"
PASSWORD = "a long enough password"


@pytest.fixture
def world(settings: Settings, tmp_path: Path) -> World:
    return build_world(settings, tmp_path)


def concepts(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["key"]: c for step in data["steps"] for c in step["concepts"]}


def base(world: World) -> str:
    return f"/api/explorations/{world.eid}/roadmap"


def test_the_roadmap_and_the_list(settings: Settings, world: World, client: TestClient) -> None:
    r = client.get(base(world))
    assert r.status_code == 200, r.text
    data = r.json()
    assert [s["number"] for s in data["steps"]] == [1, 2, 3]
    assert data["next_action"]["kind"] == "generate_page"
    assert concepts(data)["ma"]["primary_source"]["slug"] == "number-theory"
    with session_scope(settings) as session:  # a running one: listed without a roadmap
        running = ExplorationManager(session, settings, owner=None).create("Running", "intro").id
    listed = {item["id"]: item for item in client.get("/api/explorations").json()}
    assert listed[running]["roadmap"] is None
    summary = listed[world.eid]["roadmap"]
    assert summary["url"] == f"/explore/{world.eid}/roadmap"
    assert summary["progress"] == {"concepts_done": 0, "concepts_total": 5, "steps_done": 0,
                                   "steps_total": 3, "complete": False}  # fmt: skip
    assert summary["current_step_title"] == "Foundations"
    assert summary["next_action"]["kind"] == "generate_page"
    r = client.get(f"/api/explorations/{running}/roadmap")
    assert r.status_code == 409 and "only a completed exploration has a roadmap" in r.json()["detail"]
    assert client.get("/api/explorations/999999/roadmap").status_code == 404


def test_the_choices_and_their_undo(world: World, client: TestClient) -> None:
    url = f"{base(world)}/concepts/{world.rows['dl']}"
    r = client.patch(url, json={"primary_source": "dlog-paper"})
    assert r.status_code == 200, r.text
    assert concepts(r.json())["dl"]["primary_source"] == {"slug": "dlog-paper", "title": "Discrete log paper", "chosen": True}
    r = client.patch(url, json={"primary_source": None, "marked_known": True})
    c = concepts(r.json())["dl"]
    assert c["primary_source"]["slug"] == "dlog-notes" and c["primary_source"]["chosen"] is False
    assert c["done_by"] == ["marked_known"]
    r = client.patch(url, json={"marked_known": False})
    assert concepts(r.json())["dl"]["done"] is False
    # Stored: read again.
    assert concepts(client.get(base(world)).json())["dl"]["marked_known"] is False


def test_the_choices_are_checked(world: World, client: TestClient) -> None:
    url = f"{base(world)}/concepts/{world.rows['gr']}"
    r = client.patch(url, json={"primary_source": "dlog-notes"})
    assert r.status_code == 422 and "does not ground concept 'Group'" in r.json()["detail"]
    r = client.patch(url, json={"primary_source": "no-such-source"})
    assert r.status_code == 422 and "there is no source 'no-such-source' in the library" in r.json()["detail"]
    for body in ({}, {"marked_known": "yes"}, {"marked_known": None}, {"marked_known": True, "go": 1}):
        assert client.patch(url, json=body).status_code == 422, body
    dropped = f"{base(world)}/concepts/{world.rows['xx']}"
    r = client.patch(dropped, json={"marked_known": True})
    assert r.status_code == 409 and "is dropped" in r.json()["detail"]
    assert client.patch(f"{base(world)}/concepts/999999", json={"marked_known": True}).status_code == 404
    assert concepts(client.get(base(world)).json())["gr"]["primary_source"]["chosen"] is False


def test_generate_page_starts_the_source_pages_generation_once_per_source(
    settings: Settings, world: World, client: TestClient
) -> None:
    r = client.post(f"{base(world)}/concepts/{world.rows['ma']}/page", json={})
    assert r.status_code == 202, r.text
    reply = r.json()
    artifact = reply["artifact"]
    # What POST /api/artifacts {"sources": ["number-theory"]} makes from the source page.
    assert artifact["title"] == "Study: Number theory" and artifact["kind"] == "study_page"
    assert [s["slug"] for s in artifact["sources"]] == ["number-theory"]
    assert artifact["generator"] == StubGenerator().name and artifact["status"] == "generating"
    c = concepts(reply["roadmap"])
    assert c["ma"]["page"]["slug"] == c["gr"]["page"]["slug"] == artifact["slug"]
    assert c["ma"]["page"]["status"] == "generating"
    assert reply["roadmap"]["next_action"]["kind"] == "open_page"
    with session_scope(settings) as session:
        row = ArtifactManager(session, settings).get_by_slug(artifact["slug"])
        assert row.status == "ready"  # the background job ran (TestClient runs it before returning)
        assert session.scalars(select(GenerationRun).where(GenerationRun.artifact_id == row.id)).first() is not None
        events = session.scalars(select(StudyEvent).where(StudyEvent.kind == "study_page_requested")).all()
        assert [(e.artifact_id, e.payload) for e in events] == [(row.id, {"sources": ["number-theory"], "model": None})]
    # Group shares the source: its page exists, so it is linked, not generated again.
    r = client.post(f"{base(world)}/concepts/{world.rows['gr']}/page", json={})
    assert r.status_code == 409
    assert "has a study page already" in r.json()["detail"] and artifact["slug"] in r.json()["detail"]
    c = concepts(client.get(base(world)).json())["gr"]
    assert c["page"]["slug"] == artifact["slug"] and c["can_generate"] is False
    with session_scope(settings) as session:
        assert len(session.scalars(select(Artifact)).all()) == 1


def test_generate_page_refusals(settings: Settings, world: World, client: TestClient) -> None:
    page = f"{base(world)}/concepts/{{}}/page"
    r = client.post(page.format(world.rows["dh"]), json={})
    assert r.status_code == 409 and "no source in your library introduces" in r.json()["detail"]
    r = client.post(page.format(world.rows["ec"]), json={})
    assert r.status_code == 409 and "find and tick sources" in r.json()["detail"]
    r = client.post(page.format(world.rows["xx"]), json={})
    assert r.status_code == 409 and "is dropped" in r.json()["detail"]
    # The model is checked as POST /api/artifacts checks it (the stub cannot be given one).
    r = client.post(page.format(world.rows["dl"]), json={"model": "claude-x"})
    assert r.status_code == 422 and "does not support choosing a model" in r.json()["detail"]
    assert client.post(page.format(world.rows["dl"]), json={"pages": 2}).status_code == 422
    # An existing page of the primary source (made on the source page) is linked instead.
    study_page(settings, "dlog-notes", "notes-page")
    r = client.post(page.format(world.rows["dl"]))
    assert r.status_code == 409 and "notes-page" in r.json()["detail"]
    with session_scope(settings) as session:
        assert [a.slug for a in session.scalars(select(Artifact))] == ["notes-page"]


def register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "access_code": CODE})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def gated(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(), access_code=CODE)
    with TestClient(app) as test_client:
        yield test_client


def test_another_accounts_roadmap_is_a_404(settings: Settings, tmp_path: Path, gated: TestClient) -> None:
    ann, bob = register(gated, "ann@example.com"), register(gated, "bob@example.com")
    with session_scope(settings) as session:
        ann_id = UserManager(session, settings).find("ann@example.com").id
    world = build_world(settings, tmp_path, owner=ann_id)
    concept = f"{base(world)}/concepts/{world.rows['ma']}"
    assert gated.get(base(world)).status_code == 401
    assert gated.get(base(world), headers=bob).status_code == 404
    assert gated.patch(concept, json={"marked_known": True}, headers=bob).status_code == 404
    assert gated.post(f"{concept}/page", json={}, headers=bob).status_code == 404
    assert gated.get(f"/explore/{world.eid}/roadmap", headers=bob).status_code == 404
    assert [i["id"] for i in gated.get("/api/explorations", headers=bob).json()] == []
    with session_scope(settings) as session:
        assert session.scalars(select(Artifact)).all() == []  # bob generated nothing
    assert gated.get(base(world), headers=ann).status_code == 200
    r = gated.patch(concept, json={"marked_known": True}, headers=ann)
    assert r.status_code == 200 and concepts(r.json())["ma"]["marked_known"] is True
    r = gated.post(f"{base(world)}/concepts/{world.rows['dl']}/page", json={}, headers=ann)
    assert r.status_code == 202, r.text
    assert gated.get(f"/explore/{world.eid}/roadmap", headers=ann).status_code == 200
    assert gated.get("/api/explorations", headers=ann).json()[0]["roadmap"]["progress"]["concepts_done"] == 1


def test_the_roadmap_page_and_its_links(world: World, client: TestClient) -> None:
    html = client.get(f"/explore/{world.eid}/roadmap").text
    for marker in (f'data-exploration="{world.eid}"', 'id="roadmap-steps"', 'id="roadmap-next"',
                   'id="roadmap-progress"', 'id="roadmap-model"', "/static/js/roadmap.js",
                   f'href="/explore/{world.eid}"', '<a href="/explore" aria-current="page">Explore</a>'):  # fmt: skip
        assert marker in html, marker
    assert client.get("/explore/999999/roadmap").status_code == 404
    review = client.get(f"/explore/{world.eid}").text
    assert 'id="exploration-roadmap-link"' in review and f'href="/explore/{world.eid}/roadmap"' in review
    home = client.get("/").text
    assert 'id="home-roadmaps-section"' in home and "Continue your roadmap" in home
    js = (STATIC_JS / "roadmap.js").read_text(encoding="utf-8")
    assert "/roadmap" in js and "/page" in js and "chosenModel" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_roadmap_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "roadmap_test.mjs")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("roadmap_model.js", "roadmap.js", "explore.js", "exploration.js", "home.js"):
        check = subprocess.run(
            ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True, timeout=60
        )
        assert check.returncode == 0, check.stderr
