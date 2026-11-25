"""KnowledgeSourceManager: knowledge source rows, their identifiers, and their folders on disk."""

from __future__ import annotations

import builtins
import json
import mimetypes
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import ArtifactSource, KnowledgeSource, KnowledgeSourceIdentifier
from teacher.files import (
    sha256_file,
    sync_dir,
    sync_file,
    to_absolute,
    to_relative,
    validate_slug,
)
from teacher.managers.errors import (
    DuplicateError,
    IntegrityCheckError,
    Issue,
    NotFoundError,
    SourceInUseError,
)

PRIMARY_STEM = "source"
DEFAULT_MEDIA_TYPE = "application/octet-stream"
UPDATABLE_FIELDS = frozenset(
    {"kind", "title", "creators", "originated_on", "summary", "origin_url", "metadata", "notes"}
)


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _creators(value: Iterable[str] | None) -> list[str]:
    """Normalise creators to a list of names (None -> [])."""
    if value is None:
        return []
    if isinstance(value, str | bytes):
        raise ValueError("creators must be a sequence of names, not a single string")
    names = builtins.list(value)
    if not all(isinstance(n, str) for n in names):
        raise ValueError("creators must contain only strings")
    return names


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise metadata to a JSON-serialisable dict (None -> {})."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a mapping")
    data = dict(value)
    try:
        json.dumps(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata is not JSON-serialisable: {exc}") from None
    return data


class KnowledgeSourceManager:
    """Adds, finds, updates, deletes and checks knowledge sources (and their identifiers)."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- creation -------------------------------------------------------------------------

    def add(
        self,
        file: Path,
        *,
        slug: str,
        kind: str,
        title: str,
        creators: Sequence[str] | None = (),
        originated_on: str | None = None,
        summary: str | None = None,
        origin_url: str | None = None,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        notes: str | None = None,
        identifiers: dict[str, str] | None = None,
        extra_files: Sequence[Path] = (),
    ) -> KnowledgeSource:
        """Copy `file` (and extras) into `knowledge_sources/<slug>/` and insert the row.

        Raises ValueError for bad arguments, FileNotFoundError for missing inputs, and
        DuplicateError for a taken slug/folder, already-stored content or a taken identifier.
        On any failure the folder this call created is removed.
        """
        validate_slug(slug)
        kind = _required_text("kind", kind)
        title = _required_text("title", title)
        creator_list = _creators(creators)
        meta = _metadata(metadata)
        ident_pairs = self._identifier_pairs(identifiers)

        file = Path(file)
        if not file.is_file():
            raise FileNotFoundError(f"source file not found: {file}")
        primary_name = PRIMARY_STEM + file.suffix.lower()
        extras = [Path(p) for p in extra_files]
        seen = {primary_name}
        for extra in extras:
            if not extra.is_file():
                raise FileNotFoundError(f"extra file not found: {extra}")
            if extra.name in seen:
                raise ValueError(f"duplicate file name in source folder: {extra.name!r}")
            seen.add(extra.name)
        if media_type is None:
            media_type = mimetypes.guess_type(file.name)[0] or DEFAULT_MEDIA_TYPE

        # Check before touching the filesystem.
        if self._find_by_slug(slug) is not None:
            raise DuplicateError(f"knowledge source slug {slug!r} already exists")
        folder = self.settings.sources_dir / slug
        if folder.exists():
            raise DuplicateError(f"folder {folder} already exists (run check() for orphans)")
        sha = sha256_file(file)
        existing = self.find_by_content(sha)
        if existing is not None:
            raise DuplicateError(f"same content already stored as {existing.slug!r}")
        for scheme, value in ident_pairs:
            self._ensure_identifier_free(scheme, value)

        created = False
        try:
            self.settings.sources_dir.mkdir(parents=True, exist_ok=True)
            folder.mkdir()
            created = True
            shutil.copyfile(file, folder / primary_name)
            for extra in extras:
                shutil.copyfile(extra, folder / extra.name)
            if sha256_file(folder / primary_name) != sha:
                raise IntegrityCheckError(f"{file} changed while it was being copied")
            # On disk before the row can be committed (teacher.files.sync_file).
            for stored in [folder / primary_name, *(folder / extra.name for extra in extras)]:
                sync_file(stored)
            sync_dir(folder)
            sync_dir(self.settings.sources_dir)
            source = KnowledgeSource(
                slug=slug,
                kind=kind,
                title=title,
                creators=creator_list,
                originated_on=originated_on,
                summary=summary,
                origin_url=origin_url,
                dir_path=to_relative(folder, self.settings.data_root),
                primary_file=primary_name,
                media_type=media_type,
                content_sha256=sha,
                metadata_=meta,
                notes=notes,
                identifiers=[KnowledgeSourceIdentifier(scheme=s, value=v) for s, v in ident_pairs],
            )
            with self.session.begin_nested():
                self.session.add(source)
                self.session.flush()
        except IntegrityError as exc:
            if created:
                shutil.rmtree(folder, ignore_errors=True)
            raise DuplicateError(f"could not add knowledge source {slug!r}: {exc.orig}") from exc
        except BaseException:
            if created:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        return source

    # -- lookups --------------------------------------------------------------------------

    def get(self, id: int) -> KnowledgeSource:
        """Return the source with primary key `id`; NotFoundError if absent."""
        source = self.session.get(KnowledgeSource, id)
        if source is None:
            raise NotFoundError(f"no knowledge source with id {id}")
        return source

    def get_by_slug(self, slug: str) -> KnowledgeSource:
        """Return the source with `slug`; NotFoundError if absent."""
        source = self._find_by_slug(slug)
        if source is None:
            raise NotFoundError(f"no knowledge source with slug {slug!r}")
        return source

    def find_by_identifier(self, scheme: str, value: str) -> KnowledgeSource | None:
        """Return the source carrying identifier (`scheme`, `value`), or None."""
        ident = self.session.get(KnowledgeSourceIdentifier, (scheme, value))
        return ident.source if ident is not None else None

    def find_by_content(self, sha256: str) -> KnowledgeSource | None:
        """Return the source whose primary file has SHA-256 `sha256` (hex), or None."""
        return self.session.scalar(
            select(KnowledgeSource).where(KnowledgeSource.content_sha256 == sha256)
        )

    def list(self, kind: str | None = None) -> builtins.list[KnowledgeSource]:
        """All sources (optionally of one `kind`), in insertion order."""
        stmt = select(KnowledgeSource).order_by(KnowledgeSource.id)
        if kind is not None:
            stmt = stmt.where(KnowledgeSource.kind == kind)
        return builtins.list(self.session.scalars(stmt))

    def primary_path(self, source: KnowledgeSource) -> Path:
        """Absolute path of the source's primary file."""
        return to_absolute(f"{source.dir_path}/{source.primary_file}", self.settings.data_root)

    # -- updates --------------------------------------------------------------------------

    def update(self, source: KnowledgeSource, **fields: Any) -> KnowledgeSource:
        """Change descriptive fields (see `UPDATABLE_FIELDS`); file fields are not updatable."""
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        for name, value in fields.items():
            if name in ("kind", "title"):
                setattr(source, name, _required_text(name, value))
            elif name == "creators":
                source.creators = _creators(value)
            elif name == "metadata":
                source.metadata_ = _metadata(value)
            else:
                setattr(source, name, value)
        self.session.flush()
        return source

    def add_identifier(
        self, source: KnowledgeSource, scheme: str, value: str
    ) -> KnowledgeSourceIdentifier:
        """Attach identifier (`scheme`, `value`); DuplicateError if any source already has it."""
        ((scheme, value),) = self._identifier_pairs({scheme: value})
        self._ensure_identifier_free(scheme, value)
        ident = KnowledgeSourceIdentifier(scheme=scheme, value=value)
        try:
            with self.session.begin_nested():
                source.identifiers.append(ident)
                self.session.flush()
        except IntegrityError as exc:
            raise DuplicateError(f"identifier {scheme}:{value} already exists") from exc
        return ident

    def remove_identifier(self, source: KnowledgeSource, scheme: str, value: str) -> None:
        """Detach identifier (`scheme`, `value`); NotFoundError if `source` does not have it."""
        ident = self.session.get(KnowledgeSourceIdentifier, (scheme, value))
        if ident is None or ident.source_id != source.id:
            raise NotFoundError(f"{source.slug!r} has no identifier {scheme}:{value}")
        source.identifiers.remove(ident)
        self.session.flush()

    # -- deletion -------------------------------------------------------------------------

    def delete(self, source: KnowledgeSource, *, remove_files: bool = True) -> None:
        """Delete the row (identifiers and segments cascade) and, by default, its folders.

        Raises SourceInUseError, touching nothing, while artifacts still reference the source.
        With `remove_files`, `knowledge_sources/<slug>/` and the `derived/<slug>/` cache are
        removed after the flush succeeds (a later rollback by the caller does not restore them).
        """
        refs = self.session.scalar(
            select(func.count())
            .select_from(ArtifactSource)
            .where(ArtifactSource.source_id == source.id)
        )
        if refs:
            raise SourceInUseError(
                f"knowledge source {source.slug!r} is used by {refs} artifact(s)"
            )
        folder = to_absolute(source.dir_path, self.settings.data_root)
        derived = self.settings.derived_dir / source.slug
        try:
            with self.session.begin_nested():
                self.session.delete(source)
                self.session.flush()
        except IntegrityError as exc:
            raise SourceInUseError(
                f"knowledge source {source.slug!r} is still referenced: {exc.orig}"
            ) from exc
        if remove_files:
            for path in (folder, derived):
                if path.is_dir():
                    shutil.rmtree(path)

    # -- consistency ----------------------------------------------------------------------

    def check(self) -> builtins.list[Issue]:
        """Compare rows with folders on disk. Issue codes:

        - 'orphan_dir':    a folder under knowledge_sources/ with no row (slug = folder name)
        - 'missing_dir':   a row whose folder does not exist
        - 'missing_file':  a row whose folder exists but whose primary file does not
        - 'hash_mismatch': the primary file's SHA-256 differs from `content_sha256`
        """
        issues: builtins.list[Issue] = []
        root = self.settings.data_root
        rows = builtins.list(
            self.session.scalars(select(KnowledgeSource).order_by(KnowledgeSource.slug))
        )
        known = {row.dir_path for row in rows}
        if self.settings.sources_dir.is_dir():
            for entry in sorted(self.settings.sources_dir.iterdir()):
                if entry.is_dir() and to_relative(entry, root) not in known:
                    issues.append(Issue("orphan_dir", entry.name, f"folder {entry} has no row"))
        for row in rows:
            folder = to_absolute(row.dir_path, root)
            if not folder.is_dir():
                issues.append(Issue("missing_dir", row.slug, f"folder {folder} is missing"))
                continue
            primary = folder / row.primary_file
            if not primary.is_file():
                issues.append(Issue("missing_file", row.slug, f"primary file {primary} is missing"))
                continue
            actual = sha256_file(primary)
            if actual != row.content_sha256:
                issues.append(
                    Issue(
                        "hash_mismatch",
                        row.slug,
                        f"{primary}: expected {row.content_sha256}, found {actual}",
                    )
                )
        return issues

    # -- helpers --------------------------------------------------------------------------

    def _find_by_slug(self, slug: str) -> KnowledgeSource | None:
        return self.session.scalar(select(KnowledgeSource).where(KnowledgeSource.slug == slug))

    @staticmethod
    def _identifier_pairs(identifiers: Mapping[str, str] | None) -> builtins.list[tuple[str, str]]:
        if identifiers is None:
            return []
        if not isinstance(identifiers, Mapping):
            raise ValueError("identifiers must be a mapping of scheme -> value")
        return [
            (_required_text("identifier scheme", s), _required_text("identifier value", v))
            for s, v in identifiers.items()
        ]

    def _ensure_identifier_free(self, scheme: str, value: str) -> None:
        ident = self.session.get(KnowledgeSourceIdentifier, (scheme, value))
        if ident is not None:
            raise DuplicateError(
                f"identifier {scheme}:{value} already belongs to {ident.source.slug!r}"
            )


__all__ = ["Issue", "KnowledgeSourceManager", "SourceInUseError"]
