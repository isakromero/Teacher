"""ArtifactBlockManager: immutable JSON block files of an artifact, and their segment references."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import (
    Artifact,
    ArtifactBlock,
    ArtifactSource,
    BlockSegmentRef,
    SourceSegment,
)
from teacher.files import sha256_bytes, to_absolute
from teacher.managers.artifacts import (
    _is_unique_violation,
    _require_persistent,
    _validate_text,
)
from teacher.managers.errors import (
    DuplicateError,
    IntegrityCheckError,
    Issue,
    NotFoundError,
)

BLOCKS_DIR = "blocks"


def block_key(position: int) -> str:
    """The key (and file stem) for a 1-based position: 1 -> 'b01'."""
    return f"b{position:02d}"


def _encode(content: dict[str, Any]) -> bytes:
    """UTF-8 JSON, indent 2, non-ASCII kept; ValueError if not JSON-serialisable."""
    try:
        text = json.dumps(content, indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise ValueError(f"block content is not JSON-serialisable: {err}") from err
    return (text + "\n").encode("utf-8")


class ArtifactBlockManager:
    """Append and read artifact blocks (and own `block_segment_refs`). Blocks are immutable."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    # -- creation ---------------------------------------------------------------------------

    def add(
        self,
        artifact: Artifact,
        *,
        type: str,
        content: dict[str, Any],
        is_gate: bool = False,
        segments: Sequence[SourceSegment] = (),
    ) -> ArtifactBlock:
        """Append a block at the next position, writing `blocks/<key>.json` first.

        `content` must be a dict (TypeError) that serialises to JSON (ValueError). Every segment
        must belong to one of the artifact's linked sources (ValueError). The file is removed
        again if inserting the row fails.
        """
        _require_persistent(artifact, "artifact")
        _validate_text(type, "type")
        if not isinstance(content, dict):
            raise TypeError(f"block content must be a dict, got {content.__class__.__name__}")
        data = _encode(content)
        segs = self._checked_segments(artifact, segments)

        blocks_dir = self._artifact_dir(artifact) / BLOCKS_DIR
        if not blocks_dir.is_dir():
            raise IntegrityCheckError(f"blocks folder {blocks_dir} of {artifact.slug!r} is missing")
        last = self.session.scalar(
            select(func.max(ArtifactBlock.position)).where(ArtifactBlock.artifact_id == artifact.id)
        )
        position = (last or 0) + 1
        key = block_key(position)
        rel_file = f"{BLOCKS_DIR}/{key}.json"
        path = blocks_dir / f"{key}.json"

        try:
            with path.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            raise IntegrityCheckError(
                f"{artifact.slug}: orphan file {rel_file} is in the way of block {key}"
            ) from None
        try:
            block = ArtifactBlock(
                artifact_id=artifact.id,
                position=position,
                key=key,
                type=type,
                is_gate=bool(is_gate),
                content_file=rel_file,
                content_sha256=sha256_bytes(data),
            )
            try:
                with self.session.begin_nested():
                    self.session.add(block)
                    self.session.flush()
                    self.session.add_all(
                        BlockSegmentRef(block_id=block.id, segment_id=seg.id) for seg in segs
                    )
                    self.session.flush()
            except IntegrityError as err:
                if _is_unique_violation(err):
                    raise DuplicateError(f"{artifact.slug}: block {key} already exists") from err
                raise
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.session.expire(artifact, ["blocks"])
        return block

    def _checked_segments(
        self, artifact: Artifact, segments: Sequence[SourceSegment]
    ) -> list[SourceSegment]:
        """De-duplicated `segments`, each verified to belong to a source linked to `artifact`."""
        if not segments:
            return []
        linked = set(
            self.session.scalars(
                select(ArtifactSource.source_id).where(ArtifactSource.artifact_id == artifact.id)
            )
        )
        unique: dict[int, SourceSegment] = {}
        for seg in segments:
            if not isinstance(seg, SourceSegment):
                raise TypeError(f"segments must be SourceSegment objects, got {seg!r}")
            _require_persistent(seg, "segment")
            if seg.source_id not in linked:
                raise ValueError(
                    f"segment {seg.label!r} belongs to source id {seg.source_id}, "
                    f"which is not linked to artifact {artifact.slug!r}"
                )
            unique.setdefault(seg.id, seg)
        return list(unique.values())

    # -- lookups ----------------------------------------------------------------------------

    def get(self, id: int) -> ArtifactBlock:
        """Return the block with `id`; NotFoundError if there is none."""
        block = self.session.get(ArtifactBlock, id)
        if block is None:
            raise NotFoundError(f"no artifact block with id {id}")
        return block

    def get_by_key(self, artifact: Artifact, key: str) -> ArtifactBlock:
        """Return the block of `artifact` with `key` (e.g. 'b03'); NotFoundError if missing."""
        block = self.session.scalar(
            select(ArtifactBlock).where(
                ArtifactBlock.artifact_id == artifact.id, ArtifactBlock.key == key
            )
        )
        if block is None:
            raise NotFoundError(f"{artifact.slug}: no block {key!r}")
        return block

    def list(self, artifact: Artifact) -> list[ArtifactBlock]:
        """The artifact's blocks in reading order."""
        return list(
            self.session.scalars(
                select(ArtifactBlock)
                .where(ArtifactBlock.artifact_id == artifact.id)
                .order_by(ArtifactBlock.position)
            )
        )

    def read_content(self, block: ArtifactBlock, *, verify: bool = True) -> dict[str, Any]:
        """Load the block's JSON content.

        Raises IntegrityCheckError if the file is missing, (with `verify`) its hash differs from
        the stored one, or it does not hold a JSON object.
        """
        path = self._block_path(block)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise IntegrityCheckError(f"block {block.key}: file {path} is missing") from None
        if verify and sha256_bytes(data) != block.content_sha256:
            raise IntegrityCheckError(f"block {block.key}: {path} does not match its stored hash")
        try:
            content = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise IntegrityCheckError(f"block {block.key}: {path} is not valid JSON") from err
        if not isinstance(content, dict):
            raise IntegrityCheckError(f"block {block.key}: {path} does not hold a JSON object")
        return content

    def segments(self, block: ArtifactBlock) -> list[SourceSegment]:
        """Segments referenced by `block`, ordered by source then position."""
        return list(
            self.session.scalars(
                select(SourceSegment)
                .join(BlockSegmentRef, BlockSegmentRef.segment_id == SourceSegment.id)
                .where(BlockSegmentRef.block_id == block.id)
                .order_by(SourceSegment.source_id, SourceSegment.position, SourceSegment.id)
            )
        )

    # -- segment references -----------------------------------------------------------------

    def add_segment_ref(self, block: ArtifactBlock, segment: SourceSegment) -> None:
        """Reference `segment` from `block`.

        ValueError if the segment's source is not linked to the block's artifact;
        DuplicateError if the reference already exists.
        """
        _require_persistent(block, "block")
        self._checked_segments(block.artifact, [segment])
        if self.session.get(BlockSegmentRef, (block.id, segment.id)) is not None:
            raise DuplicateError(f"block {block.key} already references segment {segment.label!r}")
        try:
            with self.session.begin_nested():
                self.session.add(BlockSegmentRef(block_id=block.id, segment_id=segment.id))
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(
                    f"block {block.key} already references segment {segment.label!r}"
                ) from err
            raise
        self.session.expire(block, ["segments"])
        self.session.expire(segment, ["blocks"])

    def remove_segment_ref(self, block: ArtifactBlock, segment: SourceSegment) -> None:
        """Drop the reference from `block` to `segment`; NotFoundError if there is none."""
        ref = self.session.get(BlockSegmentRef, (block.id, segment.id))
        if ref is None:
            raise NotFoundError(f"block {block.key} does not reference segment {segment.label!r}")
        self.session.delete(ref)
        self.session.flush()
        self.session.expire(block, ["segments"])
        self.session.expire(segment, ["blocks"])

    # -- consistency ------------------------------------------------------------------------

    def check(self, artifact: Artifact) -> list[Issue]:
        """Compare the artifact's block rows with its `blocks/` folder.

        Issue codes (`slug` is the artifact slug, `detail` starts with the block key):
          - 'missing_dir': the artifact's `blocks/` folder does not exist
          - 'missing_file':   a block row whose file does not exist
          - 'hash_mismatch':  a block file whose bytes differ from the stored content_sha256
          - 'orphan_file':    an entry in `blocks/` that no block row refers to
        """
        issues: list[Issue] = []
        blocks_dir = self._artifact_dir(artifact) / BLOCKS_DIR
        if not blocks_dir.is_dir():
            issues.append(
                Issue("missing_dir", artifact.slug, f"{BLOCKS_DIR}/ folder {blocks_dir} is missing")
            )
        known: set[str] = set()
        for block in self.list(artifact):
            known.add(block.content_file)
            path = self._block_path(block)
            if not path.is_file():
                issues.append(
                    Issue("missing_file", artifact.slug, f"{block.key}: {block.content_file} is missing")
                )
            elif sha256_bytes(path.read_bytes()) != block.content_sha256:
                issues.append(
                    Issue(
                        "hash_mismatch",
                        artifact.slug,
                        f"{block.key}: {block.content_file} does not match its stored hash",
                    )
                )
        if blocks_dir.is_dir():
            for entry in sorted(blocks_dir.iterdir()):
                rel = f"{BLOCKS_DIR}/{entry.name}"
                if rel not in known:
                    issues.append(
                        Issue("orphan_file", artifact.slug, f"{entry.stem}: {rel} has no block row")
                    )
        return issues

    # -- paths ------------------------------------------------------------------------------

    def _artifact_dir(self, artifact: Artifact) -> Path:
        return to_absolute(artifact.dir_path, self.settings.root)

    def _block_path(self, block: ArtifactBlock) -> Path:
        return to_absolute(block.content_file, self._artifact_dir(block.artifact))


__all__ = ["ArtifactBlockManager", "block_key"]
