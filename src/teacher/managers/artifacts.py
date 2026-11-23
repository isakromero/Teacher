"""ArtifactManager: study artifacts, their folders, and their links to knowledge sources."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import exists, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from teacher.config import Settings
from teacher.db.models import Artifact, ArtifactSource, KnowledgeSource
from teacher.files import to_absolute, validate_slug
from teacher.managers.errors import DuplicateError, NotFoundError

ARTIFACT_STATUSES: tuple[str, ...] = ("generating", "ready", "failed")
DEFAULT_ROLE = "primary"

SourceSpec = KnowledgeSource | tuple[KnowledgeSource, str]


def _is_unique_violation(err: IntegrityError) -> bool:
    return "UNIQUE constraint failed" in str(err.orig)


def _require_persistent(obj: object, what: str) -> None:
    """Raise ValueError unless `obj` has been flushed to the database."""
    if not inspect(obj).has_identity:
        raise ValueError(f"{what} must be saved (flushed) before use")


def _validate_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_status(status: str) -> str:
    if status not in ARTIFACT_STATUSES:
        raise ValueError(f"invalid artifact status {status!r}: expected one of {ARTIFACT_STATUSES}")
    return status


class ArtifactManager:
    """Create, query, re-status and delete artifacts (and own `artifact_sources`)."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- creation ---------------------------------------------------------------------------

    def create(
        self,
        *,
        slug: str,
        kind: str,
        title: str,
        generator: str,
        summary: str | None = None,
        params: dict[str, Any] | None = None,
        sources: Sequence[SourceSpec] = (),
        supersedes: Artifact | None = None,
    ) -> Artifact:
        """Create `artifacts/<slug>/{blocks,assets}/` and the row (status 'generating') + links.

        Raises DuplicateError for a taken slug, an existing folder or a source listed twice.
        The folder is removed again if anything after creating it fails.
        """
        validate_slug(slug)
        _validate_text(kind, "kind")
        _validate_text(title, "title")
        _validate_text(generator, "generator")
        if params is not None and not isinstance(params, dict):
            raise TypeError("params must be a dict")
        links = self._normalise_sources(sources)
        if supersedes is not None:
            if not isinstance(supersedes, Artifact):
                raise TypeError("supersedes must be an Artifact or None")
            _require_persistent(supersedes, "supersedes")

        if self.session.scalar(select(Artifact.id).where(Artifact.slug == slug)) is not None:
            raise DuplicateError(f"artifact slug {slug!r} already exists")
        folder = self.settings.artifacts_dir / slug
        if folder.exists():
            raise DuplicateError(f"artifact folder {folder} already exists (no matching row)")

        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        try:
            (folder / "blocks").mkdir()
            (folder / "assets").mkdir()
            artifact = Artifact(
                slug=slug,
                kind=kind,
                title=title,
                summary=summary,
                dir_path=f"artifacts/{slug}",
                status="generating",
                generator=generator,
                params=dict(params) if params is not None else {},
                supersedes_id=supersedes.id if supersedes is not None else None,
            )
            artifact.source_links = [
                ArtifactSource(source_id=source.id, role=role) for source, role in links
            ]
            try:
                with self.session.begin_nested():
                    self.session.add(artifact)
                    self.session.flush()
            except IntegrityError as err:
                if _is_unique_violation(err):
                    raise DuplicateError(f"artifact slug {slug!r} already exists") from err
                raise
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        for source, _role in links:
            self.session.expire(source, ["artifact_links", "artifacts"])
        if supersedes is not None:
            self.session.expire(supersedes, ["superseded_by"])
        return artifact

    def _normalise_sources(self, sources: Sequence[SourceSpec]) -> list[tuple[KnowledgeSource, str]]:
        links: list[tuple[KnowledgeSource, str]] = []
        seen: set[int] = set()
        for item in sources:
            if isinstance(item, KnowledgeSource):
                source, role = item, DEFAULT_ROLE
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], KnowledgeSource):
                source, role = item
            else:
                raise TypeError(f"sources items must be KnowledgeSource or (KnowledgeSource, role), got {item!r}")
            _require_persistent(source, "source")
            _validate_text(role, "role")
            if source.id in seen:
                raise DuplicateError(f"source {source.slug!r} listed more than once")
            seen.add(source.id)
            links.append((source, role))
        return links

    # -- lookups ----------------------------------------------------------------------------

    def get(self, id: int) -> Artifact:
        """Return the artifact with `id`; NotFoundError if there is none."""
        artifact = self.session.get(Artifact, id)
        if artifact is None:
            raise NotFoundError(f"no artifact with id {id}")
        return artifact

    def get_by_slug(self, slug: str) -> Artifact:
        """Return the artifact with `slug`; NotFoundError if there is none."""
        artifact = self.session.scalar(select(Artifact).where(Artifact.slug == slug))
        if artifact is None:
            raise NotFoundError(f"no artifact with slug {slug!r}")
        return artifact

    def list(
        self,
        kind: str | None = None,
        status: str | None = None,
        source: KnowledgeSource | None = None,
    ) -> list[Artifact]:
        """Artifacts in creation order, optionally filtered by kind, status and linked source."""
        stmt = select(Artifact)
        if kind is not None:
            stmt = stmt.where(Artifact.kind == kind)
        if status is not None:
            stmt = stmt.where(Artifact.status == _validate_status(status))
        if source is not None:
            stmt = stmt.join(ArtifactSource, ArtifactSource.artifact_id == Artifact.id).where(
                ArtifactSource.source_id == source.id
            )
        return list(self.session.scalars(stmt.order_by(Artifact.created_at, Artifact.id)))

    def sources(self, artifact: Artifact) -> list[tuple[KnowledgeSource, str]]:
        """The artifact's linked sources as `(source, role)` pairs, ordered by source id."""
        rows = self.session.execute(
            select(KnowledgeSource, ArtifactSource.role)
            .join(ArtifactSource, ArtifactSource.source_id == KnowledgeSource.id)
            .where(ArtifactSource.artifact_id == artifact.id)
            .order_by(KnowledgeSource.id)
        )
        return [(source, role) for source, role in rows]

    def latest_for_source(self, source: KnowledgeSource, kind: str | None = None) -> Artifact | None:
        """Newest artifact linked to `source` that no other artifact supersedes, or None.

        Labs are linked to their study page's sources, so the source's study page is
        `latest_for_source(source, kind="study_page")`; without `kind` a lab may be returned."""
        successor = aliased(Artifact)
        stmt = (
            select(Artifact)
            .join(ArtifactSource, ArtifactSource.artifact_id == Artifact.id)
            .where(ArtifactSource.source_id == source.id)
            .where(~exists().where(successor.supersedes_id == Artifact.id))
        )
        if kind is not None:
            stmt = stmt.where(Artifact.kind == kind)
        stmt = stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(1)
        return self.session.scalar(stmt)

    def dir(self, artifact: Artifact) -> Path:
        """Absolute path of the artifact's folder."""
        return to_absolute(artifact.dir_path, self.settings.root)

    # -- changes ----------------------------------------------------------------------------

    def set_status(self, artifact: Artifact, status: str) -> Artifact:
        """Set status to 'generating', 'ready' or 'failed' (ValueError otherwise)."""
        artifact.status = _validate_status(status)
        self.session.flush()
        return artifact

    def mark_ready(self, artifact: Artifact) -> Artifact:
        return self.set_status(artifact, "ready")

    def mark_failed(self, artifact: Artifact) -> Artifact:
        return self.set_status(artifact, "failed")

    def add_source(
        self, artifact: Artifact, source: KnowledgeSource, role: str = DEFAULT_ROLE
    ) -> ArtifactSource:
        """Link `source` to `artifact`; DuplicateError if it is already linked."""
        _require_persistent(artifact, "artifact")
        _require_persistent(source, "source")
        _validate_text(role, "role")
        existing = self.session.get(ArtifactSource, (artifact.id, source.id))
        if existing is not None:
            raise DuplicateError(f"source {source.slug!r} is already linked to {artifact.slug!r}")
        link = ArtifactSource(artifact_id=artifact.id, source_id=source.id, role=role)
        try:
            with self.session.begin_nested():
                self.session.add(link)
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"source {source.slug!r} is already linked to {artifact.slug!r}"
                ) from err
            raise
        self.session.expire(artifact, ["source_links", "sources"])
        self.session.expire(source, ["artifact_links", "artifacts"])
        return link

    def delete(self, artifact: Artifact, *, remove_files: bool = True) -> None:
        """Delete the row (blocks, responses, runs and links cascade) and, by default, its folder.

        Artifacts that superseded this one keep existing with `supersedes_id` set to NULL.
        Files are removed only after the row delete has been flushed successfully.
        """
        folder = self.dir(artifact)
        _ = artifact.superseded_by  # load so the ORM nulls their FK in memory too
        self.session.delete(artifact)
        self.session.flush()
        if remove_files and folder.exists():
            shutil.rmtree(folder)


__all__ = ["ARTIFACT_STATUSES", "ArtifactManager", "SourceSpec"]
