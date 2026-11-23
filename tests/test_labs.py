"""The lab spec (`teacher.labs`): the model rules, `verify_lab` on a local runner (every rule,
including the vacuous-check rule), and the `lab.json` round trip."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from teacher.lab_generator import stub_lab
from teacher.labs import (
    ALLOWED_PACKAGES,
    LAB_FILE,
    LabFile,
    LabKindError,
    LabSpec,
    LabStep,
    LabVerificationError,
    read_lab,
    require_lab,
    require_not_lab,
    verify_lab,
    write_lab,
)
from teacher.managers import IntegrityCheckError

STARTER = '''\
def double(x):
    """Return twice x."""
    return 0  # TODO (step 2)


if __name__ == "__main__":
    print("double(3) =", double(3))
'''
SOLUTION = STARTER.replace("return 0  # TODO (step 2)", "return 2 * x")
CHECK = 'from calc import double\nassert double(3) == 6, f"double(3) should be 6, got {double(3)}"\n'


def step(kind: str = "explore", check: str | None = None, **kw: Any) -> dict[str, Any]:
    return {"title": f"A {kind} step", "instructions": "Do it.", "kind": kind, "check": check,
            "hint": None, **kw}


def spec_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Doubling",
        "brief": "A tiny lab.",
        "packages": [],
        "entry": "calc.py",
        "files": [{"path": "calc.py", "content": STARTER}],
        "steps": [step(), step("implement", CHECK), step("extend")],
        "solution": [{"path": "calc.py", "content": SOLUTION}],
        "study_page": "study-paper",
    }
    data.update(overrides)
    return data


def make_spec(**overrides: Any) -> LabSpec:
    return LabSpec.model_validate(spec_data(**overrides))


# -- the models -----------------------------------------------------------------------------


def test_a_valid_spec_and_its_checks() -> None:
    spec = make_spec()
    assert [s.kind for s in spec.steps] == ["explore", "implement", "extend"]
    assert spec.checked_steps() == [(1, spec.steps[1])]
    assert spec.checks() == [CHECK]
    assert ALLOWED_PACKAGES == ("numpy", "matplotlib")
    assert make_spec(packages=["numpy", "matplotlib"]).packages == ["numpy", "matplotlib"]


@pytest.mark.parametrize("path", ["Calc.py", "calc", "dir/calc.py", "1calc.py", "calc.txt",
                                  "../calc.py", "cal-c.py", ""])
def test_file_names_must_be_flat_python_names(path: str) -> None:
    with pytest.raises(ValidationError, match="flat Python file name"):
        LabFile(path=path, content="")
    assert LabFile(path="_my_file2.py", content="").path == "_my_file2.py"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"steps": [step(), step("implement", CHECK)]}, "at least 3 items"),
        ({"steps": [step()] * 6 + [step("implement", CHECK)]}, "at most 6 items"),
        ({"steps": [step(), step(), step("extend")]}, "at least one implement step"),
        ({"steps": [step(), step("implement"), step()]}, "must have a check"),
        ({"steps": [step(), step("implement", "  "), step()]}, "must have a check"),
        ({"steps": [step(check=CHECK), step("implement", CHECK), step()]},
         "explore step 'A explore step' must not have a check"),
        ({"steps": [step(), step("implement", CHECK), step("extend", CHECK)]},
         "must not have a check"),
        ({"steps": [step(kind="quiz"), step("implement", CHECK), step()]}, "kind"),
        ({"steps": [step(hint=""), step("implement", CHECK), step()]}, "hint"),
        ({"steps": [step(title=" "), step("implement", CHECK), step()]}, "title"),
        ({"files": []}, "at least 1 item"),
        ({"files": [{"path": f"f{n}.py", "content": ""} for n in range(5)]}, "at most 4 items"),
        ({"entry": "main.py"}, "entry file 'main.py' is not one of the files"),
        ({"packages": ["scipy"]}, "'scipy' are not available"),
        ({"packages": ["numpy", "numpy"]}, "more than once"),
        ({"solution": []}, "exactly the same files"),
        ({"solution": [{"path": "other.py", "content": ""}]}, "exactly the same files"),
        ({"files": [{"path": "calc.py", "content": ""}, {"path": "calc.py", "content": ""}]},
         "files name calc.py more than once"),
        ({"title": ""}, "title must not be empty"),
        ({"brief": "\n"}, "brief must not be empty"),
        ({"study_page": ""}, "study_page must not be empty"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_spec_rules(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_spec(**changes)


def test_step_extra_keys_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        LabStep.model_validate(step(points=3))


def test_kind_guards() -> None:
    class Row:
        def __init__(self, kind: str) -> None:
            self.kind, self.slug = kind, "x"

    require_lab(Row("lab"))
    require_not_lab(Row("study_page"), "blocks")
    with pytest.raises(LabKindError, match="'x' is a study_page, not a lab"):
        require_lab(Row("study_page"))
    with pytest.raises(LabKindError, match="'x' is a lab, which has no blocks; use /api/labs/x"):
        require_not_lab(Row("lab"), "blocks")
    assert issubclass(LabKindError, ValueError)  # HTTP 422


# -- lab.json -------------------------------------------------------------------------------


def test_lab_file_round_trip(tmp_path: Path) -> None:
    spec = make_spec(brief="Brief with “unicode” ü.")
    path = write_lab(tmp_path, spec)
    assert path == tmp_path / LAB_FILE
    assert json.loads(path.read_text(encoding="utf-8"))["brief"] == "Brief with “unicode” ü."
    assert "“unicode”" in path.read_text(encoding="utf-8")  # written as UTF-8, not escaped
    assert read_lab(tmp_path, slug="lab") == spec
    assert [p.name for p in tmp_path.iterdir()] == [LAB_FILE]  # no temp file left behind


def test_reading_a_missing_or_broken_lab_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(IntegrityCheckError, match="lab 'lab' has no lab.json"):
        read_lab(tmp_path, slug="lab")
    (tmp_path / LAB_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(IntegrityCheckError, match="is not a valid lab"):
        read_lab(tmp_path, slug="lab")
    data = spec_data(steps=[step(), step(), step()])
    (tmp_path / LAB_FILE).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(IntegrityCheckError, match="at least one implement step"):
        read_lab(tmp_path, slug="lab")


# -- verify_lab -----------------------------------------------------------------------------


def test_a_working_lab_passes_and_reports_both_runs(local_runner) -> None:
    spec = make_spec()
    verification = verify_lab(spec, local_runner)
    record = verification.record(spec)
    assert record["starter"]["ok"] and record["solution"]["ok"]
    assert record["starter"]["stdout"] == "double(3) = 0"
    assert record["starter"]["checks"][0]["step"] == 2
    assert record["starter"]["checks"][0]["passed"] is False
    assert "double(3) should be 6, got 0" in record["starter"]["checks"][0]["error"]
    assert record["solution"]["checks"] == [
        {"step": 2, "passed": True, "output": "", "error": None}
    ]
    assert [call["files"] for call in local_runner.calls] == [
        {"calc.py": STARTER}, {"calc.py": SOLUTION}
    ]
    assert {call["entry"] for call in local_runner.calls} == {"calc.py"}
    assert local_runner.calls[0]["checks"] == [CHECK]


def test_the_stub_lab_passes_verification(local_runner) -> None:
    spec = stub_lab("study-paper")
    verification = verify_lab(spec, local_runner)
    assert "attention weights" in verification.starter.run.stdout
    assert local_runner.calls[0]["packages"] == ["numpy"]


def test_a_starter_that_does_not_run_fails(local_runner) -> None:
    broken = STARTER.replace('print("double(3) =", double(3))', "print(undefined_name)")
    with pytest.raises(LabVerificationError) as info:
        verify_lab(make_spec(files=[{"path": "calc.py", "content": broken}]), local_runner)
    assert len(info.value.problems) == 1  # the solution still runs and passes its check
    assert info.value.problems[0].startswith("the starter program (calc.py) failed with an error")
    assert "NameError" in info.value.problems[0]


def test_a_solution_that_fails_its_check_or_does_not_run(local_runner) -> None:
    wrong = STARTER.replace("return 0  # TODO (step 2)", "return 3 * x")
    with pytest.raises(LabVerificationError, match=r"the check of step 2 \('A implement step'\) "
                       r"fails on the solution: AssertionError: double\(3\) should be 6, got 9"):
        verify_lab(make_spec(solution=[{"path": "calc.py", "content": wrong}]), local_runner)
    crashing = SOLUTION + "\nraise SystemExit('boom')\n"
    with pytest.raises(LabVerificationError, match=r"the solution program \(calc.py\) failed"):
        verify_lab(make_spec(solution=[{"path": "calc.py", "content": crashing}]), local_runner)


def test_a_check_the_starter_already_passes_is_vacuous(local_runner) -> None:
    vacuous = "from calc import double\nassert double(0) == 0\n"
    with pytest.raises(LabVerificationError) as info:
        verify_lab(make_spec(steps=[step(), step("implement", vacuous), step("extend")]),
                   local_runner)
    assert info.value.problems == (
        "the check of step 2 ('A implement step') already passes on the starter files, so it "
        "tests nothing: it must fail until the reader has done the step",
    )


def test_every_problem_is_listed_at_once(local_runner) -> None:
    second = 'from calc import double\nassert double(1) == 0, "always true on the starter"\n'
    spec = make_spec(
        steps=[step(), step("implement", CHECK), step("implement", second), step("extend")]
    )
    with pytest.raises(LabVerificationError) as info:
        verify_lab(spec, local_runner)
    assert len(info.value.problems) == 2
    assert "check of step 3" in info.value.problems[0] and "fails on the solution" in (
        info.value.problems[0]
    )
    assert "check of step 3" in info.value.problems[1] and "already passes" in (
        info.value.problems[1]
    )
    assert str(info.value).startswith("the lab failed verification (2 problems): (1) the check")


def test_a_starter_that_runs_too_long_is_reported_as_a_timeout() -> None:
    class SlowRunner:
        def check_available(self) -> None: ...

        def execute(self, files, *, entry, checks=(), packages=()):
            slow = "while" in files[entry]
            run = SimpleNamespace(ok=not slow, stdout="", stderr="", timed_out=slow, figures=0,
                                  error="the program ran for more than 60 s" if slow else None)
            check = SimpleNamespace(passed=False, output="", error="no")
            return SimpleNamespace(run=run, checks=tuple(check for _ in checks))

    spec = make_spec(files=[{"path": "calc.py", "content": "while True: pass\n"}])
    with pytest.raises(LabVerificationError, match=r"the starter program \(calc.py\) did not "
                       r"finish in time: the program ran for more than 60 s"):
        verify_lab(spec, SlowRunner())


def test_a_runner_returning_the_wrong_number_of_checks_is_a_bug() -> None:
    class BrokenRunner:
        def check_available(self) -> None: ...

        def execute(self, files, *, entry, checks=(), packages=()):
            run = SimpleNamespace(ok=True, stdout="", stderr="", error=None, timed_out=False,
                                  figures=0)
            return SimpleNamespace(run=run, checks=())

    with pytest.raises(RuntimeError, match=r"returned 0 check result\(s\) for 1 check\(s\)"):
        verify_lab(make_spec(), BrokenRunner())


def test_long_errors_are_cut_and_say_so(local_runner) -> None:
    noisy = STARTER + '\nraise ValueError("x" * 5000)\n'
    with pytest.raises(LabVerificationError, match=r"\[\.\.\. cut: \d+ more characters\]"):
        verify_lab(make_spec(files=[{"path": "calc.py", "content": noisy}]), local_runner)


def test_verification_error_needs_a_problem() -> None:
    with pytest.raises(ValueError, match="at least one problem"):
        LabVerificationError([])


def test_the_stub_lab_passes_on_the_real_runtime(settings) -> None:
    """The stub lab through the real `PythonRunner` (Pyodide under Node) and `verify_lab`.
    Needs an installed runtime named by $TEACHER_PYODIDE_DIR (see `teacher labs setup`)."""
    import os

    from teacher.pyodide_runtime import ENV_PYODIDE_DIR, PythonRunner, RuntimeUnavailableError

    if not os.environ.get(ENV_PYODIDE_DIR):
        pytest.skip(f"${ENV_PYODIDE_DIR} is not set: no installed Pyodide runtime to run labs on")
    runner = PythonRunner(settings)
    try:
        runner.check_available()
    except RuntimeUnavailableError as exc:
        pytest.skip(f"the Pyodide runtime in ${ENV_PYODIDE_DIR} cannot run: {exc}")
    spec = stub_lab("study-paper")
    verification = verify_lab(spec, runner)
    assert "attention weights (one row per query):" in verification.starter.run.stdout
    assert verification.starter.checks[0].passed is False
    assert "the weights should be the softmax" in (verification.starter.checks[0].error or "")
    assert verification.solution.checks[0].passed is True
