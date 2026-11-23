"""GenerationRunManager: bookkeeping for each pipeline stage run that produces an artifact."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, GenerationRun, utcnow
from teacher.files import to_relative
from teacher.managers.artifacts import _require_persistent, _validate_text

RUN_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")


def _validate_tokens(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int or None")
    return value


class GenerationRunManager:
    """Start, finish and summarise generation runs of an artifact."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def start(self, artifact: Artifact, *, stage: str, model: str) -> GenerationRun:
        """Insert a run with status 'running' and `started_at` now."""
        _require_persistent(artifact, "artifact")
        _validate_text(stage, "stage")
        _validate_text(model, "model")
        run = GenerationRun(
            artifact_id=artifact.id, stage=stage, model=model, status="running", started_at=utcnow()
        )
        self.session.add(run)
        self.session.flush()
        self.session.expire(artifact, ["runs"])
        return run

    def succeed(
        self,
        run: GenerationRun,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        output_file: str | Path | None = None,
        note: str | None = None,
    ) -> GenerationRun:
        """Mark a running run 'succeeded'. `output_file` is stored relative to the root; `note`
        (non-blank text, or None) is something the owner should see with the stage."""
        rel_output = None
        if output_file is not None:
            rel_output = to_relative(Path(output_file), self.settings.root)
        if note is not None:
            _validate_text(note, "note")
        self._finish(run, "succeeded", input_tokens, output_tokens)
        run.output_file = rel_output
        run.note = note
        self.session.flush()
        return run

    def fail(
        self,
        run: GenerationRun,
        error: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> GenerationRun:
        """Mark a running run 'failed' with an error message."""
        _validate_text(error, "error")
        self._finish(run, "failed", input_tokens, output_tokens)
        run.error = error
        self.session.flush()
        return run

    def _finish(
        self, run: GenerationRun, status: str, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status {status!r}: expected one of {RUN_STATUSES}")
        if run.status != "running":
            raise ValueError(f"run {run.id} is already {run.status!r}, not 'running'")
        input_tokens = _validate_tokens(input_tokens, "input_tokens")
        output_tokens = _validate_tokens(output_tokens, "output_tokens")
        run.status = status
        run.input_tokens = input_tokens
        run.output_tokens = output_tokens
        run.finished_at = utcnow()

    def list(self, artifact: Artifact) -> list[GenerationRun]:
        """The artifact's runs, oldest first."""
        return list(
            self.session.scalars(
                select(GenerationRun)
                .where(GenerationRun.artifact_id == artifact.id)
                .order_by(GenerationRun.started_at, GenerationRun.id)
            )
        )

    def latest(self, artifact: Artifact, stage: str | None = None) -> GenerationRun | None:
        """The most recently started run (optionally of `stage`), or None."""
        stmt = select(GenerationRun).where(GenerationRun.artifact_id == artifact.id)
        if stage is not None:
            stmt = stmt.where(GenerationRun.stage == stage)
        return self.session.scalar(
            stmt.order_by(GenerationRun.started_at.desc(), GenerationRun.id.desc()).limit(1)
        )

    def token_totals(self, artifact: Artifact) -> tuple[int, int]:
        """(input, output) token sums over all runs of `artifact`, NULL counted as 0."""
        row = self.session.execute(
            select(
                func.coalesce(func.sum(func.coalesce(GenerationRun.input_tokens, 0)), 0),
                func.coalesce(func.sum(func.coalesce(GenerationRun.output_tokens, 0)), 0),
            ).where(GenerationRun.artifact_id == artifact.id)
        ).one()
        return int(row[0]), int(row[1])


__all__ = ["RUN_STATUSES", "GenerationRunManager"]
