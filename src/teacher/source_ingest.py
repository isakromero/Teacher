"""Storing a fetched web address as a knowledge source: what `POST /api/sources/from-url` (the Add
page's "Add to library") does, and what an exploration's enrichment does with each approved
candidate (`teacher.exploration.enrichment`), so both store the same file, details and metadata
with the same checks.

- `write_fetched(fetched, folder)`: the fetched bytes as a file in `folder` (a temporary one).
- `detect_details(fetched, path, extractor)`: the details the Add page offers for an address
  (`POST /api/sources/inspect-url`): a PDF's from the metadata extractor (one model call,
  `teacher.metadata`; MetadataUnavailableError or UnreadablePdfError when it fails), a web
  page's or a video's from what it says about itself (`teacher.url_sources.PageDetails`, no
  model), None when there are none.
- `fetched_metadata(fetched)`: the stored source's `metadata`: when it was fetched, the address
  asked for, what the server sent, the page's word count and site name, and the fetcher's
  `extra_metadata` (a YouTube video's id, channel, duration and transcript).
- `split_identifiers(manager, pairs)`: every `(scheme, value)` checked free (DuplicateError
  naming the source that has it), split into the first value per scheme and the rest.
- `store_fetched(manager, fetched, ...)`: the source added (`KnowledgeSourceManager.add`, which
  refuses a taken slug or folder and content already stored, DuplicateError), with the
  fetched address after redirects as `origin_url`, and the extra identifiers attached.

Fetched text is never put into a prompt here; it is stored as the source's file (the snapshot
generation and the tutor read).
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from teacher.db.models import KnowledgeSource
from teacher.managers import DuplicateError, KnowledgeSourceManager
from teacher.url_sources import KIND_PAPER, FetchedSource


@dataclass(frozen=True)
class DetectedDetails:
    """What was detected about a fetched address (a field not found is None; authors empty).
    `model` is the model that read a PDF (None for a page's own details)."""

    title: str | None = None
    authors: tuple[str, ...] = field(default=())
    year: int | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    summary: str | None = None
    model: str | None = None


def write_fetched(fetched: FetchedSource, folder: str | Path) -> Path:
    path = Path(folder) / f"fetched{fetched.suffix}"
    path.write_bytes(fetched.content)
    return path


def detect_details(fetched: FetchedSource, path: Path, extractor: Any) -> DetectedDetails | None:
    """The details of `fetched` (stored at `path`), see the module docstring."""
    if fetched.kind == KIND_PAPER:
        found = extractor.extract(path, fetched.media_type)
        return DetectedDetails(
            title=found.title, authors=tuple(found.authors), year=found.year,
            arxiv_id=found.arxiv_id, doi=found.doi, summary=found.summary, model=found.model,
        )  # fmt: skip
    details = fetched.details
    if details is None:
        return None
    return DetectedDetails(
        title=details.title, authors=tuple(details.authors), year=details.year,
        arxiv_id=details.arxiv_id, doi=details.doi, summary=details.summary,
    )  # fmt: skip


def fetched_metadata(fetched: FetchedSource, *, now: datetime | None = None) -> dict[str, Any]:
    """The stored source's metadata for `fetched` (see the module docstring)."""
    when = now if now is not None else datetime.now(UTC)
    metadata: dict[str, Any] = {
        "fetched_at": when.isoformat(timespec="seconds"),
        "requested_url": fetched.requested_url,
        "original_media_type": fetched.original_media_type,
    }
    if fetched.words is not None:
        metadata["words"] = fetched.words
    if fetched.details is not None and fetched.details.site_name:
        metadata["site_name"] = fetched.details.site_name
    metadata.update(fetched.extra_metadata)
    return metadata


def split_identifiers(
    manager: KnowledgeSourceManager, pairs: Sequence[tuple[str, str]]
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Check that every identifier is free, then split them into the first value per scheme
    (for `manager.add`) and the rest (attached afterwards). Checked up front: the manager's
    add() takes one value per scheme, and the extra values must not fail once the files have
    been copied."""
    for scheme, value in pairs:
        owner = manager.find_by_identifier(scheme, value)
        if owner is not None:
            raise DuplicateError(f"identifier {scheme}:{value} already belongs to {owner.slug!r}")
    first: dict[str, str] = {}
    rest: list[tuple[str, str]] = []
    for scheme, value in pairs:
        if scheme in first:
            rest.append((scheme, value))
        else:
            first[scheme] = value
    return first, rest


def store_fetched(
    manager: KnowledgeSourceManager,
    fetched: FetchedSource,
    *,
    slug: str,
    title: str,
    creators: Sequence[str] = (),
    originated_on: str | None = None,
    summary: str | None = None,
    notes: str | None = None,
    identifiers: Sequence[tuple[str, str]] = (),
    metadata: Mapping[str, Any] | None = None,
) -> KnowledgeSource:
    """Add `fetched` as a new knowledge source (see the module docstring). `metadata` is added
    to `fetched_metadata(fetched)`."""
    first, rest = split_identifiers(manager, list(identifiers))
    stored = fetched_metadata(fetched)
    stored.update(metadata or {})
    with tempfile.TemporaryDirectory(prefix="teacher-fetch-") as tmp:
        source = manager.add(
            write_fetched(fetched, tmp),
            slug=slug,
            kind=fetched.kind,
            title=title,
            creators=list(creators),
            originated_on=originated_on,
            summary=summary,
            origin_url=fetched.url,
            notes=notes,
            media_type=fetched.media_type,
            metadata=stored,
            identifiers=first or None,
        )
    for scheme, value in rest:
        manager.add_identifier(source, scheme, value)
    return source


__all__ = [
    "DetectedDetails",
    "detect_details",
    "fetched_metadata",
    "split_identifiers",
    "store_fetched",
    "write_fetched",
]
