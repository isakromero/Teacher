"""GlossaryManager: an artifact's glossary terms, replaced as a whole and read in order.

A glossary is always built in one go (by the generator's `glossary` stage or on demand), so the
only write is `replace`: it deletes the artifact's terms, stores the new ones at positions
1, 2, ... and sets `artifacts.glossary_built_at`. That timestamp is what tells "never built"
(NULL) apart from "built, with no terms" (set, no rows).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, GlossaryTerm, utcnow
from teacher.glossary import KINDS, GlossaryEntry
from teacher.managers.artifacts import _is_unique_violation, _require_persistent
from teacher.managers.errors import DuplicateError


def _text(value: object, name: str, number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"glossary term {number}: {name} must be a non-empty string")
    return value


def _checked(entry: object, number: int) -> GlossaryEntry:
    if not isinstance(entry, GlossaryEntry):
        raise TypeError(
            f"glossary term {number} must be a GlossaryEntry, got {entry.__class__.__name__}"
        )
    _text(entry.term, "term", number)
    _text(entry.definition, "definition", number)
    if entry.kind not in KINDS:
        raise ValueError(f"glossary term {number}: invalid kind {entry.kind!r}: expected one of {KINDS}")
    if entry.expansion is not None:
        _text(entry.expansion, "expansion", number)
    if not isinstance(entry.aliases, tuple | list):
        raise TypeError(f"glossary term {number}: aliases must be a list of strings")
    for alias in entry.aliases:
        _text(alias, "every alias", number)
    return entry


def to_entry(row: GlossaryTerm) -> GlossaryEntry:
    return GlossaryEntry(
        term=row.term,
        kind=row.kind,
        definition=row.definition,
        expansion=row.expansion,
        aliases=tuple(row.aliases or ()),
    )


class GlossaryManager:
    """Replace and list the glossary terms of an artifact."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def replace(
        self, artifact: Artifact, entries: Sequence[GlossaryEntry]
    ) -> builtins.list[GlossaryTerm]:
        """Make `entries` the artifact's whole glossary (in that order) and mark it built.

        Every entry is checked first (TypeError/ValueError); two terms equal apart from case are
        a DuplicateError, since the page could not tell them apart. Nothing changes on error.
        An empty `entries` stores an empty, built glossary.
        """
        _require_persistent(artifact, "artifact")
        checked = [_checked(entry, number) for number, entry in enumerate(entries, start=1)]
        seen: dict[str, int] = {}
        for number, entry in enumerate(checked, start=1):
            key = entry.term.casefold()
            if key in seen:
                raise DuplicateError(
                    f"glossary term {number} ({entry.term!r}) repeats term {seen[key]} "
                    "(terms must differ by more than case)"
                )
            seen[key] = number
        rows = [
            GlossaryTerm(
                artifact_id=artifact.id,
                position=position,
                term=entry.term,
                kind=entry.kind,
                expansion=entry.expansion,
                definition=entry.definition,
                aliases=list(entry.aliases),
            )
            for position, entry in enumerate(checked, start=1)
        ]
        try:
            with self.session.begin_nested():
                self.session.execute(
                    delete(GlossaryTerm).where(GlossaryTerm.artifact_id == artifact.id)
                )
                self.session.add_all(rows)
                artifact.glossary_built_at = utcnow()
                self.session.flush()
        except IntegrityError as err:
            if _is_unique_violation(err):
                raise DuplicateError(f"{artifact.slug}: duplicate glossary term") from err
            raise
        self.session.expire(artifact, ["glossary_terms"])
        return rows

    def list(self, artifact: Artifact) -> builtins.list[GlossaryTerm]:
        """The artifact's terms in glossary order (empty if none or never built)."""
        return list(
            self.session.scalars(
                select(GlossaryTerm)
                .where(GlossaryTerm.artifact_id == artifact.id)
                .order_by(GlossaryTerm.position)
            )
        )

    def entries(self, artifact: Artifact) -> builtins.list[GlossaryEntry]:
        """`list` as `GlossaryEntry` values."""
        return [to_entry(row) for row in self.list(artifact)]

    @staticmethod
    def is_built(artifact: Artifact) -> bool:
        """Whether a glossary was ever built for the artifact (possibly with no terms)."""
        return artifact.glossary_built_at is not None


__all__ = ["GlossaryManager", "to_entry"]
