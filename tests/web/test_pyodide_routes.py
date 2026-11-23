"""The lab runtime routes (`teacher.web.routers.pyodide`), on a small app that includes only
that router, with a fake runtime install (no network, no Pyodide)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.pyodide_runtime import ENV_PYODIDE_DIR, MANIFEST_FILE, PYODIDE_VERSION
from teacher.web.routers import pyodide

FILES = {
    "pyodide.mjs": b"export const loadPyodide = 1;",
    "pyodide.asm.wasm": b"\x00asm\x01\x00\x00\x00",
    "pyodide-lock.json": b'{"packages": {}}',
    "python_stdlib.zip": b"PK\x05\x06",
    "numpy-2.4.6-cp314-cp314-pyemscripten_2026_0_wasm32.whl": b"PK wheel",
}


def install_fake(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in FILES.items():
        (directory / name).write_bytes(data)
    manifest = {
        "version": PYODIDE_VERSION,
        "packages": ["matplotlib", "numpy"],
        "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in FILES.items()},
    }
    (directory / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "unlisted.txt").write_text("not part of the install")
    return directory


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv(ENV_PYODIDE_DIR, raising=False)
    return Settings(tmp_path / "home")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(pyodide.router)
    return TestClient(app)


def runtime_path(settings: Settings) -> Path:
    return settings.derived_dir / "pyodide" / PYODIDE_VERSION


def test_status_when_not_installed(client: TestClient, settings: Settings) -> None:
    response = client.get("/api/labs/runtime")
    assert response.status_code == 200
    body = response.json()
    assert body["installed"] is False
    assert body["version"] == PYODIDE_VERSION
    assert body["url"] == f"/pyodide/{PYODIDE_VERSION}/"
    assert body["packages"] == ["numpy", "matplotlib"]
    assert "`teacher labs setup`" in body["detail"]
    assert str(settings.root) not in body["detail"]  # the data root is not leaked


def test_files_are_404_when_not_installed(client: TestClient) -> None:
    response = client.get(f"/pyodide/{PYODIDE_VERSION}/pyodide.mjs")
    assert response.status_code == 404
    assert "teacher labs setup" in response.json()["detail"]


def test_status_and_files_when_installed(client: TestClient, settings: Settings) -> None:
    install_fake(runtime_path(settings))
    assert client.get("/api/labs/runtime").json() == {
        "installed": True,
        "version": PYODIDE_VERSION,
        "url": f"/pyodide/{PYODIDE_VERSION}/",
        "packages": ["numpy", "matplotlib"],
        "detail": None,
    }
    for name, data in FILES.items():
        response = client.get(f"/pyodide/{PYODIDE_VERSION}/{name}")
        assert response.status_code == 200, name
        assert response.content == data
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    types = {
        "pyodide.mjs": "text/javascript; charset=utf-8",
        "pyodide.asm.wasm": "application/wasm",
        "pyodide-lock.json": "application/json",
        "python_stdlib.zip": "application/zip",
    }
    for name, media_type in types.items():
        assert client.get(f"/pyodide/{PYODIDE_VERSION}/{name}").headers["content-type"] == media_type


def test_only_listed_files_of_the_pinned_version_are_served(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    install_fake(runtime_path(settings))
    (tmp_path / "outside.txt").write_text("secret")
    for path in (
        f"/pyodide/{PYODIDE_VERSION}/unlisted.txt",
        f"/pyodide/{PYODIDE_VERSION}/{MANIFEST_FILE}",
        f"/pyodide/{PYODIDE_VERSION}/..%2F..%2F..%2Foutside.txt",
        f"/pyodide/{PYODIDE_VERSION}/sub/pyodide.mjs",
        "/pyodide/0.27.0/pyodide.mjs",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "secret" not in response.text


def test_the_runtime_follows_the_override(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = install_fake(tmp_path / "shared-runtime")
    monkeypatch.setenv(ENV_PYODIDE_DIR, str(shared))
    assert client.get("/api/labs/runtime").json()["installed"] is True
    assert client.get(f"/pyodide/{PYODIDE_VERSION}/pyodide.mjs").content == FILES["pyodide.mjs"]
    (shared / "pyodide.mjs").write_bytes(b"tampered, other size")
    body = client.get("/api/labs/runtime").json()
    assert body["installed"] is False and "wrong size" in body["detail"]
    assert client.get(f"/pyodide/{PYODIDE_VERSION}/pyodide.mjs").status_code == 404
