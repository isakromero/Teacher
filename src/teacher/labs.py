"""Labs: hands-on coding follow-ups to a study page, as a checked spec stored in `lab.json`.

A lab is an `artifacts` row with `kind = "lab"`, linked to the same sources as the study page it
was made from, with `params["study_page"]` naming that page. It has no blocks: its whole content
is a `LabSpec`, saved as `lab.json` in the artifact's folder (`write_lab` / `read_lab`):

- `brief`: what the lab is about (Markdown);
- `files`: one to four small Python files (flat names such as `attention.py`) that run as they
  are; `entry` is the one that runs as `__main__`;
- `steps`: three to six staged steps, each `explore` (run, predict, change a parameter,
  explain), `implement` (write a missing piece; has a `check`) or `extend` (open-ended). Only
  implement steps have a check, and every lab has at least one;
- `solution`: the reference version of exactly the same files, hidden until the reader asks;
- `packages`: the packages the code imports, a subset of `ALLOWED_PACKAGES`.

A check is a Python snippet that runs after the entry, in a fresh namespace, with the files
importable (`from attention import softmax`); it passes iff it raises nothing. The runtime that
runs them (Pyodide; `teacher.pyodide_runtime.PythonRunner` on the server) is anything with the
`LabRunner` shape below.

A check is run by the runtime; the results are `teacher.pyodide_runtime.LabRun` (a `RunResult`
and one `CheckResult` per check).

`verify_lab(spec, runner)` is what makes a generated lab trustworthy. It raises
`LabVerificationError`, listing every problem it found, unless:

1. the starter files run without error;
2. the solution runs without error and passes every check;
3. the starter fails every check (a check the starter already passes tests nothing).

The pydantic models forbid extra keys and hold every rule that needs no runtime (file names,
step kinds, the solution's paths, the packages), so a `LabSpec` that exists is well formed.
`LabKindError` (a ValueError, so HTTP 422) is raised where an endpoint needs a lab and gets
another kind of artifact, or the other way round.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from teacher.pyodide_runtime import ALLOWED_PACKAGES, LabRun

LAB_KIND = "lab"
STUDY_PAGE_KIND = "study_page"
STUDY_PAGE_PARAM = "study_page"  # artifacts.params key of a lab: the slug of its study page
LAB_FILE = "lab.json"

FILE_PATH_PATTERN = r"^[a-z_][a-z0-9_]*\.py$"
_FILE_PATH_RE = re.compile(FILE_PATH_PATTERN)
MIN_FILES, MAX_FILES = 1, 4
MIN_STEPS, MAX_STEPS = 3, 6
STEP_KINDS: tuple[str, ...] = ("explore", "implement", "extend")

# What a problem message quotes of a program's error or output at most (a traceback can be long).
MAX_QUOTED_CHARS = 3000

StepKind = Literal["explore", "implement", "extend"]


class LabVerificationError(Exception):
    """The lab does not work as a lab: its starter or solution fails, or a check is vacuous.

    `problems` lists every problem found, each a self-contained sentence naming the step or
    the program concerned; the message joins them."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        if not self.problems:
            raise ValueError("LabVerificationError needs at least one problem")
        count = len(self.problems)
        heading = "the lab failed verification" + (f" ({count} problems)" if count > 1 else "")
        super().__init__(heading + ": " + " ".join(
            f"({n}) {p}" if count > 1 else p for n, p in enumerate(self.problems, start=1)
        ))


class LabKindError(ValueError):
    """The artifact is not of the kind the request needs (a lab, or a study page)."""


# -- the spec ---------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _non_blank(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


class LabFile(_Model):
    """One Python file of a lab: a flat name (`attention.py`) and its text."""

    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _FILE_PATH_RE.fullmatch(value):
            raise ValueError(
                f"file name {value!r} must be a flat Python file name: lowercase letters, "
                "digits and underscores, not starting with a digit, ending in .py"
            )
        return value


class LabStep(_Model):
    """One step. `instructions` and `hint` are Markdown; only implement steps have a check."""

    title: str
    instructions: str
    kind: StepKind
    check: str | None
    hint: str | None

    @model_validator(mode="after")
    def _rules(self) -> LabStep:
        _non_blank(self.title, "a step's title")
        _non_blank(self.instructions, f"the instructions of step {self.title!r}")
        if self.kind == "implement":
            if self.check is None or not self.check.strip():
                raise ValueError(f"implement step {self.title!r} must have a check")
        elif self.check is not None:
            raise ValueError(
                f"{self.kind} step {self.title!r} must not have a check (only implement steps "
                "have one)"
            )
        if self.hint is not None and not self.hint.strip():
            raise ValueError(f"the hint of step {self.title!r} is empty; use null for no hint")
        return self


def _unique_paths(files: Sequence[LabFile], what: str) -> list[str]:
    paths = [f.path for f in files]
    repeated = sorted({p for p in paths if paths.count(p) > 1})
    if repeated:
        raise ValueError(f"{what} name {', '.join(repeated)} more than once")
    return paths


class LabSpec(_Model):
    """A whole lab, as stored in `lab.json`."""

    title: str
    brief: str
    packages: list[str]
    entry: str
    files: list[LabFile] = Field(min_length=MIN_FILES, max_length=MAX_FILES)
    steps: list[LabStep] = Field(min_length=MIN_STEPS, max_length=MAX_STEPS)
    solution: list[LabFile]
    study_page: str

    @model_validator(mode="after")
    def _rules(self) -> LabSpec:
        _non_blank(self.title, "the title")
        _non_blank(self.brief, "the brief")
        _non_blank(self.study_page, "study_page")
        unknown = [p for p in self.packages if p not in ALLOWED_PACKAGES]
        if unknown:
            raise ValueError(
                f"package(s) {', '.join(map(repr, unknown))} are not available; a lab may use "
                f"only {', '.join(ALLOWED_PACKAGES)}"
            )
        if len(set(self.packages)) != len(self.packages):
            raise ValueError("packages lists a package more than once")
        paths = _unique_paths(self.files, "files")
        if self.entry not in paths:
            raise ValueError(f"the entry file {self.entry!r} is not one of the files ({', '.join(paths)})")
        solution_paths = _unique_paths(self.solution, "solution")
        if set(solution_paths) != set(paths):
            raise ValueError(
                "the solution must have exactly the same files as the starter: starter "
                f"{', '.join(sorted(paths))}; solution {', '.join(sorted(solution_paths)) or 'none'}"
            )
        if not any(step.kind == "implement" for step in self.steps):
            raise ValueError("a lab needs at least one implement step (with a check)")
        return self

    def checked_steps(self) -> list[tuple[int, LabStep]]:
        """`(index, step)` for every step with a check, in order (indexes count from 0)."""
        return [(index, step) for index, step in enumerate(self.steps) if step.check is not None]

    def checks(self) -> list[str]:
        """The checks, in step order (what a runner gets)."""
        return [step.check for _index, step in self.checked_steps() if step.check is not None]


def file_map(files: Sequence[LabFile]) -> dict[str, str]:
    """`{path: content}`, the shape a runner takes."""
    return {f.path: f.content for f in files}


def validation_problems(exc: ValidationError) -> list[str]:
    """One sentence per pydantic error, with its location (`steps.2.check: ...`)."""
    problems = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "the lab"
        message = error["msg"].removeprefix("Value error, ")
        problems.append(f"{location}: {message}")
    return problems


# -- lab.json ---------------------------------------------------------------------------------


def lab_path(artifact_dir: Path) -> Path:
    return artifact_dir / LAB_FILE


def write_lab(artifact_dir: Path, spec: LabSpec) -> Path:
    """Write `spec` as `<artifact_dir>/lab.json` (atomically: a temp file, then a rename)."""
    path = lab_path(artifact_dir)
    tmp = path.with_name(f".{LAB_FILE}.tmp")
    tmp.write_text(
        json.dumps(spec.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def read_lab(artifact_dir: Path, *, slug: str) -> LabSpec:
    """The `LabSpec` in `<artifact_dir>/lab.json`. IntegrityCheckError (HTTP 422) if the file
    is missing, unreadable or not a valid spec: a ready lab always has one."""
    from teacher.managers.errors import IntegrityCheckError  # (the managers import this module)

    path = lab_path(artifact_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise IntegrityCheckError(f"lab {slug!r} has no {LAB_FILE}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise IntegrityCheckError(
            f"the {LAB_FILE} of lab {slug!r} could not be read ({exc.__class__.__name__})"
        ) from exc
    try:
        return LabSpec.model_validate_json(raw)
    except ValidationError as exc:
        raise IntegrityCheckError(
            f"the {LAB_FILE} of lab {slug!r} is not a valid lab: "
            + "; ".join(validation_problems(exc))
        ) from exc


# -- kinds ------------------------------------------------------------------------------------


def require_lab(artifact: Any) -> None:
    """LabKindError (422) unless `artifact` is a lab."""
    if artifact.kind != LAB_KIND:
        raise LabKindError(f"artifact {artifact.slug!r} is a {artifact.kind}, not a lab")


def require_not_lab(artifact: Any, what: str) -> None:
    """LabKindError (422) if `artifact` is a lab; `what` names what a lab does not have, e.g.
    "blocks"."""
    if artifact.kind == LAB_KIND:
        raise LabKindError(
            f"artifact {artifact.slug!r} is a lab, which has no {what}; use /api/labs/{artifact.slug}"
        )


# -- the runner and verification --------------------------------------------------------------


class LabRunner(Protocol):
    """What `verify_lab` runs a lab with (`teacher.pyodide_runtime.PythonRunner`)."""

    def check_available(self) -> None:
        """Raise if `execute` cannot work (no runtime installed, no Node)."""
        ...

    def execute(
        self,
        files: Mapping[str, str],
        *,
        entry: str,
        checks: Sequence[str] = (),
        packages: Sequence[str] = (),
    ) -> LabRun: ...


def quoted(text: str | None) -> str:
    """`text` stripped and cut to MAX_QUOTED_CHARS (saying so), for a problem message."""
    text = (text or "").strip()
    if len(text) <= MAX_QUOTED_CHARS:
        return text
    return text[:MAX_QUOTED_CHARS] + f" [... cut: {len(text) - MAX_QUOTED_CHARS} more characters]"


def _run_failure(result: LabRun, what: str) -> str:
    run = result.run
    if run.timed_out:
        return f"{what} did not finish in time: {quoted(run.error) or 'timed out'}"
    return f"{what} failed with an error: {quoted(run.error) or 'no error message'}"


def _step_name(index: int, step: LabStep) -> str:
    return f"step {index + 1} ({step.title!r})"


@dataclass(frozen=True)
class LabVerification:
    """A lab that passed `verify_lab`, with what its two runs produced."""

    starter: LabRun
    solution: LabRun

    def record(self, spec: LabSpec) -> dict[str, Any]:
        """A JSON-able summary of both runs (for the verify stage's output file)."""
        return {
            "starter": run_record(self.starter, spec),
            "solution": run_record(self.solution, spec),
        }


def run_record(result: LabRun, spec: LabSpec) -> dict[str, Any]:
    """One runner result as JSON: the run, and each check by step number."""
    run = result.run
    return {
        "ok": run.ok,
        "timed_out": run.timed_out,
        "error": run.error,
        "stdout": quoted(run.stdout),
        "stderr": quoted(run.stderr),
        "figures": run.figures,
        "checks": [
            {"step": index + 1, "passed": check.passed, "output": quoted(check.output),
             "error": check.error}
            for (index, _step), check in zip(spec.checked_steps(), result.checks, strict=False)
        ],
    }


def _execute(runner: LabRunner, spec: LabSpec, files: Sequence[LabFile]) -> LabRun:
    checks = spec.checks()
    result = runner.execute(file_map(files), entry=spec.entry, checks=checks, packages=spec.packages)
    if len(result.checks) != len(checks):
        raise RuntimeError(
            f"the runner returned {len(result.checks)} check result(s) for {len(checks)} check(s)"
        )
    return result


def verify_lab(spec: LabSpec, runner: LabRunner) -> LabVerification:
    """Run the starter and the solution with every check; LabVerificationError listing every
    problem unless the starter runs, the solution runs and passes every check, and the starter
    fails every check. Runner errors (e.g. no runtime installed) propagate unchanged."""
    starter = _execute(runner, spec, spec.files)
    solution = _execute(runner, spec, spec.solution)
    problems: list[str] = []
    if not starter.run.ok:
        problems.append(_run_failure(starter, f"the starter program ({spec.entry})"))
    if not solution.run.ok:
        problems.append(_run_failure(solution, f"the solution program ({spec.entry})"))
    else:
        for (index, step), check in zip(spec.checked_steps(), solution.checks, strict=True):
            if not check.passed:
                problems.append(
                    f"the check of {_step_name(index, step)} fails on the solution: "
                    f"{quoted(check.error) or 'no error message'}"
                )
    if starter.run.ok:
        for (index, step), check in zip(spec.checked_steps(), starter.checks, strict=True):
            if check.passed:
                problems.append(
                    f"the check of {_step_name(index, step)} already passes on the starter "
                    "files, so it tests nothing: it must fail until the reader has done the step"
                )
    if problems:
        raise LabVerificationError(problems)
    return LabVerification(starter=starter, solution=solution)


__all__ = [
    "ALLOWED_PACKAGES",
    "FILE_PATH_PATTERN",
    "LAB_FILE",
    "LAB_KIND",
    "MAX_FILES",
    "MAX_STEPS",
    "MIN_FILES",
    "MIN_STEPS",
    "STEP_KINDS",
    "STUDY_PAGE_KIND",
    "STUDY_PAGE_PARAM",
    "LabFile",
    "LabKindError",
    "LabRunner",
    "LabSpec",
    "LabStep",
    "LabVerification",
    "LabVerificationError",
    "file_map",
    "lab_path",
    "read_lab",
    "require_lab",
    "require_not_lab",
    "run_record",
    "validation_problems",
    "verify_lab",
    "write_lab",
]
