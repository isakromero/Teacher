"""Lab endpoints (`teacher.web.routers.labs`) with the offline `StubLabGenerator`: making a lab
from a study page, listing, reading, the workspace and its limits, runs and checks, the
solution, the zip export, regenerate and delete through the artifact endpoints, the endpoints a
lab has no use for, and the page routes."""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator, stub_lab
from teacher.managers import ArtifactManager, LabManager
from teacher.web.app import create_app, fail_interrupted_generations

Json = dict[str, Any]
STUB = stub_lab("study-deep-learning")
STARTER = STUB.files[0].content
SOLUTION = STUB.solution[0].content
CHECK_STEP = 2  # the stub's implement step (indexes count from 0)


def lab_files(content: str = STARTER, *extra: tuple[str, str]) -> list[Json]:
    return [{"path": "attention.py", "content": content}] + [
        {"path": path, "content": text} for path, text in extra
    ]


@pytest.fixture
def page(client: TestClient, upload_source: Callable[..., Json]) -> Json:
    """A ready study page, `study-deep-learning`."""
    upload_source(title="Deep Learning", slug="deep-learning")
    created = client.post("/api/artifacts", json={"sources": ["deep-learning"]})
    assert created.status_code == 202, created.text
    slug = created.json()["slug"]
    assert client.get(f"/api/artifacts/{slug}").json()["status"] == "ready"
    return client.get(f"/api/artifacts/{slug}").json()


@pytest.fixture
def make_lab(client: TestClient, page: Json) -> Callable[..., Json]:
    """`make_lab(**body)`: POST a lab of `page`, assert 202, return the lab (now ready)."""

    def make(**body: Any) -> Json:
        response = client.post(f"/api/artifacts/{page['slug']}/labs", json=body or None)
        assert response.status_code == 202, response.text
        return client.get(f"/api/artifacts/{response.json()['slug']}").json()

    return make


@pytest.fixture
def lab(make_lab: Callable[..., Json]) -> Json:
    return make_lab()


def run_body(**overrides: Any) -> Json:
    body: Json = {"kind": "check", "step": CHECK_STEP, "files": lab_files(), "passed": False,
                  "output": "", "error": "AssertionError: not yet"}
    body.update(overrides)
    return body


# -- making labs ----------------------------------------------------------------------------


def test_fixture_app_uses_the_stub_lab_generator(client: TestClient) -> None:
    lab_generator = client.app.state.lab_generator
    assert isinstance(lab_generator, StubLabGenerator) and lab_generator.runner is None
    assert client.app.state.lab_runner is None


def test_create_a_lab_from_a_study_page(client: TestClient, page: Json, settings: Settings) -> None:
    response = client.post(f"/api/artifacts/{page['slug']}/labs", json={"model": None})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["slug"] == "lab-deep-learning"
    assert body["kind"] == "lab"
    assert body["status"] == "generating"
    assert body["title"] == "Lab: Deep Learning"
    assert body["generator"] == "stub-lab-v1"
    assert body["params"] == {"study_page": "study-deep-learning"} and body["model"] is None
    assert body["sources"] == page["sources"]
    assert body["progress"] == {"total_blocks": 0, "gates_total": 0, "gates_cleared": 0,
                                "complete": False}

    ready = client.get("/api/artifacts/lab-deep-learning").json()
    assert ready["status"] == "ready"
    assert ready["title"] == STUB.title
    assert ready["summary"].startswith("This placeholder lab was made without a language model.")
    assert ready["progress"] == {"total_blocks": 0, "gates_total": 1, "gates_cleared": 0,
                                 "complete": False}
    runs = client.get("/api/artifacts/lab-deep-learning/runs").json()
    assert [(r["stage"], r["status"]) for r in runs] == [
        ("preflight", "succeeded"), ("write", "succeeded")
    ]
    assert (settings.artifacts_dir / "lab-deep-learning" / "lab.json").is_file()

    no_body = client.post(f"/api/artifacts/{page['slug']}/labs")  # the body is optional
    assert no_body.status_code == 202 and no_body.json()["slug"] == "lab-deep-learning-2"


def test_create_lab_errors(client: TestClient, page: Json, lab: Json, settings: Settings) -> None:
    assert client.post("/api/artifacts/nope/labs").status_code == 404
    of_a_lab = client.post(f"/api/artifacts/{lab['slug']}/labs")
    assert of_a_lab.status_code == 422
    assert "is a lab, not a study page; labs are made from study pages" in of_a_lab.json()["detail"]

    quiz = client.post("/api/artifacts", json={"sources": ["deep-learning"], "kind": "quiz"})
    of_a_quiz = client.post(f"/api/artifacts/{quiz.json()['slug']}/labs")
    assert of_a_quiz.status_code == 422 and "is a quiz, not a study page" in of_a_quiz.text

    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        manager.mark_failed(manager.get_by_slug(page["slug"]))
    not_ready = client.post(f"/api/artifacts/{page['slug']}/labs")
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"] == "the study page 'study-deep-learning' is failed, not ready"


def test_create_lab_body_rules(client: TestClient, page: Json) -> None:
    model = client.post(f"/api/artifacts/{page['slug']}/labs", json={"model": "claude-opus-5-5"})
    assert model.status_code == 422
    assert "stub-lab-v1) does not support choosing a model" in model.json()["detail"]
    extra = client.post(f"/api/artifacts/{page['slug']}/labs", json={"title": "x"})
    assert extra.status_code == 422
    assert client.get(f"/api/artifacts/{page['slug']}/labs").json() == []  # nothing was made


def test_a_lab_cannot_be_created_as_a_plain_artifact(client: TestClient, page: Json) -> None:
    response = client.post(
        "/api/artifacts",
        json={"sources": ["deep-learning"], "kind": "lab", "params": {"study_page": page["slug"]}},
    )
    assert response.status_code == 422
    assert "POST /api/artifacts/{slug}/labs" in response.json()["detail"]


def test_list_the_labs_of_a_study_page(
    client: TestClient, page: Json, make_lab: Callable[..., Json]
) -> None:
    assert client.get(f"/api/artifacts/{page['slug']}/labs").json() == []
    first, second = make_lab(), make_lab()
    listed = client.get(f"/api/artifacts/{page['slug']}/labs").json()
    assert [a["slug"] for a in listed] == [second["slug"], first["slug"]]  # newest first
    assert all(a["kind"] == "lab" for a in listed)

    newer = client.post(f"/api/artifacts/{first['slug']}/regenerate").json()
    listed = client.get(f"/api/artifacts/{page['slug']}/labs").json()
    assert [a["slug"] for a in listed] == [newer["slug"], second["slug"]]  # not the superseded

    other = client.post("/api/artifacts", json={"sources": ["deep-learning"], "slug": "other"})
    assert other.status_code == 202
    assert client.get("/api/artifacts/other/labs").json() == []  # only labs of that page
    assert client.get(f"/api/artifacts/{first['slug']}/labs").status_code == 422
    assert client.get("/api/artifacts/nope/labs").status_code == 404

    every = client.get("/api/artifacts").json()  # the plain listing has labs too, by kind
    assert sorted(a["kind"] for a in every) == ["lab", "lab", "lab", "study_page", "study_page"]
    assert len(client.get("/api/artifacts?kind=lab").json()) == 3


# -- reading a lab --------------------------------------------------------------------------


def test_get_lab(client: TestClient, page: Json, lab: Json) -> None:
    response = client.get(f"/api/labs/{lab['slug']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "slug": lab["slug"],
        "title": STUB.title,
        "status": "ready",
        "study_page": {"slug": page["slug"], "title": page["title"]},
        "brief": STUB.brief,
        "packages": ["numpy"],
        "entry": "attention.py",
        "starter_files": lab_files(),
        "files": lab_files(),
        "steps": [
            {"index": index, "title": step.title, "instructions": step.instructions,
             "kind": step.kind, "check": step.check, "hint": step.hint, "passed": False,
             "attempts": 0}
            for index, step in enumerate(STUB.steps)
        ],
        "solution_revealed": False,
    }
    assert "solution" not in body  # never sent until asked for


def test_lab_endpoints_need_a_ready_lab(client: TestClient, page: Json, settings: Settings,
                                        lab: Json) -> None:
    calls = [
        ("get", "", None),
        ("put", "/workspace", {"files": lab_files()}),
        ("post", "/runs", run_body()),
        ("get", "/solution", None),
        ("get", "/export", None),
    ]
    for method, path, body in calls:
        assert client.request(method, f"/api/labs/nope{path}", json=body).status_code == 404
        study = client.request(method, f"/api/labs/{page['slug']}{path}", json=body)
        assert study.status_code == 422, path
        assert study.json()["detail"] == (
            "artifact 'study-deep-learning' is a study_page, not a lab"
        )
    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        manager.mark_failed(manager.get_by_slug(lab["slug"]))
    for method, path, body in calls:
        failed = client.request(method, f"/api/labs/{lab['slug']}{path}", json=body)
        assert failed.status_code == 409, path
        assert failed.json()["detail"] == f"lab {lab['slug']!r} is failed, not ready"


def test_a_ready_lab_without_its_spec_fails_loudly(
    client: TestClient, lab: Json, settings: Settings
) -> None:
    (settings.artifacts_dir / lab["slug"] / "lab.json").unlink()
    response = client.get(f"/api/labs/{lab['slug']}")
    assert response.status_code == 422
    assert response.json()["detail"] == f"lab {lab['slug']!r} has no lab.json"
    assert client.get(f"/api/artifacts/{lab['slug']}").status_code == 422  # its progress too


# -- the workspace --------------------------------------------------------------------------


def test_save_the_workspace(client: TestClient, lab: Json) -> None:
    files = lab_files("print('mine')\n", ("helper.py", "X = 1\n"))
    response = client.put(f"/api/labs/{lab['slug']}/workspace", json={"files": files})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == files
    assert body["updated_at"].endswith("+00:00")
    shown = client.get(f"/api/labs/{lab['slug']}").json()
    assert shown["files"] == files
    assert shown["starter_files"] == lab_files()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (lab_files("", *((f"f{n}.py", "") for n in range(8))), "9 files; a lab workspace holds at most 8"),
        (lab_files("x" * 200_001), "200001 characters; at most 200000 are allowed in total"),
        ([{"path": "other.py", "content": ""}], "must contain the lab's entry file 'attention.py'"),
        (lab_files("", ("Bad.py", "")), "must be a flat Python file name"),
        (lab_files("", ("../x.py", "")), "must be a flat Python file name"),
        (lab_files("", ("attention.py", "")), "more than one file named attention.py"),
        ([], "the workspace has no files"),
    ],
)
def test_workspace_limits(client: TestClient, lab: Json, files: list[Json], message: str) -> None:
    response = client.put(f"/api/labs/{lab['slug']}/workspace", json={"files": files})
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert client.get(f"/api/labs/{lab['slug']}").json()["files"] == lab_files()


def test_workspace_body_rules(client: TestClient, lab: Json) -> None:
    for body in ({}, {"files": lab_files(), "extra": 1},
                 {"files": [{"path": "attention.py", "content": "", "mode": "x"}]}):
        assert client.put(f"/api/labs/{lab['slug']}/workspace", json=body).status_code == 422


# -- runs and checks ------------------------------------------------------------------------


def test_record_runs_and_checks(client: TestClient, lab: Json, settings: Settings) -> None:
    url = f"/api/labs/{lab['slug']}/runs"
    ran = client.post(url, json=run_body(kind="run", step=None, passed=True, output="hi",
                                          error=None, files=lab_files("print('hi')\n")))
    assert ran.status_code == 201, ran.text
    assert [(s["index"], s["passed"], s["attempts"]) for s in ran.json()["steps"]] == [
        (0, False, 0), (1, False, 0), (2, False, 0), (3, False, 0)
    ]
    assert client.get(f"/api/labs/{lab['slug']}").json()["files"] == lab_files("print('hi')\n")

    failed = client.post(url, json=run_body()).json()["steps"][CHECK_STEP]
    assert (failed["passed"], failed["attempts"]) == (False, 1)
    passed = client.post(url, json=run_body(passed=True, error=None, files=lab_files(SOLUTION)))
    step = passed.json()["steps"][CHECK_STEP]
    assert (step["passed"], step["attempts"], step["title"]) == (True, 2, "Implement attention")
    assert step["check"] == STUB.steps[CHECK_STEP].check

    shown = client.get(f"/api/labs/{lab['slug']}").json()
    assert shown["files"] == lab_files(SOLUTION)
    assert (shown["steps"][CHECK_STEP]["passed"], shown["steps"][CHECK_STEP]["attempts"]) == (
        True, 2
    )
    progress = client.get(f"/api/artifacts/{lab['slug']}").json()["progress"]
    assert progress == {"total_blocks": 0, "gates_total": 1, "gates_cleared": 1, "complete": True}

    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(lab["slug"])
        rows = LabManager(session, settings).runs(artifact)
        assert [(r.kind, r.step_index, r.passed, r.output, r.error) for r in rows] == [
            ("run", None, True, "hi", None),
            ("check", 2, False, "", "AssertionError: not yet"),
            ("check", 2, True, "", None),
        ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"step": None}, "a check must name its step"),
        ({"step": 0}, "is an explore step and has no check"),
        ({"step": 7}, "step 7 does not exist"),
        ({"output": "x" * 20_001}, "the output has 20001 characters; at most 20000"),
        ({"files": [{"path": "other.py", "content": ""}]}, "entry file 'attention.py'"),
        ({"kind": "test"}, "kind: Input should be 'run' or 'check'"),
        ({"passed": None}, "passed"),
        ({"extra": 1}, "extra"),
    ],
)
def test_run_rules(client: TestClient, lab: Json, overrides: Json, message: str) -> None:
    response = client.post(f"/api/labs/{lab['slug']}/runs", json=run_body(**overrides))
    assert response.status_code == 422
    assert message in response.json()["detail"]
    shown = client.get(f"/api/labs/{lab['slug']}").json()
    assert shown["steps"][CHECK_STEP]["attempts"] == 0 and shown["files"] == lab_files()


def test_output_at_the_limit_is_accepted(client: TestClient, lab: Json) -> None:
    response = client.post(f"/api/labs/{lab['slug']}/runs", json=run_body(output="x" * 20_000))
    assert response.status_code == 201


# -- the solution and the export ------------------------------------------------------------


def test_reveal_the_solution(client: TestClient, lab: Json) -> None:
    response = client.get(f"/api/labs/{lab['slug']}/solution")
    assert response.status_code == 200
    assert response.json() == {"files": lab_files(SOLUTION)}
    shown = client.get(f"/api/labs/{lab['slug']}").json()
    assert shown["solution_revealed"] is True
    assert shown["files"] == lab_files()  # the reader's files are not replaced


def extract(content: bytes, target: Path) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        archive.extractall(target)
        return sorted(archive.namelist())


def test_export_a_lab(client: TestClient, lab: Json, tmp_path: Path) -> None:
    mine = lab_files(STARTER + "\n# my notes\n")
    client.put(f"/api/labs/{lab['slug']}/workspace", json={"files": mine})
    response = client.get(f"/api/labs/{lab['slug']}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f'attachment; filename="{lab["slug"]}.zip"'
    folder = tmp_path / "export"
    assert extract(response.content, folder) == [
        "README.md", "attention.py", "requirements.txt", "tests/test_step_3.py"
    ]
    assert (folder / "attention.py").read_text() == mine[0]["content"]  # the reader's files
    assert (folder / "requirements.txt").read_text() == "numpy\npytest\n"
    readme = (folder / "README.md").read_text()
    assert readme.startswith(f"# {STUB.title}\n\n{STUB.brief}\n")
    assert "`python attention.py`" in readme and "`python -m pytest tests`" in readme
    for number, step in enumerate(STUB.steps, start=1):
        assert f"### Step {number}: {step.title} ({step.kind})" in readme
        assert step.instructions in readme
    assert "Hint: Look at the largest entry" in readme
    assert "Checked by `tests/test_step_3.py`." in readme
    assert "softmax(scores, axis=-1)" not in readme and SOLUTION not in readme  # no solution


def test_the_exported_check_runs_under_pytest(client: TestClient, lab: Json, tmp_path: Path) -> None:
    """The exported test fails on the starter files and passes on the solution."""

    def pytest_in(files: list[Json]) -> subprocess.CompletedProcess:
        client.put(f"/api/labs/{lab['slug']}/workspace", json={"files": files})
        folder = tmp_path / f"run-{len(list(tmp_path.iterdir()))}"
        extract(client.get(f"/api/labs/{lab['slug']}/export").content, folder)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=folder, capture_output=True, text=True, timeout=120,
        )

    starter = pytest_in(lab_files())
    assert starter.returncode == 1, starter.stdout
    assert "the weights should be the softmax" in starter.stdout
    solution = pytest_in(lab_files(SOLUTION))
    assert solution.returncode == 0, solution.stdout + solution.stderr
    assert "1 passed" in solution.stdout


# -- the artifact endpoints on a lab --------------------------------------------------------


def test_regenerate_a_lab(client: TestClient, page: Json, lab: Json) -> None:
    response = client.post(f"/api/artifacts/{lab['slug']}/regenerate")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["slug"] == "lab-deep-learning-2"
    assert body["kind"] == "lab" and body["generator"] == "stub-lab-v1"
    assert body["params"] == {"study_page": page["slug"]}
    assert body["supersedes"] == lab["slug"]
    assert client.get(f"/api/artifacts/{body['slug']}").json()["status"] == "ready"
    assert client.get(f"/api/artifacts/{lab['slug']}").json()["superseded_by"] == body["slug"]

    kept = client.post(f"/api/artifacts/{body['slug']}/regenerate", json={"params": {"n": 1}})
    assert kept.status_code == 202
    assert kept.json()["params"] == {"n": 1, "study_page": page["slug"]}  # the page is kept
    moved = client.post(f"/api/artifacts/{body['slug']}/regenerate",
                        json={"params": {"study_page": "other"}})
    assert moved.status_code == 422
    assert "a regenerated lab keeps its study page 'study-deep-learning'" in moved.text


def test_regenerate_needs_the_study_page(client: TestClient, page: Json, lab: Json,
                                        settings: Settings) -> None:
    with session_scope(settings) as session:
        manager = ArtifactManager(session, settings)
        manager.mark_failed(manager.get_by_slug(page["slug"]))
    not_ready = client.post(f"/api/artifacts/{lab['slug']}/regenerate")
    assert not_ready.status_code == 409 and "is failed, not ready" in not_ready.text

    assert client.delete(f"/api/artifacts/{page['slug']}").status_code == 204
    assert client.get(f"/api/labs/{lab['slug']}").json()["study_page"] is None
    gone = client.post(f"/api/artifacts/{lab['slug']}/regenerate")
    assert gone.status_code == 404
    assert gone.json()["detail"] == (
        f"the study page 'study-deep-learning' of lab {lab['slug']!r} no longer exists"
    )


def test_delete_a_lab(client: TestClient, lab: Json, settings: Settings) -> None:
    client.post(f"/api/labs/{lab['slug']}/runs", json=run_body())
    client.get(f"/api/labs/{lab['slug']}/solution")
    assert client.delete(f"/api/artifacts/{lab['slug']}").status_code == 204
    assert client.get(f"/api/artifacts/{lab['slug']}").status_code == 404
    assert client.get(f"/api/labs/{lab['slug']}").status_code == 404
    assert not (settings.artifacts_dir / lab["slug"]).exists()
    from sqlalchemy import func, select

    from teacher.db.models import LabRunRecord, LabWorkspace

    with session_scope(settings) as session:
        assert session.scalar(select(func.count()).select_from(LabRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(LabWorkspace)) == 0


def test_a_source_with_a_lab_is_in_use(client: TestClient, page: Json, lab: Json) -> None:
    detail = client.get("/api/sources/deep-learning").json()
    assert sorted((a["kind"], a["slug"]) for a in detail["artifacts"]) == [
        ("lab", lab["slug"]), ("study_page", page["slug"])
    ]
    response = client.delete("/api/sources/deep-learning")
    assert response.status_code == 409 and "used by 2 artifact(s)" in response.text


@pytest.mark.parametrize(
    ("method", "path", "what"),
    [
        ("get", "/blocks", "blocks"),
        ("post", "/blocks/b01/responses", "blocks"),
        ("get", "/blocks/b01/responses", "blocks"),
        ("get", "/glossary", "glossary"),
        ("post", "/glossary", "glossary"),
    ],
)
def test_study_page_endpoints_refuse_a_lab(client: TestClient, lab: Json, method: str,
                                           path: str, what: str) -> None:
    body = {"response": {"choice": 0}} if path.endswith("responses") and method == "post" else None
    response = client.request(method, f"/api/artifacts/{lab['slug']}{path}", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        f"artifact {lab['slug']!r} is a lab, which has no {what}; use /api/labs/{lab['slug']}"
    )


def test_startup_fails_an_interrupted_lab(client: TestClient, page: Json, settings: Settings) -> None:
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        study = artifacts.get_by_slug(page["slug"])
        artifacts.create(slug="lab-stuck", kind="lab", title="Lab", generator="stub-lab-v1",
                         params={"study_page": page["slug"]}, sources=artifacts.sources(study))
    assert fail_interrupted_generations(settings) == ["lab-stuck"]
    stuck = client.get("/api/artifacts/lab-stuck").json()
    assert stuck["status"] == "failed"
    assert stuck["progress"] == {"total_blocks": 0, "gates_total": 0, "gates_cleared": 0,
                                 "complete": False}


# -- the app and the pages ------------------------------------------------------------------


def test_create_app_lab_options(settings: Settings) -> None:
    runner = object()
    with pytest.raises(ValueError, match="lab_runner only configures the default lab generator"):
        create_app(settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
                   lab_runner=runner)
    stub = StubLabGenerator(runner=runner)  # type: ignore[arg-type]
    app = create_app(settings, generator=StubGenerator(), lab_generator=stub)
    assert app.state.lab_generator is stub and app.state.lab_runner is runner


def test_lab_page(client: TestClient, lab: Json) -> None:
    response = client.get(f"/labs/{lab['slug']}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert (f'data-page="lab" data-slug="{lab["slug"]}" data-status="ready"') in response.text
    assert f"<title>{STUB.title} - Teacher</title>" in response.text


def test_page_routes_redirect_between_labs_and_study_pages(
    client: TestClient, page: Json, lab: Json
) -> None:
    to_lab = client.get(f"/artifacts/{lab['slug']}", follow_redirects=False)
    assert to_lab.status_code == 307 and to_lab.headers["location"] == f"/labs/{lab['slug']}"
    to_page = client.get(f"/labs/{page['slug']}", follow_redirects=False)
    assert to_page.status_code == 307
    assert to_page.headers["location"] == f"/artifacts/{page['slug']}"
    assert client.get(f"/artifacts/{lab['slug']}").url.path == f"/labs/{lab['slug']}"
    missing = client.get("/labs/nope")
    assert missing.status_code == 404 and "There is no lab called" in missing.text


def test_the_runtime_endpoint_is_not_taken_for_a_lab_slug(client: TestClient) -> None:
    """`GET /api/labs/runtime` (teacher.web.routers.pyodide) is routed before `/api/labs/{slug}`."""
    response = client.get("/api/labs/runtime")
    assert response.status_code == 200
    assert set(response.json()) == {"installed", "version", "url", "packages", "detail"}
