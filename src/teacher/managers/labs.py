"""LabManager: a lab's spec, the reader's workspace, the runs and checks they made, the solution.

The browser runs the reader's code (Pyodide in a sandbox) and reports each run here; nothing
runs on the server. A step's status comes from those reports alone:

- `passed`: some `check` run of that step passed;
- `attempts`: the number of `check` runs of that step.

Steps without a check (explore and extend steps) are never passed and have no attempts.

The workspace is the reader's current set of files (one row per lab, saved on every edit and
with every run); until the first save the reader works on the starter files. Its limits are
`MAX_WORKSPACE_FILES` files and `MAX_WORKSPACE_CHARS` characters of content in total, and it
must contain the lab's entry file. A run's `output` may hold at most `MAX_OUTPUT_CHARS`
characters (the client truncates longer output and says so in the text).

`reveal_solution` returns the reference solution and records when the reader first asked for
it (`lab_workspaces.solution_revealed_at`, kept on later requests).

Every method takes a lab (`LabKindError`, a ValueError, for any other kind) and raises
ValueError for input that breaks the rules; like every manager it flushes and never commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, LabRunRecord, LabWorkspace, utcnow
from teacher.labs import LabFile, LabSpec, read_lab, require_lab
from teacher.managers.artifacts import ArtifactManager, _require_persistent
from teacher.managers.study_history import StudyHistoryManager

MAX_WORKSPACE_FILES = 8
MAX_WORKSPACE_CHARS = 200_000
MAX_OUTPUT_CHARS = 20_000
RUN_KINDS: tuple[str, ...] = ("run", "check")


@dataclass(frozen=True, slots=True)
class StepStatus:
    """The reader's standing on one step (`index` counts from 0)."""

    index: int
    passed: bool
    attempts: int


def _files_json(files: Sequence[LabFile]) -> list[dict[str, str]]:
    return [{"path": f.path, "content": f.content} for f in files]


def check_workspace(spec: LabSpec, files: Sequence[LabFile]) -> list[LabFile]:
    """`files` as a list if they are a valid workspace for `spec`, else ValueError naming the
    broken limit."""
    files = list(files)
    if not all(isinstance(f, LabFile) for f in files):
        raise TypeError("workspace files must be LabFile objects")
    if not files:
        raise ValueError("the workspace has no files")
    if len(files) > MAX_WORKSPACE_FILES:
        raise ValueError(
            f"the workspace has {len(files)} files; a lab workspace holds at most "
            f"{MAX_WORKSPACE_FILES}"
        )
    paths = [f.path for f in files]
    repeated = sorted({p for p in paths if paths.count(p) > 1})
    if repeated:
        raise ValueError(f"the workspace has more than one file named {', '.join(repeated)}")
    total = sum(len(f.content) for f in files)
    if total > MAX_WORKSPACE_CHARS:
        raise ValueError(
            f"the workspace files hold {total} characters; at most {MAX_WORKSPACE_CHARS} are "
            "allowed in total"
        )
    if spec.entry not in paths:
        raise ValueError(f"the workspace must contain the lab's entry file {spec.entry!r}")
    return files


class LabManager:
    """Read a lab's spec; save the workspace; record runs; compute step status; reveal."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def spec(self, artifact: Artifact) -> LabSpec:
        """The lab's `lab.json` (IntegrityCheckError if it is missing or invalid)."""
        require_lab(artifact)
        return read_lab(ArtifactManager(self.session, self.settings).dir(artifact), slug=artifact.slug)

    # -- the workspace ----------------------------------------------------------------------

    def workspace(self, artifact: Artifact) -> LabWorkspace | None:
        """The saved workspace, or None while the reader has saved nothing."""
        require_lab(artifact)
        _require_persistent(artifact, "artifact")
        return self.session.get(LabWorkspace, artifact.id)

    def files(self, artifact: Artifact, spec: LabSpec) -> list[LabFile]:
        """The reader's files: the saved workspace, or the starter files."""
        workspace = self.workspace(artifact)
        if workspace is None:
            return list(spec.files)
        return [LabFile.model_validate(item) for item in workspace.files]

    def save_workspace(
        self, artifact: Artifact, spec: LabSpec, files: Sequence[LabFile]
    ) -> LabWorkspace:
        """Replace the workspace with `files` (see `check_workspace`) and stamp `updated_at`."""
        checked = check_workspace(spec, files)
        workspace = self.workspace(artifact)
        if workspace is None:
            workspace = LabWorkspace(artifact_id=artifact.id, files=_files_json(checked))
            self.session.add(workspace)
        else:
            workspace.files = _files_json(checked)
        workspace.updated_at = utcnow()
        self.session.flush()
        return workspace

    # -- runs -------------------------------------------------------------------------------

    def record_run(
        self,
        artifact: Artifact,
        spec: LabSpec,
        *,
        kind: str,
        step: int | None,
        files: Sequence[LabFile],
        passed: bool,
        output: str,
        error: str | None,
    ) -> LabRunRecord:
        """Record one run or check the browser made, and save its `files` as the workspace.

        `kind` is "run" or "check"; a check names its `step` (an index into the lab's steps,
        from 0), which must be a step with a check; a run may name the step the reader was on.
        ValueError for anything else, or for `output` over `MAX_OUTPUT_CHARS` characters."""
        if kind not in RUN_KINDS:
            raise ValueError(f"invalid run kind {kind!r}: expected one of {', '.join(RUN_KINDS)}")
        if step is not None:
            if isinstance(step, bool) or not isinstance(step, int):
                raise ValueError("step must be an integer step index or null")
            if not 0 <= step < len(spec.steps):
                raise ValueError(
                    f"step {step} does not exist: this lab's steps are numbered 0 to "
                    f"{len(spec.steps) - 1}"
                )
        if kind == "check":
            if step is None:
                raise ValueError("a check must name its step")
            if spec.steps[step].check is None:
                raise ValueError(
                    f"step {step} ({spec.steps[step].title!r}) is an {spec.steps[step].kind} "
                    "step and has no check"
                )
        if not isinstance(passed, bool):
            raise ValueError("passed must be true or false")
        if len(output) > MAX_OUTPUT_CHARS:
            raise ValueError(
                f"the output has {len(output)} characters; at most {MAX_OUTPUT_CHARS} may be "
                "recorded (truncate it and say so in the text)"
            )
        checked = check_workspace(spec, files)
        run = LabRunRecord(
            artifact_id=artifact.id,
            kind=kind,
            step_index=step,
            passed=passed,
            files=_files_json(checked),
            output=output,
            error=error,
            session_id=StudyHistoryManager(self.session, self.settings).activity_session().id,
        )
        self.session.add(run)
        self.save_workspace(artifact, spec, checked)
        self.session.expire(artifact, ["lab_runs"])
        return run

    def runs(self, artifact: Artifact) -> list[LabRunRecord]:
        """Every recorded run and check of the lab, oldest first."""
        require_lab(artifact)
        return list(
            self.session.scalars(
                select(LabRunRecord)
                .where(LabRunRecord.artifact_id == artifact.id)
                .order_by(LabRunRecord.id)
            )
        )

    def latest_run(self, artifact: Artifact, kind: str) -> LabRunRecord | None:
        """The most recently recorded run of `kind` ("run" or "check"), or None if there is
        none (one row; read-only)."""
        require_lab(artifact)
        if kind not in RUN_KINDS:
            raise ValueError(f"invalid run kind {kind!r}: expected one of {', '.join(RUN_KINDS)}")
        return self.session.scalar(
            select(LabRunRecord)
            .where(LabRunRecord.artifact_id == artifact.id, LabRunRecord.kind == kind)
            .order_by(LabRunRecord.id.desc())
            .limit(1)
        )

    def step_statuses(self, artifact: Artifact, spec: LabSpec) -> list[StepStatus]:
        """One `StepStatus` per step of `spec`, in order."""
        require_lab(artifact)
        rows = self.session.execute(
            select(
                LabRunRecord.step_index,
                func.count(),
                # max() over a boolean column: SQLite stores 0/1, Postgres has no max(boolean).
                func.max(case((LabRunRecord.passed, 1), else_=0)),
            )
            .where(LabRunRecord.artifact_id == artifact.id, LabRunRecord.kind == "check")
            .group_by(LabRunRecord.step_index)
        )
        checks = {index: (int(count), bool(passed)) for index, count, passed in rows}
        return [
            StepStatus(
                index=index,
                passed=checks.get(index, (0, False))[1] if step.check is not None else False,
                attempts=checks.get(index, (0, False))[0] if step.check is not None else 0,
            )
            for index, step in enumerate(spec.steps)
        ]

    # -- the solution -----------------------------------------------------------------------

    def solution_revealed(self, artifact: Artifact) -> bool:
        workspace = self.workspace(artifact)
        return workspace is not None and workspace.solution_revealed_at is not None

    def reveal_solution(self, artifact: Artifact, spec: LabSpec) -> list[LabFile]:
        """The reference solution; records the first reveal (a workspace with the starter files
        is created if the reader has saved none, so the reveal has a row to live on)."""
        workspace = self.workspace(artifact)
        if workspace is None:
            workspace = self.save_workspace(artifact, spec, spec.files)
        if workspace.solution_revealed_at is None:
            workspace.solution_revealed_at = utcnow()
            self.session.flush()
        return list(spec.solution)


__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_WORKSPACE_CHARS",
    "MAX_WORKSPACE_FILES",
    "RUN_KINDS",
    "LabManager",
    "StepStatus",
    "check_workspace",
]
