"""Enriching an exploration over HTTP (`teacher.web.routers.explorations`): the preview, the
confirmed start (run in the background, as page generation is) with its progress and summary in
the exploration's detail, the refusals (a changed plan, one already running, edits meanwhile, no
enricher), accounts (another account's exploration is a 404 for every route), and the review
page's Enrich controls."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers import ExplorationManager, UserManager
from teacher.web.app import create_app
from tests.enrichment_fakes import approve, cand, candidate, completed_exploration, enricher, outline, page

URL = "https://a.example/dlog"
CODE = "an access code"
PASSWORD = "a long enough password"
CONCEPTS = [outline("ma", "Modular arithmetic", 1),
            outline("dl", "Discrete logarithm", 2, ["ma"])]  # fmt: skip


@pytest.fixture
def make_enricher(fake_anthropic: Any, fake_embedder: Any) -> Any:
    def make() -> Any:
        extract = fake_anthropic(
            fake_anthropic.concepts_reply([cand("Discrete logarithm"), cand("Modular arithmetic")])
        )
        the_enricher, _ = enricher(
            pages={URL: page(URL, "Discrete logarithms, built on modular arithmetic.")},
            extract_client=extract, judge_client=fake_anthropic(), embedder=fake_embedder(),
        )  # fmt: skip
        return the_enricher

    return make


def app_with(settings: Settings, the_enricher: Any, **kw: Any) -> Any:
    return create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                      enricher=the_enricher, **kw)  # fmt: skip


@pytest.fixture
def client(settings: Settings, make_enricher: Any) -> Iterator[TestClient]:
    with TestClient(app_with(settings, make_enricher())) as test_client:
        yield test_client


def exploration(settings: Settings, owner: int | None = None) -> int:
    eid = completed_exploration(settings, CONCEPTS, {"dl": [candidate("c_dl", URL, title="Discrete logs")]},
                                owner=owner)  # fmt: skip
    approve(settings, eid, "c_dl", owner=owner)
    return eid


def test_preview_start_and_progress(settings: Settings, client: TestClient) -> None:
    eid = exploration(settings)
    base = f"/api/explorations/{eid}"
    p = client.get(f"{base}/enrichment/preview").json()
    assert p["can_start"] is True and p["sources"]["to_add"] == 1
    assert [(link["from"], link["to"], link["expected"]) for link in p["links"]["items"]] == [
        ("Discrete logarithm", "Modular arithmetic", "add")]
    assert client.get("/api/sources").json() == []  # the preview added nothing
    r = client.post(f"{base}/enrichments", json={"digest": p["digest"]})
    assert r.status_code == 202, r.text
    started = r.json()
    # The reply is sent when the enrichment starts; TestClient runs the job before returning.
    assert started["enriching"] is True and started["enrichment"]["status"] == "running"
    assert started["editable"] is False
    data = client.get(base).json()
    e = data["enrichment"]
    assert e["status"] == "completed", e["error"]
    assert e["planned"] == {"sources": 1, "concepts": 2, "links": 1}
    assert e["summary"]["sources"]["added"] == 1 and e["summary"]["relations"]["added"] == 1
    assert e["cost"]["calls"] == 1  # concept extraction (the fake embedder records nothing)
    assert data["enrichments"] == 1 and data["editable"] is True
    assert [s["slug"] for s in client.get("/api/sources").json()] == ["discrete-logs"]
    concepts = {c["key"]: c for s in data["steps"] for c in s["concepts"]}
    assert concepts["dl"]["status"] == "grounded"
    assert concepts["dl"]["grounding"][0]["slug"] == "discrete-logs"
    assert concepts["dl"]["links"] == [{"key": "ma", "name": "Modular arithmetic", "outcome": "added",
                                        "code": None, "reason": None,
                                        "relation_id": concepts["dl"]["links"][0]["relation_id"]}]  # fmt: skip


def test_the_refusals(settings: Settings, client: TestClient) -> None:
    eid = exploration(settings)
    base = f"/api/explorations/{eid}"
    digest = client.get(f"{base}/enrichment/preview").json()["digest"]
    data = client.get(base).json()
    cid = data["steps"][0]["concepts"][0]["id"]
    client.patch(f"{base}/concepts/{cid}", json={"name": "Clock arithmetic"})  # the plan changes
    r = client.post(f"{base}/enrichments", json={"digest": digest})
    assert r.status_code == 409 and "The plan changed since it was shown" in r.json()["detail"]
    assert client.post(f"{base}/enrichments", json={}).status_code == 422
    assert client.post(f"{base}/enrichments", json={"digest": "x", "go": True}).status_code == 422
    # One running already: a second is refused, and so are the edits.
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        m.create_enrichment(m.get(eid), {"digest": "x"})
    preview = client.get(f"{base}/enrichment/preview").json()
    assert preview["can_start"] is False and "running" in preview["blocked"]
    r = client.post(f"{base}/enrichments", json={"digest": preview["digest"]})
    assert r.status_code == 409 and "An enrichment of this exploration is running" in r.json()["detail"]
    r = client.patch(f"{base}/concepts/{cid}", json={"dropped": True})
    assert r.status_code == 409 and "is being enriched" in r.json()["detail"]
    cand_id = data["steps"][1]["concepts"][0]["candidates"][0]["id"]
    assert client.patch(f"{base}/candidates/{cand_id}", json={"decision": "proposed"}).status_code == 409
    assert client.put(f"{base}/steps", json={"order": [1, 2]}).status_code == 409
    assert client.get(base).json()["enriching"] is True
    assert client.get("/api/explorations/999999/enrichment/preview").status_code == 404


def test_without_an_enricher_it_is_a_503(settings: Settings) -> None:
    eid = exploration(settings)
    with TestClient(app_with(settings, None)) as without:
        digest = without.get(f"/api/explorations/{eid}/enrichment/preview").json()["digest"]
        r = without.post(f"/api/explorations/{eid}/enrichments", json={"digest": digest})
        assert r.status_code == 503 and "not available" in r.json()["detail"]
        assert without.get(f"/api/explorations/{eid}").json()["enrichment"] is None


def register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "access_code": CODE})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_another_accounts_exploration_is_a_404(settings: Settings, make_enricher: Any) -> None:
    with TestClient(app_with(settings, make_enricher(), access_code=CODE)) as gated:
        ann, bob = register(gated, "ann@example.com"), register(gated, "bob@example.com")
        with session_scope(settings) as session:
            ann_id = UserManager(session, settings).find("ann@example.com").id
        eid = exploration(settings, owner=ann_id)
        base = f"/api/explorations/{eid}"
        assert gated.get(f"{base}/enrichment/preview").status_code == 401
        assert gated.get(f"{base}/enrichment/preview", headers=bob).status_code == 404
        digest = gated.get(f"{base}/enrichment/preview", headers=ann).json()["digest"]
        assert gated.post(f"{base}/enrichments", json={"digest": digest}, headers=bob).status_code == 404
        r = gated.post(f"{base}/enrichments", json={"digest": digest}, headers=ann)
        assert r.status_code == 202, r.text
        assert gated.get(base, headers=ann).json()["enrichment"]["status"] == "completed"


def test_the_review_page_has_the_enrich_controls(settings: Settings, client: TestClient) -> None:
    eid = exploration(settings)
    html = client.get(f"/explore/{eid}").text
    for marker in ('id="exploration-enrich"', 'id="exploration-enrich-button"',
                   'id="exploration-enrich-preview"', 'id="exploration-enrich-status"'):  # fmt: skip
        assert marker in html, marker
