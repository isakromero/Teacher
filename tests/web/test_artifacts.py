"""Artifact endpoints: create + background generation, list, show, runs, regenerate, delete."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import BlockDraft, GenerationContext, StubGenerator
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    BlockResponseManager,
    KnowledgeSourceManager,
)
from teacher.web.app import create_app

Json = dict[str, Any]


@pytest.fixture
def source(upload_source: Callable[..., Json]) -> Json:
    return upload_source(title="Deep Learning", slug="deep-learning")


@pytest.fixture
def create_artifact(client: TestClient) -> Callable[..., Json]:
    """`create_artifact(**body)`: POST /api/artifacts, assert 202, return the JSON."""

    def create(**body: Any) -> Json:
        response = client.post("/api/artifacts", json=body)
        assert response.status_code == 202, response.text
        return response.json()

    return create


@pytest.fixture
def ready_artifact(client: TestClient, source: Json, create_artifact: Callable[..., Json]) -> Json:
    created = create_artifact(sources=[source["slug"]])
    return client.get(f"/api/artifacts/{created['slug']}").json()


class RaisingGenerator:
    name = "raising"
    model = "broken-model"

    def generate(self, ctx: GenerationContext) -> list[BlockDraft]:
        with ctx.stage("analyse"):
            raise RuntimeError(f"cannot read {ctx.settings.root / 'secret.txt'}")


class InvalidGenerator:
    name = "invalid"
    model = "m"

    def generate(self, ctx: GenerationContext) -> list[BlockDraft]:
        return [BlockDraft("blurb", {"prompt": "Explain."})]  # missing private part


@pytest.fixture
def client_with(settings: Settings) -> Iterator[Callable[..., TestClient]]:
    """`client_with(**create_app_kwargs)`: a started TestClient for a custom app."""
    clients: list[TestClient] = []

    def make(**kwargs: Any) -> TestClient:
        kwargs.setdefault("generator", StubGenerator())  # never the default (Claude) generator
        test_client = TestClient(create_app(settings, **kwargs))
        test_client.__enter__()
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        test_client.__exit__(None, None, None)


def upload(test_client: TestClient, slug: str = "deep-learning") -> Json:
    response = test_client.post(
        "/api/sources",
        data={"kind": "note", "title": "Deep Learning", "slug": slug},
        files={"file": ("notes.txt", f"text {slug}".encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


# -- create -------------------------------------------------------------------------------


def test_fixture_app_uses_the_stub_generator(app: FastAPI) -> None:
    assert isinstance(app.state.generator, StubGenerator)


def test_create_returns_generating_then_ready(
    client: TestClient, source: Json, settings: Settings
) -> None:
    response = client.post("/api/artifacts", json={"sources": ["deep-learning"]})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["slug"] == "study-deep-learning"
    assert body["title"] == "Study: Deep Learning"
    assert body["kind"] == "study_page"
    assert body["status"] == "generating"
    assert body["generator"] == "stub-v1"
    assert body["params"] == {}
    assert body["supersedes"] is None and body["superseded_by"] is None
    assert body["sources"] == [{"slug": "deep-learning", "title": "Deep Learning", "role": "primary"}]
    assert body["progress"] == {
        "total_blocks": 0,
        "gates_total": 0,
        "gates_cleared": 0,
        "complete": False,
    }
    assert body["tokens"] == {"input": 0, "output": 0}
    assert body["created_at"].endswith("+00:00")

    shown = client.get("/api/artifacts/study-deep-learning")
    assert shown.status_code == 200
    detail = shown.json()
    assert detail["status"] == "ready"
    # The stub page: seven blocks, three of them gates (a question, a blurb, a prediction).
    assert detail["progress"] == {
        "total_blocks": 7,
        "gates_total": 3,
        "gates_cleared": 0,
        "complete": False,
    }
    assert (settings.artifacts_dir / "study-deep-learning" / "blocks" / "b07.json").is_file()


def test_create_with_title_slug_kind_params(
    client: TestClient, source: Json, create_artifact: Callable[..., Json]
) -> None:
    body = create_artifact(
        sources=["deep-learning"], title="My page", slug="mine", kind="quiz", params={"n": 3}
    )
    assert (body["slug"], body["title"], body["kind"], body["params"]) == (
        "mine",
        "My page",
        "quiz",
        {"n": 3},
    )


def test_default_slug_is_uniquified(source: Json, create_artifact: Callable[..., Json]) -> None:
    slugs = [create_artifact(sources=["deep-learning"])["slug"] for _ in range(3)]
    assert slugs == ["study-deep-learning", "study-deep-learning-2", "study-deep-learning-3"]


def test_create_multiple_sources(
    client: TestClient, upload_source: Callable[..., Json], create_artifact: Callable[..., Json]
) -> None:
    upload_source(title="First", slug="first")
    upload_source(title="Second", slug="second")
    body = create_artifact(sources=["second", "first"])
    assert body["title"] == "Study: Second"
    assert {s["slug"] for s in body["sources"]} == {"first", "second"}
    source = client.get("/api/sources/first").json()
    assert source["artifacts"] == [
        {"slug": "study-second", "title": "Study: Second", "kind": "study_page", "status": "ready"}
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"sources": []},
        {},
        {"sources": ["deep-learning", "deep-learning"]},
        {"sources": ["deep-learning"], "slug": "Not A Slug"},
        {"sources": ["deep-learning"], "title": "!!!"},
        {"sources": ["deep-learning"], "title": "  "},
        {"sources": ["deep-learning"], "params": []},
        {"sources": ["deep-learning"], "extra": 1},
    ],
)
def test_create_validation_errors(client: TestClient, source: Json, body: Json) -> None:
    response = client.post("/api/artifacts", json=body)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert client.get("/api/artifacts").json() == []


def test_create_unknown_source_is_404(client: TestClient, source: Json) -> None:
    response = client.post("/api/artifacts", json={"sources": ["deep-learning", "nope"]})
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]
    assert client.get("/api/artifacts").json() == []


def test_create_duplicate_slug_is_409(
    client: TestClient, source: Json, create_artifact: Callable[..., Json]
) -> None:
    create_artifact(sources=["deep-learning"], slug="taken")
    response = client.post("/api/artifacts", json={"sources": ["deep-learning"], "slug": "taken"})
    assert response.status_code == 409


# -- failing generators -------------------------------------------------------------------


def test_raising_generator_marks_failed(client_with: Callable[..., TestClient]) -> None:
    test_client = client_with(generator=RaisingGenerator())
    upload(test_client)
    created = test_client.post("/api/artifacts", json={"sources": ["deep-learning"]}).json()
    assert created["generator"] == "raising"

    detail = test_client.get(f"/api/artifacts/{created['slug']}").json()
    assert detail["status"] == "failed"
    runs = test_client.get(f"/api/artifacts/{created['slug']}/runs").json()
    assert len(runs) == 1
    run = runs[0]
    assert (run["stage"], run["model"], run["status"]) == ("analyse", "broken-model", "failed")
    assert run["error"].startswith("RuntimeError: cannot read")
    assert "secret.txt" in run["error"]
    assert str(test_client.app.state.settings.root) not in run["error"]  # no absolute paths
    assert run["finished_at"] is not None


def test_invalid_block_marks_failed(client_with: Callable[..., TestClient]) -> None:
    test_client = client_with(generator=InvalidGenerator())
    upload(test_client)
    created = test_client.post("/api/artifacts", json={"sources": ["deep-learning"]}).json()
    detail = test_client.get(f"/api/artifacts/{created['slug']}").json()
    assert detail["status"] == "failed"
    assert detail["progress"]["total_blocks"] == 0
    runs = test_client.get(f"/api/artifacts/{created['slug']}/runs").json()
    assert [(r["stage"], r["status"]) for r in runs] == [("store", "failed")]
    assert "invalid blurb block content" in runs[0]["error"]


# -- list / show / runs -------------------------------------------------------------------


def test_list_filters(
    client: TestClient, upload_source: Callable[..., Json], create_artifact: Callable[..., Json]
) -> None:
    upload_source(title="One", slug="one")
    upload_source(title="Two", slug="two")
    create_artifact(sources=["one"])
    create_artifact(sources=["two"], kind="quiz")
    with session_scope(client.app.state.settings) as session:
        manager = ArtifactManager(session, client.app.state.settings)
        manager.mark_failed(manager.get_by_slug("study-two"))

    def slugs(**query: str) -> list[str]:
        response = client.get("/api/artifacts", params=query)
        assert response.status_code == 200, response.text
        return [a["slug"] for a in response.json()]

    assert slugs() == ["study-one", "study-two"]
    assert slugs(source="one") == ["study-one"]
    assert slugs(status="failed") == ["study-two"]
    assert slugs(status="ready") == ["study-one"]
    assert slugs(kind="quiz") == ["study-two"]
    assert slugs(kind="quiz", status="ready") == []
    assert client.get("/api/artifacts", params={"status": "weird"}).status_code == 422
    assert client.get("/api/artifacts", params={"source": "missing"}).status_code == 404


def test_get_unknown_artifact_is_404(client: TestClient) -> None:
    for path in ("/api/artifacts/nope", "/api/artifacts/nope/runs"):
        response = client.get(path)
        assert response.status_code == 404
        assert "nope" in response.json()["detail"]


def test_runs(client: TestClient, ready_artifact: Json) -> None:
    runs = client.get(f"/api/artifacts/{ready_artifact['slug']}/runs").json()
    assert [(r["stage"], r["model"], r["status"]) for r in runs] == [
        ("preflight", "none", "succeeded"),
        ("analyse", "none", "succeeded"),
        ("write", "none", "succeeded"),
        ("verify", "none", "succeeded"),
        ("concepts", "none", "succeeded"),
        ("block_concepts", "none", "succeeded"),
    ]
    assert set(runs[0]) == {
        "id",
        "stage",
        "model",
        "status",
        "input_tokens",
        "output_tokens",
        "error",
        "note",
        "started_at",
        "finished_at",
    }
    assert runs[0]["error"] is None and runs[0]["started_at"].endswith("+00:00")
    assert all(r["note"] is None for r in runs)  # the stub's stages carry no note


def test_tokens_are_summed(client_with: Callable[..., TestClient]) -> None:
    class Counting:
        name = "counting"
        model = "m"

        def generate(self, ctx: GenerationContext) -> list[BlockDraft]:
            with ctx.stage("a") as stage:
                stage.set_tokens(100, 20)
            with ctx.stage("b") as stage:
                stage.set_tokens(5, 1)
            return [BlockDraft("prose", {"markdown": "hi"})]

    test_client = client_with(generator=Counting())
    upload(test_client)
    slug = test_client.post("/api/artifacts", json={"sources": ["deep-learning"]}).json()["slug"]
    assert test_client.get(f"/api/artifacts/{slug}").json()["tokens"] == {
        "input": 105,
        "output": 21,
    }


# -- progress -----------------------------------------------------------------------------


def _record(settings: Settings, slug: str, key: str, passed: bool) -> None:
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        block = ArtifactBlockManager(session, settings).get_by_key(artifact, key)
        BlockResponseManager(session, settings).record(
            block, response={"x": 1}, passed=passed, grader="test"
        )


def test_progress_counts_passed_and_exhausted_gates(
    client_with: Callable[..., TestClient], settings: Settings
) -> None:
    test_client = client_with(max_attempts=3)
    upload(test_client)
    slug = test_client.post("/api/artifacts", json={"sources": ["deep-learning"]}).json()["slug"]

    def progress() -> Json:
        return test_client.get(f"/api/artifacts/{slug}").json()["progress"]

    assert progress()["gates_cleared"] == 0
    _record(settings, slug, "b02", passed=False)
    _record(settings, slug, "b02", passed=True)  # gate 1 passed
    assert progress() == {"total_blocks": 7, "gates_total": 3, "gates_cleared": 1, "complete": False}
    _record(settings, slug, "b04", passed=False)
    _record(settings, slug, "b04", passed=False)
    assert progress()["gates_cleared"] == 1  # 2 failed attempts < max_attempts=3
    _record(settings, slug, "b04", passed=False)  # escape hatch reached
    assert progress() == {"total_blocks": 7, "gates_total": 3, "gates_cleared": 2, "complete": False}
    _record(settings, slug, "b05", passed=True)  # the prediction: any choice clears it
    assert progress() == {"total_blocks": 7, "gates_total": 3, "gates_cleared": 3, "complete": True}


# -- regenerate ---------------------------------------------------------------------------


def test_regenerate(client: TestClient, ready_artifact: Json) -> None:
    old = ready_artifact["slug"]
    response = client.post(f"/api/artifacts/{old}/regenerate")
    assert response.status_code == 202, response.text
    new = response.json()
    assert new["slug"] == f"{old}-2"
    assert new["status"] == "generating"
    assert new["supersedes"] == old
    assert (new["title"], new["kind"], new["sources"]) == (
        ready_artifact["title"],
        ready_artifact["kind"],
        ready_artifact["sources"],
    )
    assert client.get(f"/api/artifacts/{old}").json()["superseded_by"] == new["slug"]
    assert client.get(f"/api/artifacts/{new['slug']}").json()["status"] == "ready"

    third = client.post(f"/api/artifacts/{new['slug']}/regenerate", json={}).json()
    assert third["slug"] == f"{old}-3"
    assert third["supersedes"] == new["slug"]


def test_regenerate_params(
    client: TestClient, source: Json, create_artifact: Callable[..., Json]
) -> None:
    old = create_artifact(sources=["deep-learning"], params={"depth": 1})["slug"]
    kept = client.post(f"/api/artifacts/{old}/regenerate").json()
    assert kept["params"] == {"depth": 1}
    changed = client.post(f"/api/artifacts/{old}/regenerate", json={"params": {"depth": 5}}).json()
    assert changed["params"] == {"depth": 5}
    bad = client.post(f"/api/artifacts/{old}/regenerate", json={"params": 3})
    assert bad.status_code == 422


def test_model_choice_needs_a_generator_that_supports_it(
    client: TestClient, source: Json, ready_artifact: Json
) -> None:
    """The stub generator has one fixed model: none is offered and naming one is an error."""
    offered = client.get("/api/generation-models").json()
    assert offered == {"selectable": False, "default": "none", "models": []}
    assert ready_artifact["model"] is None
    for response in (
        client.post("/api/artifacts", json={"sources": [source["slug"]], "model": "claude-opus-5-5"}),
        client.post(
            f"/api/artifacts/{ready_artifact['slug']}/regenerate", json={"model": "claude-opus-5-5"}
        ),
        client.post(
            "/api/artifacts",
            json={"sources": [source["slug"]], "params": {"model": "claude-opus-5-5"}},
        ),
    ):
        assert response.status_code == 422, response.text
        assert "does not support choosing a model" in response.text
    assert len(client.get("/api/artifacts").json()) == 1


def test_regenerate_keeps_roles(client: TestClient, source: Json, settings: Settings) -> None:
    upload_resp = client.post(
        "/api/sources",
        data={"kind": "note", "title": "Extra", "slug": "extra"},
        files={"file": ("e.txt", b"extra text", "text/plain")},
    )
    assert upload_resp.status_code == 201
    with session_scope(settings) as session:
        sources = KnowledgeSourceManager(session, settings)
        artifact = ArtifactManager(session, settings).create(
            slug="manual",
            kind="study_page",
            title="Manual",
            generator="x",
            sources=[
                (sources.get_by_slug("deep-learning"), "primary"),
                (sources.get_by_slug("extra"), "background"),
            ],
        )
        ArtifactManager(session, settings).mark_ready(artifact)
    new = client.post("/api/artifacts/manual/regenerate").json()
    assert sorted((s["slug"], s["role"]) for s in new["sources"]) == [
        ("deep-learning", "primary"),
        ("extra", "background"),
    ]


def test_regenerate_and_delete_conflict_while_generating(
    client: TestClient, source: Json, settings: Settings
) -> None:
    with session_scope(settings) as session:
        ArtifactManager(session, settings).create(
            slug="busy",
            kind="study_page",
            title="Busy",
            generator="stub-v1",
            sources=[KnowledgeSourceManager(session, settings).get_by_slug("deep-learning")],
        )
    for response in (
        client.post("/api/artifacts/busy/regenerate"),
        client.delete("/api/artifacts/busy"),
    ):
        assert response.status_code == 409
        assert "generating" in response.json()["detail"]
    assert client.get("/api/artifacts/busy").status_code == 200


def test_regenerate_unknown_is_404(client: TestClient) -> None:
    assert client.post("/api/artifacts/nope/regenerate").status_code == 404


# -- delete -------------------------------------------------------------------------------


def test_delete(client: TestClient, ready_artifact: Json, settings: Settings) -> None:
    slug = ready_artifact["slug"]
    response = client.delete(f"/api/artifacts/{slug}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/api/artifacts/{slug}").status_code == 404
    assert not (settings.artifacts_dir / slug).exists()
    # the source can be deleted now that nothing references it
    assert client.delete("/api/sources/deep-learning").status_code == 204


def test_delete_superseded_keeps_successor(client: TestClient, ready_artifact: Json) -> None:
    old = ready_artifact["slug"]
    new = client.post(f"/api/artifacts/{old}/regenerate").json()["slug"]
    assert client.delete(f"/api/artifacts/{old}").status_code == 204
    assert client.get(f"/api/artifacts/{new}").json()["supersedes"] is None


def test_delete_unknown_is_404(client: TestClient) -> None:
    assert client.delete("/api/artifacts/nope").status_code == 404


def test_startup_marks_generating_failed(settings: Settings, source: Json) -> None:
    with session_scope(settings) as session:
        ArtifactManager(session, settings).create(
            slug="stale",
            kind="study_page",
            title="Stale",
            generator="stub-v1",
            sources=[KnowledgeSourceManager(session, settings).get_by_slug("deep-learning")],
        )
    with TestClient(create_app(settings, generator=StubGenerator())) as fresh:
        assert fresh.get("/api/artifacts/stale").json()["status"] == "failed"
