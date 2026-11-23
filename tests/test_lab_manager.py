"""LabManager: the spec, the workspace and its limits, recording runs, step status, the
solution."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, LabRunRecord
from teacher.lab_generator import stub_lab
from teacher.labs import LabFile, LabKindError, LabSpec, write_lab
from teacher.managers import ArtifactManager, IntegrityCheckError, LabManager
from teacher.managers.labs import MAX_OUTPUT_CHARS, MAX_WORKSPACE_CHARS, MAX_WORKSPACE_FILES


@pytest.fixture
def spec() -> LabSpec:
    return stub_lab("study-paper")  # steps: explore, explore, implement (index 2), extend


@pytest.fixture
def lab(session: Session, settings: Settings, spec: LabSpec) -> Artifact:
    artifacts = ArtifactManager(session, settings)
    artifact = artifacts.create(
        slug="lab-paper", kind="lab", title="Lab", generator="stub-lab-v1",
        params={"study_page": "study-paper"},
    )
    write_lab(artifacts.dir(artifact), spec)
    artifacts.mark_ready(artifact)
    return artifact


@pytest.fixture
def labs(session: Session, settings: Settings) -> LabManager:
    return LabManager(session, settings)


def files(content: str = "print(1)\n", *extra: str) -> list[LabFile]:
    return [LabFile(path="attention.py", content=content)] + [
        LabFile(path=name, content="") for name in extra
    ]


def run(labs: LabManager, lab: Artifact, spec: LabSpec, **kw) -> LabRunRecord:
    values = {"kind": "check", "step": 2, "files": files(), "passed": False, "output": "",
              "error": None}
    values.update(kw)
    return labs.record_run(lab, spec, **values)


def test_spec_is_read_from_lab_json(labs: LabManager, lab: Artifact, spec: LabSpec,
                                    settings: Settings, session: Session) -> None:
    assert labs.spec(lab) == spec
    (ArtifactManager(session, settings).dir(lab) / "lab.json").unlink()
    with pytest.raises(IntegrityCheckError, match="has no lab.json"):
        labs.spec(lab)


def test_only_labs(labs: LabManager, session: Session, settings: Settings, spec: LabSpec) -> None:
    page = ArtifactManager(session, settings).create(
        slug="study-paper", kind="study_page", title="S", generator="stub-v1"
    )
    for call in (lambda: labs.spec(page), lambda: labs.workspace(page),
                 lambda: labs.step_statuses(page, spec), lambda: labs.runs(page)):
        with pytest.raises(LabKindError, match="not a lab"):
            call()


def test_files_are_the_starter_until_saved(labs: LabManager, lab: Artifact, spec: LabSpec) -> None:
    assert labs.workspace(lab) is None
    assert labs.files(lab, spec) == spec.files
    saved = labs.save_workspace(lab, spec, files("x = 1\n", "helper.py"))
    first = saved.updated_at
    assert labs.files(lab, spec) == files("x = 1\n", "helper.py")
    again = labs.save_workspace(lab, spec, files("x = 2\n"))
    assert again is saved and again.updated_at >= first
    assert labs.files(lab, spec) == files("x = 2\n")


@pytest.mark.parametrize(
    ("workspace", "message"),
    [
        ([], "has no files"),
        (files("", *(f"f{n}.py" for n in range(MAX_WORKSPACE_FILES))), "at most 8"),
        (files("x" * (MAX_WORKSPACE_CHARS + 1)), "200001 characters; at most 200000"),
        ([LabFile(path="other.py", content="")], "entry file 'attention.py'"),
        (files("", "attention.py"), "more than one file named attention.py"),
    ],
)
def test_workspace_limits(labs: LabManager, lab: Artifact, spec: LabSpec,
                          workspace: list[LabFile], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        labs.save_workspace(lab, spec, workspace)
    assert labs.workspace(lab) is None  # nothing was saved


def test_workspace_at_the_limits_is_fine(labs: LabManager, lab: Artifact, spec: LabSpec) -> None:
    at_limit = files("x" * (MAX_WORKSPACE_CHARS - 2), *(f"f{n}.py" for n in range(7)))
    assert len(at_limit) == MAX_WORKSPACE_FILES
    labs.save_workspace(lab, spec, at_limit)


def test_step_status_counts_check_runs(labs: LabManager, lab: Artifact, spec: LabSpec) -> None:
    statuses = labs.step_statuses(lab, spec)
    assert [(s.index, s.passed, s.attempts) for s in statuses] == [
        (0, False, 0), (1, False, 0), (2, False, 0), (3, False, 0)
    ]
    run(labs, lab, spec, error="AssertionError: no")
    run(labs, lab, spec, kind="run", step=None, passed=True, output="ran")
    run(labs, lab, spec, kind="run", step=2, passed=True)  # a run is not an attempt
    assert [(s.passed, s.attempts) for s in labs.step_statuses(lab, spec)][2] == (False, 1)
    run(labs, lab, spec, passed=True, files=files("solved\n"))
    run(labs, lab, spec, passed=False)  # a later failure does not undo the pass
    assert [(s.passed, s.attempts) for s in labs.step_statuses(lab, spec)] == [
        (False, 0), (False, 0), (True, 3), (False, 0)
    ]
    rows = labs.runs(lab)
    assert [(r.kind, r.step_index, r.passed) for r in rows] == [
        ("check", 2, False), ("run", None, True), ("run", 2, True), ("check", 2, True),
        ("check", 2, False),
    ]
    assert rows[0].error == "AssertionError: no" and rows[1].output == "ran"
    assert rows[3].files == [{"path": "attention.py", "content": "solved\n"}]
    assert labs.files(lab, spec) == files()  # every run saves its files as the workspace


def test_other_labs_runs_do_not_count(labs: LabManager, lab: Artifact, spec: LabSpec,
                                      session: Session, settings: Settings) -> None:
    artifacts = ArtifactManager(session, settings)
    other = artifacts.create(slug="lab-2", kind="lab", title="L", generator="g",
                             params={"study_page": "study-paper"})
    run(labs, other, spec, passed=True)
    assert labs.step_statuses(lab, spec)[2].attempts == 0


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"kind": "test"}, "invalid run kind 'test'"),
        ({"step": None}, "a check must name its step"),
        ({"step": 0}, "step 0 \\('Run and predict'\\) is an explore step and has no check"),
        ({"step": 4}, "step 4 does not exist: this lab's steps are numbered 0 to 3"),
        ({"step": -1}, "step -1 does not exist"),
        ({"kind": "run", "step": 9}, "step 9 does not exist"),
        ({"step": True}, "step must be an integer"),
        ({"passed": "yes"}, "passed must be true or false"),
        ({"output": "x" * (MAX_OUTPUT_CHARS + 1)}, "20001 characters; at most 20000"),
        ({"files": [LabFile(path="other.py", content="")]}, "entry file"),
    ],
)
def test_record_run_rules(labs: LabManager, lab: Artifact, spec: LabSpec, values: dict,
                          message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run(labs, lab, spec, **values)
    assert labs.runs(lab) == [] and labs.workspace(lab) is None


def test_output_at_the_limit_is_recorded(labs: LabManager, lab: Artifact, spec: LabSpec) -> None:
    assert run(labs, lab, spec, output="x" * MAX_OUTPUT_CHARS).output == "x" * MAX_OUTPUT_CHARS


def test_reveal_solution(labs: LabManager, lab: Artifact, spec: LabSpec) -> None:
    assert labs.solution_revealed(lab) is False
    assert labs.reveal_solution(lab, spec) == spec.solution
    workspace = labs.workspace(lab)
    assert workspace is not None and workspace.solution_revealed_at is not None
    assert labs.files(lab, spec) == spec.files  # the reveal does not change the reader's files
    first = workspace.solution_revealed_at
    labs.save_workspace(lab, spec, files("mine\n"))
    labs.reveal_solution(lab, spec)
    assert labs.workspace(lab).solution_revealed_at == first  # the first reveal is kept
    assert labs.solution_revealed(lab) is True
    assert labs.files(lab, spec) == files("mine\n")
