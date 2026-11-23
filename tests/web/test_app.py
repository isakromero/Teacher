"""App factory, lifespan, dependencies and error mapping."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from teacher.config import Settings
from teacher.db.engine import dispose_engine, session_scope
from teacher.db.migrate import current_revision, head_revision
from teacher.db.models import Artifact
from teacher.managers import (
    ArtifactManager,
    DuplicateError,
    IntegrityCheckError,
    KnowledgeSourceManager,
    ManagerError,
    NotFoundError,
    SourceInUseError,
)
from teacher.web import app as app_module
from teacher.web.app import create_app
from teacher.web.deps import SessionDep, SettingsDep


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_state(settings: Settings) -> None:
    gen, graders, extractor = object(), {"prose": object()}, object()
    app = create_app(
        settings, generator=gen, graders=graders, metadata_extractor=extractor, max_attempts=3
    )
    assert app.state.settings is settings
    assert app.state.generator is gen
    assert app.state.graders is graders
    assert app.state.metadata_extractor is extractor
    assert app.state.max_attempts == 3


def test_defaults(settings: Settings) -> None:
    app = create_app(settings)
    assert app.state.max_attempts == 2


def test_default_metadata_extractor_uses_env_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teacher.metadata import MetadataExtractor

    monkeypatch.delenv("TEACHER_METADATA_MODEL", raising=False)
    extractor = create_app(settings).state.metadata_extractor
    assert isinstance(extractor, MetadataExtractor)
    assert extractor.model == "claude-haiku-5-5"
    assert extractor._client is None  # nothing created until the first detection

    monkeypatch.setenv("TEACHER_METADATA_MODEL", "claude-test-reader")
    assert create_app(settings).state.metadata_extractor.model == "claude-test-reader"
    monkeypatch.setenv("TEACHER_METADATA_MODEL", " ")
    with pytest.raises(ValueError, match="TEACHER_METADATA_MODEL is set but empty"):
        create_app(settings)


def test_default_glossary_builder_uses_generation_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teacher.claude_generator import GlossaryBuilder

    monkeypatch.delenv("TEACHER_GENERATION_MODEL", raising=False)
    builder = create_app(settings).state.glossary_builder
    assert isinstance(builder, GlossaryBuilder)
    assert builder.model == "claude-sonnet-5-5"
    assert builder._client is None  # nothing created until the first build

    monkeypatch.setenv("TEACHER_GENERATION_MODEL", "claude-test-writer")
    assert create_app(settings).state.glossary_builder.model == "claude-test-writer"
    given = object()
    assert create_app(settings, glossary_builder=given).state.glossary_builder is given


@pytest.mark.parametrize("bad", [0, -1, True, "2"])
def test_max_attempts_validated(settings: Settings, bad: object) -> None:
    with pytest.raises(ValueError):
        create_app(settings, max_attempts=bad)  # type: ignore[arg-type]


def test_settings_default_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEACHER_HOME", str(tmp_path / "envhome"))
    app = create_app()
    assert app.state.settings.root == (tmp_path / "envhome").resolve()
    assert not (tmp_path / "envhome").exists()  # nothing touched until startup


def test_lifespan_migrates_fresh_root(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "fresh")
    try:
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/health").status_code == 200
            assert current_revision(settings) == head_revision(settings)
            assert settings.sources_dir.is_dir() and settings.artifacts_dir.is_dir()
    finally:
        dispose_engine(settings)


def test_startup_fails_stale_generating_artifacts(settings: Settings) -> None:
    with session_scope(settings) as session:
        mgr = ArtifactManager(session, settings)
        mgr.create(slug="stale", kind="study_page", title="Stale", generator="stub-v1")
        done = mgr.create(slug="done", kind="study_page", title="Done", generator="stub-v1")
        mgr.mark_ready(done)

    assert app_module.fail_interrupted_generations(settings) == ["stale"]
    with session_scope(settings) as session:
        mgr = ArtifactManager(session, settings)
        mgr.set_status(mgr.get_by_slug("stale"), "generating")

    with TestClient(create_app(settings)):
        pass
    with session_scope(settings) as session:
        statuses = dict(session.execute(select(Artifact.slug, Artifact.status)).all())
    assert statuses == {"stale": "failed", "done": "ready"}


# -- errors and the session dependency ---------------------------------------------------


class Body(BaseModel):
    n: int


@pytest.fixture
def error_client(settings: Settings) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    router = APIRouter(prefix="/test")
    exceptions: dict[str, Exception] = {
        "not-found": NotFoundError("no such thing"),
        "duplicate": DuplicateError("already there"),
        "in-use": SourceInUseError("still used"),
        "integrity": IntegrityCheckError("hash mismatch"),
        "manager": ManagerError("generic manager problem"),
        "value": ValueError("bad value"),
        "http": HTTPException(status_code=418, detail="teapot"),
        "path": ValueError(f"folder {settings.root / 'knowledge_sources' / 'x'} exists"),
    }

    @router.get("/raise/{name}")
    def raise_(name: str) -> None:
        raise exceptions[name]

    @router.post("/body")
    def body(payload: Body) -> dict[str, int]:
        return {"n": payload.n}

    @router.post("/write/{slug}")
    def write(slug: str, fail: bool, session: SessionDep, settings: SettingsDep) -> dict:
        mgr = ArtifactManager(session, settings)
        mgr.create(slug=slug, kind="k", title="t", generator="g")
        if fail:
            raise ValueError("boom after write")
        return {"ok": True}

    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    ("name", "status", "detail"),
    [
        ("not-found", 404, "no such thing"),
        ("duplicate", 409, "already there"),
        ("in-use", 409, "still used"),
        ("integrity", 422, "hash mismatch"),
        ("manager", 422, "generic manager problem"),
        ("value", 422, "bad value"),
        ("http", 418, "teapot"),
    ],
)
def test_error_mapping(error_client: TestClient, name: str, status: int, detail: str) -> None:
    response = error_client.get(f"/test/raise/{name}")
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_error_detail_hides_absolute_paths(error_client: TestClient, settings: Settings) -> None:
    detail = error_client.get("/test/raise/path").json()["detail"]
    assert str(settings.root) not in detail
    assert detail == "folder knowledge_sources/x exists"


def test_unknown_route_and_validation_errors_are_strings(error_client: TestClient) -> None:
    response = error_client.get("/api/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

    response = error_client.post("/test/body", json={"n": "not a number"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.startswith("n:")


def test_session_commits_on_success_and_rolls_back_on_error(
    error_client: TestClient, settings: Settings
) -> None:
    assert error_client.post("/test/write/kept", params={"fail": False}).status_code == 200
    response = error_client.post("/test/write/dropped", params={"fail": True})
    assert response.status_code == 422
    with session_scope(settings) as session:
        slugs = set(session.scalars(select(Artifact.slug)))
    assert slugs == {"kept"}


def test_upload_source_fixture_accepts_explicit_client(client: TestClient, upload_source) -> None:
    assert upload_source(client, title="Explicit")["slug"] == "explicit"
    assert upload_source(title="Implicit")["slug"] == "implicit"


def test_no_session_leak_between_requests(client: TestClient, settings: Settings) -> None:
    # A source added directly is visible to the API at once (each request gets a fresh session).
    src = settings.root / "x.txt"
    src.write_text("hello")
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).add(src, slug="direct", kind="note", title="D")
    assert client.get("/api/sources/direct").status_code == 200


def test_default_concept_extractor_uses_env_models(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teacher.concept_extraction import ConceptExtractor
    from teacher.embeddings import OpenAIEmbedder

    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", "claude-test-concepts")
    monkeypatch.setenv("TEACHER_EMBEDDING_MODEL", "embed-test")
    app = create_app(settings)
    extractor = app.state.concept_extractor
    assert extractor is app.state.generator.concept_extractor
    assert isinstance(extractor, ConceptExtractor) and extractor.model == "claude-test-concepts"
    assert extractor._client is None  # nothing created until the first generation
    assert isinstance(extractor.embedder, OpenAIEmbedder)
    assert extractor.embedder.model == "embed-test"

    embedder = OpenAIEmbedder("embed-given")
    app = create_app(settings, embedder=embedder)
    assert app.state.concept_extractor.embedder is embedder
    given = ConceptExtractor(embedder=embedder)
    assert create_app(settings, concept_extractor=given).state.generator.concept_extractor is given

    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", " ")
    with pytest.raises(ValueError, match="TEACHER_CONCEPT_MODEL is set but empty"):
        create_app(settings)


def test_concept_wiring_conflicts_are_errors(settings: Settings) -> None:
    from teacher.concept_extraction import ConceptExtractor
    from teacher.embeddings import OpenAIEmbedder
    from teacher.generation import StubConceptExtractor, StubGenerator

    embedder = OpenAIEmbedder()
    with pytest.raises(ValueError, match="only configure the default generator"):
        create_app(settings, generator=StubGenerator(), concept_extractor=object())
    with pytest.raises(ValueError, match="only configure the default generator"):
        create_app(settings, generator=StubGenerator(), embedder=embedder)
    with pytest.raises(ValueError, match="give it to the concept_extractor you pass"):
        create_app(settings, concept_extractor=ConceptExtractor(embedder=embedder),
                   embedder=embedder)
    stub = create_app(settings, generator=StubGenerator()).state.concept_extractor
    assert isinstance(stub, StubConceptExtractor)
    assert create_app(settings, generator=object()).state.concept_extractor is None


def test_default_block_tagger_uses_the_concept_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teacher.block_tagging import BlockTagger
    from teacher.generation import StubGenerator

    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", "claude-test-concepts")
    tagger = create_app(settings).state.generator.block_tagger
    assert isinstance(tagger, BlockTagger) and tagger.model == "claude-test-concepts"
    assert tagger._client is None  # nothing created until the first generation
    given = BlockTagger(model="claude-given")
    assert create_app(settings, block_tagger=given).state.generator.block_tagger is given
    with pytest.raises(ValueError, match="block_tagger only configures the default generator"):
        create_app(settings, generator=StubGenerator(), block_tagger=given)
