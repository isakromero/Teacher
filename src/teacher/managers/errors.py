"""Exceptions raised by managers, and the `Issue` record returned by consistency checks."""

from __future__ import annotations

from dataclasses import dataclass


class ManagerError(Exception):
    """Base class for all expected manager failures."""


class NotFoundError(ManagerError, LookupError):
    """A requested row (or its file) does not exist."""


class DuplicateError(ManagerError):
    """A row with the same unique key (slug, content hash, identifier, ...) already exists."""


class IntegrityCheckError(ManagerError):
    """Stored data disagrees with the database (missing file, hash mismatch, ...)."""


class SourceInUseError(ManagerError):
    """The knowledge source is still referenced by artifacts and cannot be deleted."""


class StateConflictError(ManagerError):
    """The row is not in a state that allows the change (an exploration still running, a
    candidate already ingested, ...); the message says which (HTTP 409)."""


class RelationConflictError(DuplicateError):
    """Two concepts can have at most one relation (of any type); the message names the existing one."""


class CycleError(ManagerError):
    """`builds_on` would become (or is) cyclic. `cycle` lists the concept slugs in edge
    direction, first slug repeated at the end (`a -> b -> a`: a builds on b, b builds on a)."""

    def __init__(self, message: str, cycle: tuple[str, ...]) -> None:
        super().__init__(message)
        self.cycle = cycle


class EmbeddingIndexError(ManagerError):
    """The stored concept embeddings cannot be used (mixed dimensions, a corrupt vector, ...)."""


class StaleEmbeddingsError(EmbeddingIndexError):
    """Some concepts have no up-to-date embedding for the model, so a similarity search over the
    index would silently skip them. `stale_count` of `concept_count` concepts are stale (both
    None only when a caller raises it without counting)."""

    def __init__(
        self, message: str, *, stale_count: int | None = None, concept_count: int | None = None
    ) -> None:
        super().__init__(message)
        self.stale_count = stale_count
        self.concept_count = concept_count


class VectorIndexError(EmbeddingIndexError):
    """The on-disk vector index of the concept embeddings is missing a file, unreadable,
    corrupt or mismatched; the message names the problem and the command that rebuilds it."""


@dataclass(frozen=True, slots=True)
class Issue:
    """One problem found by a `check()` method.

    `code` is a short machine-readable tag (e.g. 'missing_dir', 'hash_mismatch');
    `slug` is the knowledge source or artifact slug concerned; `detail` is human-readable.
    """

    code: str
    slug: str
    detail: str


__all__ = [
    "CycleError",
    "DuplicateError",
    "EmbeddingIndexError",
    "IntegrityCheckError",
    "Issue",
    "ManagerError",
    "NotFoundError",
    "RelationConflictError",
    "SourceInUseError",
    "StaleEmbeddingsError",
    "VectorIndexError",
]
