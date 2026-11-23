"""Glossary endpoints: the study page's terms for hover definitions, and building them on demand.

- `GET /api/artifacts/{slug}/glossary` -> `{"terms": [...], "generated": bool}`. Only terms that
  occur in what the reader can currently see are listed (the public content of visible blocks
  plus the revealed private part of cleared gates), each with only the aliases that occur
  there, so the list never previews locked parts of the page. `generated` is false when no
  glossary was ever built for the artifact (`artifacts.glossary_built_at` is NULL), true when
  one was built, even with no terms.
- `POST /api/artifacts/{slug}/glossary` builds (or rebuilds, replacing) the glossary of a
  `ready` artifact synchronously with `app.state.glossary_builder` and returns the GET view
  plus `dropped_count` (proposed terms removed because they do not occur in the page). The
  build is recorded as a `glossary` generation run with its tokens and output file. No
  database session is open during the model call.

A lab (`kind = "lab"`) has no glossary: both endpoints are a 422 for it. 409 unless the artifact
is ready; the builder's errors map to 503 (model unavailable), 502
(unusable reply) and 422 (no usable source or blocks) in `teacher.web.errors`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from teacher.claude_generator import (
    GLOSSARY_ERROR_PREFIX,
    GLOSSARY_FILE,
    GLOSSARY_STAGE,
    OUTPUT_DIR,
    GlossaryBuilder,
    token_counts,
    write_record,
)
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import Artifact, GenerationRun
from teacher.generation import source_infos
from teacher.glossary import GlossaryInputError, block_texts, visible_entries
from teacher.labs import require_not_lab
from teacher.managers import (
    ArtifactBlockManager,
    ArtifactManager,
    GenerationRunManager,
    GlossaryManager,
    StudyHistoryManager,
)
from teacher.reading import ReadingConflict, reading_state, require_ready, split_content
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.reading import MaxAttemptsDep

router = APIRouter(prefix="/api", tags=["glossary"])


class GlossaryTermOut(BaseModel):
    term: str
    kind: str
    expansion: str | None
    definition: str
    aliases: list[str]


class GlossaryOut(BaseModel):
    terms: list[GlossaryTermOut]
    generated: bool


class GlossaryBuildOut(GlossaryOut):
    dropped_count: int


def get_glossary_builder(request: Request) -> GlossaryBuilder:
    return request.app.state.glossary_builder


GlossaryBuilderDep = Annotated[GlossaryBuilder, Depends(get_glossary_builder)]


def _require_ready(artifact: Artifact) -> None:
    try:
        require_ready(artifact)
    except ReadingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def visible_texts(
    session: Session, settings: Settings, artifact: Artifact, max_attempts: int
) -> list[str]:
    """The text the reader can see now: visible blocks, with private parts only once cleared."""
    blocks = ArtifactBlockManager(session, settings)
    texts: list[str] = []
    for block_state in reading_state(session, settings, artifact, max_attempts=max_attempts).visible:
        public, revealed = split_content(block_state, blocks.read_content(block_state.block))
        texts += block_texts(public) + block_texts(revealed)
    return texts


def glossary_out(
    session: Session, settings: Settings, artifact: Artifact, max_attempts: int
) -> GlossaryOut:
    manager = GlossaryManager(session, settings)
    entries = visible_entries(
        manager.entries(artifact), visible_texts(session, settings, artifact, max_attempts)
    )
    return GlossaryOut(
        terms=[GlossaryTermOut(**entry.to_dict()) for entry in entries],
        generated=manager.is_built(artifact),
    )


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


def _fail_run(
    settings: Settings, run_id: int, exc: BaseException, tokens: tuple[Any, Any]
) -> None:
    """Record the failure on the build's run (gone with the artifact if it was deleted)."""
    with session_scope(settings) as session:
        run = session.get(GenerationRun, run_id)
        if run is not None and run.status == "running":
            GenerationRunManager(session, settings).fail(
                run, _error_text(exc), input_tokens=tokens[0], output_tokens=tokens[1]
            )


@router.get("/artifacts/{slug}/glossary")
def get_glossary(
    slug: str, session: SessionDep, settings: SettingsDep, max_attempts: MaxAttemptsDep
) -> GlossaryOut:
    """The glossary terms that occur in the blocks the reader can currently see."""
    artifact = ArtifactManager(session, settings).get_by_slug(slug)
    require_not_lab(artifact, "glossary")
    _require_ready(artifact)
    return glossary_out(session, settings, artifact, max_attempts)


@router.post("/artifacts/{slug}/glossary")
def build_glossary(
    slug: str, settings: SettingsDep, max_attempts: MaxAttemptsDep, builder: GlossaryBuilderDep
) -> GlossaryBuildOut:
    """Build or rebuild the glossary of a ready artifact now (one Claude call)."""
    with session_scope(settings) as session:
        artifacts = ArtifactManager(session, settings)
        artifact = artifacts.get_by_slug(slug)
        require_not_lab(artifact, "glossary")
        _require_ready(artifact)
        sources = source_infos(session, settings, artifact)
        if not sources:
            raise GlossaryInputError(
                f"{GLOSSARY_ERROR_PREFIX}artifact {artifact.slug!r} has no linked source"
            )
        source = sources[0]  # the source the page teaches, as in generation
        manager = ArtifactBlockManager(session, settings)
        blocks = [(block.type, manager.read_content(block)) for block in manager.list(artifact)]
        path: Path = artifacts.dir(artifact) / OUTPUT_DIR / GLOSSARY_FILE
    document = builder.document(source)  # 422 for an unusable source file
    builder.check_available()  # 503 for missing credentials, before anything is recorded

    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        run_id = GenerationRunManager(session, settings).start(
            artifact, stage=GLOSSARY_STAGE, model=builder.model
        ).id
        StudyHistoryManager(session, settings).log("glossary_build_requested", artifact=artifact)

    message: Any = None
    try:
        message = builder.call(document, blocks)  # no session is open here
        result = builder.interpret(message, blocks)
        write_record(path, builder.record(message, result, source=source, trigger="on_demand"))
        tokens = token_counts(message)
        with session_scope(settings) as session:
            artifact = ArtifactManager(session, settings).get_by_slug(slug)
            GlossaryManager(session, settings).replace(artifact, result.entries)
            run = session.get(GenerationRun, run_id)
            GenerationRunManager(session, settings).succeed(
                run, input_tokens=tokens[0], output_tokens=tokens[1], output_file=path
            )
            out = glossary_out(session, settings, artifact, max_attempts)
    except BaseException as exc:
        _fail_run(
            settings, run_id, exc, token_counts(message) if message is not None else (None, None)
        )
        raise
    return GlossaryBuildOut(**out.model_dump(), dropped_count=result.dropped_count)


__all__ = ["GlossaryBuildOut", "GlossaryOut", "GlossaryTermOut", "router", "visible_texts"]
