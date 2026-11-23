"""Lab endpoints: make a lab from a study page, read it, save the reader's work, record runs and
checks, reveal the solution, export it as a zip.

- `POST /api/artifacts/{slug}/labs` body `{"model": str | null}` (optional) -> 202, the new lab
  as `ArtifactOut`, generated in the background by `app.state.lab_generator`. 422 if `{slug}` is
  not a study page, 409 if it is not `ready`.
- `GET /api/artifacts/{slug}/labs` -> `ArtifactOut[]`: the labs made from that page
  (`params.study_page == slug`) that no other artifact supersedes, newest first. 422 for a lab.
- `GET /api/labs/{slug}` -> `LabOut` (the spec, the reader's files and each step's status).
- `PUT /api/labs/{slug}/workspace` body `{"files"}` -> `{"files", "updated_at"}`.
- `POST /api/labs/{slug}/runs` body `{"kind", "step", "files", "passed", "output", "error"}`
  -> 201 `{"steps"}`; also saves `files` as the workspace.
- `GET /api/labs/{slug}/solution` -> `{"files"}`, and records that it was revealed.
- `GET /api/labs/{slug}/export` -> a zip of the reader's files, `README.md` (brief and steps),
  `tests/test_step_<n>.py` per check (n is the step number, counted from 1) and
  `requirements.txt`.

Steps are addressed by `index`, counted from 0 (the position in `steps`). Every `/api/labs/...`
endpoint is 404 for an unknown slug, 422 for an artifact that is not a lab and 409 unless the
lab is `ready`; the limits (`teacher.managers.labs`) are 422s. The browser runs the code; these
endpoints only store what it reports.
"""

from __future__ import annotations

import io
import zipfile
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from teacher.config import Settings
from teacher.db.models import Artifact
from teacher.files import slugify
from teacher.labs import (
    LAB_KIND,
    STUDY_PAGE_PARAM,
    LabFile,
    LabKindError,
    LabSpec,
    LabStep,
    require_lab,
)
from teacher.managers import (
    ArtifactManager,
    LabManager,
    NotFoundError,
    StepStatus,
    StudyHistoryManager,
)
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.artifacts import (
    ArtifactOut,
    _start,
    artifact_out,
    lab_study_page,
    unique_slug,
)

router = APIRouter(prefix="/api", tags=["labs"])

EXPORT_TESTS_DIR = "tests"


# -- models ---------------------------------------------------------------------------------


class LabCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None  # the model to write the lab with; the configured default if null


class StudyPageRefOut(BaseModel):
    slug: str
    title: str


class LabStepOut(BaseModel):
    index: int  # from 0
    title: str
    instructions: str
    kind: str
    check: str | None
    hint: str | None
    passed: bool
    attempts: int


class LabOut(BaseModel):
    slug: str
    title: str
    status: str
    study_page: StudyPageRefOut | None  # null once the study page has been deleted
    brief: str
    packages: list[str]
    entry: str
    starter_files: list[LabFile]
    files: list[LabFile]  # the saved workspace, or the starter files
    steps: list[LabStepOut]
    solution_revealed: bool


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[LabFile]


class WorkspaceOut(BaseModel):
    files: list[LabFile]
    updated_at: str


class LabRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["run", "check"]
    step: int | None = None  # required for a check
    files: list[LabFile]
    passed: bool
    output: str
    error: str | None = None


class StepsOut(BaseModel):
    steps: list[LabStepOut]


class SolutionOut(BaseModel):
    files: list[LabFile]


# -- helpers --------------------------------------------------------------------------------


def _ready_lab(session: Session, settings: Settings, slug: str) -> tuple[Artifact, LabSpec]:
    """The lab `slug` and its spec: 404 if missing, 422 if not a lab, 409 unless ready."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_lab(artifact)
    if artifact.status != "ready":
        raise HTTPException(409, f"lab {artifact.slug!r} is {artifact.status}, not ready")
    return artifact, LabManager(session, settings).spec(artifact)


def steps_out(spec: LabSpec, statuses: list[StepStatus]) -> list[LabStepOut]:
    return [
        LabStepOut(
            index=status.index,
            title=step.title,
            instructions=step.instructions,
            kind=step.kind,
            check=step.check,
            hint=step.hint,
            passed=status.passed,
            attempts=status.attempts,
        )
        for step, status in zip(spec.steps, statuses, strict=True)
    ]


def _study_page_ref(session: Session, settings: Settings, spec: LabSpec) -> StudyPageRefOut | None:
    try:
        page = ArtifactManager(session, settings).get_by_slug(spec.study_page)
    except NotFoundError:
        return None
    return StudyPageRefOut(slug=page.slug, title=page.title)


def lab_out(session: Session, settings: Settings, artifact: Artifact, spec: LabSpec) -> LabOut:
    manager = LabManager(session, settings)
    return LabOut(
        slug=artifact.slug,
        title=artifact.title,
        status=artifact.status,
        study_page=_study_page_ref(session, settings, spec),
        brief=spec.brief,
        packages=list(spec.packages),
        entry=spec.entry,
        starter_files=list(spec.files),
        files=manager.files(artifact, spec),
        steps=steps_out(spec, manager.step_statuses(artifact, spec)),
        solution_revealed=manager.solution_revealed(artifact),
    )


# -- the export -----------------------------------------------------------------------------


def _one_line(text: str) -> str:
    return " ".join(text.split())


def export_readme(spec: LabSpec) -> str:
    """`README.md`: the brief, how to run the lab and its checks, and every step."""
    lines = [
        f"# {_one_line(spec.title)}",
        "",
        spec.brief.strip(),
        "",
        "## Running it",
        "",
        "Install the requirements with `pip install -r requirements.txt`, run the program with "
        f"`python {spec.entry}`, and check the steps that have a check with "
        f"`python -m pytest {EXPORT_TESTS_DIR}`.",
        "",
        "## Steps",
    ]
    for number, step in enumerate(spec.steps, start=1):
        lines += ["", f"### Step {number}: {_one_line(step.title)} ({step.kind})", "",
                  step.instructions.strip()]
        if step.hint is not None:
            lines += ["", f"Hint: {step.hint.strip()}"]
        if step.check is not None:
            lines += ["", f"Checked by `{EXPORT_TESTS_DIR}/test_step_{number}.py`."]
    return "\n".join(lines) + "\n"


def export_test(number: int, step: LabStep, packages: list[str]) -> str:
    """`tests/test_step_<number>.py`: the step's check as a pytest test, run in a fresh
    namespace with the lab's folder importable (as in the app; with matplotlib's Agg backend
    when the lab uses matplotlib)."""
    assert step.check is not None
    uses_matplotlib = "matplotlib" in packages
    return "\n".join(
        [
            f"# The check of step {number} of the lab: {_one_line(step.title)}",
            f"# Run it from the lab's folder with `python -m pytest {EXPORT_TESTS_DIR}`.",
            "",
            *(["import os"] if uses_matplotlib else []),
            "import sys",
            "from pathlib import Path",
            "",
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))",
            *(['os.environ.setdefault("MPLBACKEND", "Agg")'] if uses_matplotlib else []),
            "",
            f"CHECK = {step.check!r}",
            "",
            "",
            f"def test_step_{number}() -> None:",
            f'    exec(compile(CHECK, "check of step {number}", "exec"), {{"__name__": "__check__"}})',
            "",
        ]
    )


def export_zip(spec: LabSpec, files: list[LabFile]) -> bytes:
    """The lab as a zip: `files` (the reader's), README.md, the tests, requirements.txt."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item.path, item.content)
        archive.writestr("README.md", export_readme(spec))
        for index, step in spec.checked_steps():
            archive.writestr(
                f"{EXPORT_TESTS_DIR}/test_step_{index + 1}.py", export_test(index + 1, step, spec.packages)
            )
        archive.writestr("requirements.txt", "".join(f"{p}\n" for p in [*spec.packages, "pytest"]))
    return buffer.getvalue()


# -- endpoints: labs of a study page --------------------------------------------------------


@router.post("/artifacts/{slug}/labs", status_code=202)
def create_lab(
    slug: str,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    body: Annotated[LabCreateIn | None, Body()] = None,
) -> ArtifactOut:
    """Make a lab from the ready study page `slug` (generated in the background)."""
    page = lab_study_page(session, settings, slug)
    sources = ArtifactManager(session, settings).sources(page)
    if not sources:
        raise ValueError(f"the study page {slug!r} has no linked source")
    title = f"Lab: {sources[0][0].title}"
    model = body.model if body is not None else None
    out = _start(
        request,
        background,
        session,
        settings,
        slug=unique_slug(session, settings, slugify(title)),
        kind=LAB_KIND,
        title=title,
        params={STUDY_PAGE_PARAM: page.slug},
        sources=sources,
        model=model,
    )
    StudyHistoryManager(session, settings).log(
        "lab_requested",
        lab=ArtifactManager(session, settings).get_by_slug(out.slug),
        payload={"model": model},
    )
    return out


@router.get("/artifacts/{slug}/labs")
def list_labs(
    slug: str, request: Request, session: SessionDep, settings: SettingsDep
) -> list[ArtifactOut]:
    """The current (not superseded) labs made from the study page `slug`, newest first."""
    page = ArtifactManager(session, settings).get_by_slug(slug)
    if page.kind == LAB_KIND:
        raise LabKindError(f"artifact {slug!r} is a lab; labs are made from study pages")
    successor = aliased(Artifact)
    labs = session.scalars(
        select(Artifact)
        .where(Artifact.kind == LAB_KIND)
        .where(Artifact.params[STUDY_PAGE_PARAM].as_string() == page.slug)
        .where(~exists().where(successor.supersedes_id == Artifact.id))
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    )
    max_attempts = request.app.state.max_attempts
    return [artifact_out(session, settings, lab, max_attempts) for lab in labs]


# -- endpoints: one lab ---------------------------------------------------------------------


@router.get("/labs/{slug}")
def get_lab(slug: str, session: SessionDep, settings: SettingsDep) -> LabOut:
    artifact, spec = _ready_lab(session, settings, slug)
    return lab_out(session, settings, artifact, spec)


@router.put("/labs/{slug}/workspace")
def save_workspace(
    slug: str, body: WorkspaceIn, session: SessionDep, settings: SettingsDep
) -> WorkspaceOut:
    """Replace the reader's files (at most 8 files and 200 000 characters; the entry file
    must be present)."""
    artifact, spec = _ready_lab(session, settings, slug)
    workspace = LabManager(session, settings).save_workspace(artifact, spec, body.files)
    StudyHistoryManager(session, settings).log(
        "lab_workspace_saved",
        lab=artifact,
        payload={"files": len(body.files), "chars": sum(len(f.content) for f in body.files)},
    )
    return WorkspaceOut(
        files=[LabFile.model_validate(item) for item in workspace.files],
        updated_at=to_iso(workspace.updated_at) or "",
    )


@router.post("/labs/{slug}/runs", status_code=201)
def record_run(
    slug: str, body: LabRunIn, session: SessionDep, settings: SettingsDep
) -> StepsOut:
    """Record a run or a check the browser made (and save its files); the steps' status."""
    artifact, spec = _ready_lab(session, settings, slug)
    manager = LabManager(session, settings)
    manager.record_run(
        artifact,
        spec,
        kind=body.kind,
        step=body.step,
        files=body.files,
        passed=body.passed,
        output=body.output,
        error=body.error,
    )
    return StepsOut(steps=steps_out(spec, manager.step_statuses(artifact, spec)))


@router.get("/labs/{slug}/solution")
def get_solution(slug: str, session: SessionDep, settings: SettingsDep) -> SolutionOut:
    """The reference solution; records that the reader revealed it (and logs
    `lab_solution_viewed`)."""
    artifact, spec = _ready_lab(session, settings, slug)
    manager = LabManager(session, settings)
    first_time = not manager.solution_revealed(artifact)
    files = manager.reveal_solution(artifact, spec)
    StudyHistoryManager(session, settings).log(
        "lab_solution_viewed", lab=artifact, payload={"first_time": first_time}
    )
    return SolutionOut(files=files)


@router.get("/labs/{slug}/export")
def export_lab(slug: str, session: SessionDep, settings: SettingsDep) -> Response:
    """The reader's files, README.md, one pytest file per check and requirements.txt."""
    artifact, spec = _ready_lab(session, settings, slug)
    files = LabManager(session, settings).files(artifact, spec)
    StudyHistoryManager(session, settings).log("lab_exported", lab=artifact)
    return Response(
        content=export_zip(spec, files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{artifact.slug}.zip"'},
    )


__all__ = [
    "LabCreateIn",
    "LabOut",
    "LabRunIn",
    "LabStepOut",
    "SolutionOut",
    "StepsOut",
    "StudyPageRefOut",
    "WorkspaceIn",
    "WorkspaceOut",
    "export_readme",
    "export_test",
    "export_zip",
    "lab_out",
    "router",
    "steps_out",
]
