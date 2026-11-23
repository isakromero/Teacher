"""SourceSegmentManager: addressable parts (sections, figures, hunks...) of a knowledge source."""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import KnowledgeSource, SourceSegment
from teacher.files import to_relative
from teacher.locators import Locator, parse_locator
from teacher.managers.errors import DuplicateError, NotFoundError
from teacher.managers.knowledge_sources import _required_text

SEGMENT_KEYS = frozenset(
    {"kind", "label", "locator", "title", "parent", "position", "content_file"}
)
PARENT_REF_KEYS = frozenset({"parent_label", "parent_kind"})


class SourceSegmentManager:
    """Adds, finds, lists and deletes source segments. Locators are validated on every write."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def add(
        self,
        source: KnowledgeSource,
        *,
        kind: str,
        label: str,
        locator: Locator | dict[str, Any],
        title: str | None = None,
        parent: SourceSegment | None = None,
        position: int | None = None,
        content_file: str | Path | None = None,
    ) -> SourceSegment:
        """Insert a segment; `position` defaults to one past the source's current maximum.

        Raises pydantic.ValidationError (a ValueError) for a bad locator, ValueError for a
        parent from another source or a content_file outside the root, and DuplicateError
        if (source, kind, label) is taken.
        """
        kind = _required_text("kind", kind)
        label = _required_text("label", label)
        loc = parse_locator(locator)
        if parent is not None and parent.source_id != source.id:
            raise ValueError(f"parent segment {parent.label!r} belongs to another knowledge source")
        if position is None:
            position = self._next_position(source)
        elif not isinstance(position, int) or isinstance(position, bool):
            raise ValueError("position must be an integer")
        rel_content = (
            to_relative(Path(content_file), self.settings.root)
            if content_file is not None
            else None
        )
        if self._find_by_label(source, kind, label) is not None:
            raise DuplicateError(f"{source.slug!r} already has a {kind} labelled {label!r}")

        segment = SourceSegment(
            kind=kind,
            label=label,
            title=title,
            position=position,
            locator_type=loc.type,
            locator=loc.model_dump(),
            content_file=rel_content,
        )
        try:
            with self.session.begin_nested():
                # Add before linking so the backref appends happen on a session object.
                self.session.add(segment)
                segment.source = source
                segment.parent = parent
                self.session.flush()
        except IntegrityError as exc:
            raise DuplicateError(
                f"could not add {kind} {label!r} to {source.slug!r}: {exc.orig}"
            ) from exc
        return segment

    def add_many(
        self, source: KnowledgeSource, segments: Iterable[Mapping[str, Any]]
    ) -> builtins.list[SourceSegment]:
        """Add several segments atomically (all or none), in the given order.

        Each dict takes `add`'s keyword arguments. Instead of `parent`, a dict may give
        `parent_label` and `parent_kind` (both required) naming a segment added earlier in the
        same call or already stored; NotFoundError if it does not exist.
        """
        added: builtins.list[SourceSegment] = []
        with self.session.begin_nested():
            for index, spec in enumerate(segments):
                kwargs = dict(spec)
                unknown = set(kwargs) - SEGMENT_KEYS - PARENT_REF_KEYS
                if unknown:
                    raise ValueError(f"segment #{index}: unknown key(s) {sorted(unknown)}")
                ref = {k: kwargs.pop(k) for k in PARENT_REF_KEYS if k in kwargs}
                if ref:
                    if set(ref) != PARENT_REF_KEYS:
                        raise ValueError(
                            f"segment #{index}: give both parent_label and parent_kind"
                        )
                    if kwargs.get("parent") is not None:
                        raise ValueError(
                            f"segment #{index}: give either parent or parent_label/parent_kind"
                        )
                    kwargs["parent"] = self.get_by_label(
                        source, ref["parent_kind"], ref["parent_label"]
                    )
                added.append(self.add(source, **kwargs))
        return added

    def get(self, id: int) -> SourceSegment:
        """Return the segment with primary key `id`; NotFoundError if absent."""
        segment = self.session.get(SourceSegment, id)
        if segment is None:
            raise NotFoundError(f"no source segment with id {id}")
        return segment

    def get_by_label(self, source: KnowledgeSource, kind: str, label: str) -> SourceSegment:
        """Return the segment (`kind`, `label`) of `source`; NotFoundError if absent."""
        segment = self._find_by_label(source, kind, label)
        if segment is None:
            raise NotFoundError(f"{source.slug!r} has no {kind} labelled {label!r}")
        return segment

    def list(
        self, source: KnowledgeSource, kind: str | None = None
    ) -> builtins.list[SourceSegment]:
        """Segments of `source` (optionally of one `kind`) ordered by position."""
        stmt = (
            select(SourceSegment)
            .where(SourceSegment.source_id == source.id)
            .order_by(SourceSegment.position, SourceSegment.id)
        )
        if kind is not None:
            stmt = stmt.where(SourceSegment.kind == kind)
        return builtins.list(self.session.scalars(stmt))

    def children(self, segment: SourceSegment) -> builtins.list[SourceSegment]:
        """Direct children of `segment`, ordered by position."""
        stmt = (
            select(SourceSegment)
            .where(SourceSegment.parent_id == segment.id)
            .order_by(SourceSegment.position, SourceSegment.id)
        )
        return builtins.list(self.session.scalars(stmt))

    def locator(self, segment: SourceSegment) -> Locator:
        """The segment's stored locator parsed back into its Pydantic model."""
        return parse_locator(segment.locator)

    def delete(self, segment: SourceSegment) -> None:
        """Delete `segment`; its descendants and block references cascade. Files are untouched."""
        source, parent = segment.source, segment.parent
        self.session.delete(segment)
        self.session.flush()
        self.session.expire(source, ["segments"])
        if parent is not None:
            self.session.expire(parent, ["children"])

    def delete_all(self, source: KnowledgeSource) -> int:
        """Delete every segment of `source`; returns how many rows were removed."""
        # Count first: rows removed by the parent_id cascade are not reliably in rowcount.
        count = self.session.scalar(
            select(func.count())
            .select_from(SourceSegment)
            .where(SourceSegment.source_id == source.id)
        )
        self.session.execute(
            delete(SourceSegment)
            .where(SourceSegment.source_id == source.id)
            .execution_options(synchronize_session="fetch")
        )
        self.session.expire(source, ["segments"])
        return count or 0

    def _next_position(self, source: KnowledgeSource) -> int:
        current = self.session.scalar(
            select(func.max(SourceSegment.position)).where(SourceSegment.source_id == source.id)
        )
        return (current or 0) + 1

    def _find_by_label(
        self, source: KnowledgeSource, kind: str, label: str
    ) -> SourceSegment | None:
        return self.session.scalar(
            select(SourceSegment).where(
                SourceSegment.source_id == source.id,
                SourceSegment.kind == kind,
                SourceSegment.label == label,
            )
        )


__all__ = ["SourceSegmentManager"]
