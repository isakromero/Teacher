"""The `teacher` command line (uvicorn is patched out)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from teacher import cli
from teacher.config import ENV_HOME


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


def test_serve_defaults(uvicorn_calls: list[dict[str, Any]], tmp_path: Path) -> None:
    assert cli.main(["serve"]) == 0
    (call,) = uvicorn_calls
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8000
    assert isinstance(call["app"], FastAPI)
    assert call["app"].state.settings.root == (tmp_path / "home").resolve()


def test_serve_host_and_port(
    uvicorn_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host other than loopback needs the account gate (tests/web/test_auth.py).
    monkeypatch.setenv("TEACHER_PASSWORD", "a test password")
    cli.main(["serve", "--host", "0.0.0.0", "--port", "9123"])
    (call,) = uvicorn_calls
    assert (call["host"], call["port"]) == ("0.0.0.0", 9123)


def test_command_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["serve", "--port", "abc"])


# -- concepts -------------------------------------------------------------------------------


@pytest.fixture
def home(settings, settings_env, tmp_path: Path):
    """$TEACHER_HOME at the migrated test `settings`, with one text source `paper`."""
    from teacher.db.engine import session_scope
    from teacher.managers import KnowledgeSourceManager

    settings_env(settings)
    path = tmp_path / "paper.md"
    path.write_text("# Paper\n\nWe use attention.\n", encoding="utf-8")
    with session_scope(settings) as session:
        KnowledgeSourceManager(session, settings).add(
            path, slug="paper", kind="paper", title="A Paper", media_type="text/markdown"
        )
    return settings


def concept(name: str, role: str = "uses", aliases: list[str] | None = None) -> dict:
    return {"name": name, "aliases": aliases or [], "summary": f"About {name}.", "role": role,
            "evidence": "we use it"}


def use_fakes(monkeypatch: pytest.MonkeyPatch, client: Any, embedder: Any) -> None:
    from teacher.concept_extraction import ConceptExtractor

    monkeypatch.setattr(cli, "build_embedder", lambda models: embedder)
    monkeypatch.setattr(
        cli,
        "build_concept_extractor",
        lambda models: ConceptExtractor(client, embedder=embedder, model=models.concept),
    )


def test_concepts_extract_prints_the_summary_and_keeps_a_record(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    fake_anthropic, fake_embedder,
) -> None:
    import json

    from teacher.db.engine import session_scope
    from teacher.managers import ConceptManager

    monkeypatch.setenv("TEACHER_CONCEPT_MODEL", "claude-test-concepts")
    with session_scope(home) as session:
        existing = ConceptManager(session, home).create("soft attention", "Weights.", "user")
        existing_id = existing.id
        ConceptManager(session, home).create("entropy", "Uncertainty.", "user")
    embedder = fake_embedder()
    fake = fake_anthropic(
        fake_anthropic.concepts_reply([
            concept("attention", aliases=["attn"]), concept("entropy"),
            concept("layer normalisation", role="introduces"),
        ]),
        fake_anthropic.judge_reply([{"candidate": 1, "verdict": "same_as",
                                     "concept_id": existing_id, "reason": "Same weighting."}]),
    )
    use_fakes(monkeypatch, fake, embedder)

    assert cli.main(["concepts", "extract", "paper"]) == 0
    out = capsys.readouterr().out
    assert fake.calls[0]["model"] == "claude-test-concepts"
    assert "Extracted 3 concepts from 'paper' with claude-test-concepts" in out
    assert "Embedded 2 stale concept(s) first." in out
    assert "Created 1:\n  + layer-normalisation [introduces] layer normalisation\n" in out
    assert "Matched exactly 1:\n  = entropy [uses] entropy <- entropy\n" in out
    assert ("Matched by the judge 1:\n  ~ soft-attention [uses] soft attention <- attention: "
            "Same weighting.\n      aliases added: attention, attn\n") in out
    assert "Links: replaced 0 previous link(s) with 3." in out
    record_file = home.derived_dir / "paper" / "concepts.json"
    assert f"Record: {record_file}" in out
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["trigger"] == "cli" and len(record["output"]["links"]) == 3

    # Again: everything now matches exactly and the links are replaced.
    fake = fake_anthropic(fake_anthropic.concepts_reply([
        concept("attention", aliases=["attn"]), concept("entropy"),
    ]))
    use_fakes(monkeypatch, fake, embedder)
    assert cli.main(["concepts", "extract", "paper"]) == 0
    out = capsys.readouterr().out
    assert "Created 0:" in out and "Matched exactly 2:" in out and "Matched by the judge 0:" in out
    assert "  = soft-attention [uses] soft attention <- attention\n" in out
    assert "Links: replaced 3 previous link(s) with 2." in out


def test_concepts_extract_failures_exit_1(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    fake_anthropic, fake_embedder,
) -> None:
    embedder = fake_embedder()
    use_fakes(monkeypatch, fake_anthropic(), embedder)
    assert cli.main(["concepts", "extract", "nope"]) == 1
    assert capsys.readouterr().err == "error: no knowledge source with slug 'nope'\n"

    empty = fake_anthropic.concepts_reply([])  # rejected, asked for once more, rejected again
    use_fakes(monkeypatch, fake_anthropic(empty, empty), embedder)
    assert cli.main(["concepts", "extract", "paper"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: Concepts could not be extracted: Claude listed 0 concepts")

    embedder.available = False
    client = fake_anthropic()
    use_fakes(monkeypatch, client, embedder)
    assert cli.main(["concepts", "extract", "paper"]) == 1
    assert "no OpenAI API key (fake)" in capsys.readouterr().err
    assert client.calls == []  # checked before any call
    assert not (home.derived_dir / "paper" / "concepts.json").exists()


def test_concepts_extract_with_the_real_defaults_needs_keys(
    home, capsys: pytest.CaptureFixture[str], forbid_real_messages_api: None
) -> None:
    assert cli.main(["concepts", "extract", "paper"]) == 1
    err = capsys.readouterr().err
    assert "Anthropic credentials could not be loaded" in err
    assert "no OpenAI API key was found" in err


def test_concepts_reindex(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_embedder
) -> None:
    from teacher.db.engine import session_scope
    from teacher.managers import ConceptEmbeddingManager, ConceptManager

    with session_scope(home) as session:
        for name in ("attention", "entropy"):
            ConceptManager(session, home).create(name, f"About {name}.", "user")
    from teacher.vector_index import index_dir

    embedder = fake_embedder()
    monkeypatch.setattr(cli, "build_embedder", lambda models: embedder)
    folder = index_dir(home, "fake-embed", 64)
    assert cli.main(["concepts", "reindex"]) == 0
    assert capsys.readouterr().out == (
        "Embedded 2 stale concept(s) with fake-embed.\n"
        f"Vector index (64 dimensions): 2 added or replaced, 0 removed, 2 in total: {folder}\n"
    )
    assert cli.main(["concepts", "reindex"]) == 0
    assert capsys.readouterr().out == (
        "Embedded 0 stale concept(s) with fake-embed.\n"
        f"Vector index (64 dimensions): 0 added or replaced, 0 removed, 2 in total: {folder}\n"
    )
    with session_scope(home) as session:
        assert ConceptEmbeddingManager(session, home).stale("fake-embed") == []

    with session_scope(home) as session:
        ConceptManager(session, home).create("softmax", "Normalised exponentials.", "user")
    embedder.fail_on = {len(embedder.calls) + 1}
    assert cli.main(["concepts", "reindex"]) == 1
    assert capsys.readouterr().err.startswith("error: Embedding failed: fake failure on call")


def test_concepts_reindex_rebuild_index_repairs_a_corrupt_index(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_embedder
) -> None:
    from teacher import vector_index
    from teacher.db.engine import session_scope
    from teacher.managers import ConceptManager

    with session_scope(home) as session:
        for name in ("attention", "entropy", "softmax"):
            ConceptManager(session, home).create(name, f"About {name}.", "user")
    embedder = fake_embedder()
    monkeypatch.setattr(cli, "build_embedder", lambda models: embedder)
    assert cli.main(["concepts", "reindex"]) == 0
    folder = vector_index.index_dir(home, "fake-embed", 64)
    capsys.readouterr()

    (folder / "manifest.json").write_text("{not json", encoding="utf-8")
    vector_index.clear_cache()  # as a new process: the index is read from disk
    assert cli.main(["concepts", "reindex"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Embedded 0 stale concept(s) with fake-embed.\n"
    assert "manifest.json is unreadable" in captured.err
    assert "`teacher concepts reindex --rebuild-index`" in captured.err

    assert cli.main(["concepts", "reindex", "--rebuild-index"]) == 0
    assert capsys.readouterr().out == (
        "Embedded 0 stale concept(s) with fake-embed.\n"
        f"Rebuilt the vector index (64 dimensions) from 3 embedding(s): {folder}\n"
    )
    vector_index.clear_cache()
    assert cli.main(["concepts", "reindex"]) == 0
    assert "0 added or replaced, 0 removed, 3 in total" in capsys.readouterr().out


def test_concepts_reindex_with_no_concepts(
    home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_embedder
) -> None:
    monkeypatch.setattr(cli, "build_embedder", lambda models: fake_embedder())
    assert cli.main(["concepts", "reindex", "--rebuild-index"]) == 0
    assert capsys.readouterr().out == (
        "Embedded 0 stale concept(s) with fake-embed.\n"
        "There are no fake-embed embeddings, so there is no vector index.\n"
    )


def test_concepts_reindex_without_a_key(home, capsys: pytest.CaptureFixture[str]) -> None:
    from teacher.db.engine import session_scope
    from teacher.managers import ConceptManager

    with session_scope(home) as session:
        ConceptManager(session, home).create("attention", "Weights.", "user")
    assert cli.main(["concepts", "reindex"]) == 1
    assert "no OpenAI API key was found" in capsys.readouterr().err


def test_concepts_needs_an_action() -> None:
    with pytest.raises(SystemExit):
        cli.main(["concepts"])
    with pytest.raises(SystemExit):
        cli.main(["concepts", "extract"])


# -- labs -----------------------------------------------------------------------------------


@pytest.fixture
def fake_cdn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A fake Pyodide release behind a MockTransport client (`labs setup` downloads it); the
    pinned hashes are replaced by the fake files' hashes. `missing` names files that 404."""
    import hashlib
    import json

    import httpx2

    from teacher import pyodide_runtime as rt

    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    monkeypatch.delenv(rt.ENV_PYODIDE_DIR, raising=False)
    wheels = {name: f"{name}-1.0-py3-none-any.whl" for name in ("numpy", "matplotlib")}
    files = {name: f"wheel {name}".encode() for name in wheels.values()}
    lock = {"packages": {
        "numpy": {"depends": [], "file_name": wheels["numpy"],
                  "sha256": hashlib.sha256(files[wheels["numpy"]]).hexdigest()},
        "matplotlib": {"depends": ["numpy"], "file_name": wheels["matplotlib"],
                       "sha256": hashlib.sha256(files[wheels["matplotlib"]]).hexdigest()},
    }}
    files[rt.LOCK_FILE] = json.dumps(lock).encode()
    for name in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"):
        files[name] = f"core {name}".encode()
    monkeypatch.setattr(rt, "CORE_FILES", {
        name: hashlib.sha256(files[name]).hexdigest()
        for name in (rt.LOCK_FILE, "pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                     "python_stdlib.zip")
    })
    missing: set[str] = set()

    def handler(request: httpx2.Request) -> httpx2.Response:
        name = str(request.url)[len(rt.CDN_URL):]
        if name in missing or name not in files:
            return httpx2.Response(404)
        return httpx2.Response(200, content=files[name])

    monkeypatch.setattr(
        cli, "build_download_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return missing


def test_labs_setup_installs_and_prints_where(
    fake_cdn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from teacher.pyodide_runtime import PYODIDE_VERSION, PythonRunner, RuntimeUnavailableError

    def no_node(self) -> None:
        raise RuntimeUnavailableError("Node.js ('node') was not found")

    monkeypatch.setattr(PythonRunner, "check_available", no_node)
    target = (tmp_path / "home").resolve() / "derived" / "pyodide" / PYODIDE_VERSION
    assert cli.main(["labs", "setup"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"Downloading Pyodide {PYODIDE_VERSION} from https://cdn.jsdelivr.net/")
    assert "[1/6] matplotlib-1.0-py3-none-any.whl" in out
    assert out[-3] == f"Installed Pyodide {PYODIDE_VERSION} at {target}"
    assert out[-2] == f"Runtime: {target}"
    assert out[-1] == "Labs cannot be checked on this server yet: Node.js ('node') was not found"
    assert (target / "teacher-runtime.json").is_file()

    monkeypatch.setattr(PythonRunner, "check_available", lambda self: None)
    assert cli.main(["labs", "setup"]) == 0
    assert capsys.readouterr().out == (
        f"Pyodide {PYODIDE_VERSION} is already installed at {target}\n"
        f"Runtime: {target}\n"
        "Labs can run in the browser and be checked on this server (Node found).\n"
    )


def test_labs_setup_failure_exits_1_and_installs_nothing(
    fake_cdn, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_cdn.add("numpy-1.0-py3-none-any.whl")
    assert cli.main(["labs", "setup"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: https://cdn.jsdelivr.net/pyodide/")
    assert err.endswith("numpy-1.0-py3-none-any.whl answered with HTTP 404\n")
    assert list((tmp_path / "home" / "derived" / "pyodide").iterdir()) == []


def test_labs_needs_an_action() -> None:
    with pytest.raises(SystemExit):
        cli.main(["labs"])
