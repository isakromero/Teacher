"""Knowledge source endpoints: upload, inspect, list, show, edit, delete, download, segments, and
the concepts concept extraction linked the source to (`GET /api/sources/{slug}/concepts`).

A source is added from an uploaded file (`POST /api/sources`, details detected by
`POST /api/sources/inspect`) or from a web address (`POST /api/sources/from-url`, details
detected by `POST /api/sources/inspect-url`); `teacher.url_sources` fetches the address and
turns a web page into Markdown, and a YouTube video into its transcript as Markdown
(`teacher.youtube_sources`). Both URL endpoints fetch the address themselves, so what is
stored is what the server read when the source was added."""

from __future__ import annotations

import mimetypes
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from teacher.db.engine import session_scope
from teacher.db.models import KnowledgeSource, SourceSegment
from teacher.files import sha256_file, slugify
from teacher.managers import (
    KnowledgeSourceManager,
    NotFoundError,
    SourceConceptManager,
    SourceSegmentManager,
    StudyHistoryManager,
)
from teacher.metadata import MetadataUnavailableError
from teacher.source_ingest import detect_details, split_identifiers, store_fetched, write_fetched
from teacher.url_sources import KIND_PAPER, FetchedSource
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.references import after_source_added, rematch_library

router = APIRouter(prefix="/api", tags=["sources"])

_UPLOAD_CHUNK = 1024 * 1024
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")
_PARTIAL_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


# -- response models ----------------------------------------------------------------------


class IdentifierOut(BaseModel):
    scheme: str
    value: str


class SourceOut(BaseModel):
    slug: str
    kind: str
    title: str
    creators: list[str]
    originated_on: str | None
    summary: str | None
    origin_url: str | None
    media_type: str
    content_sha256: str
    notes: str | None
    metadata: dict[str, Any]
    added_at: str
    updated_at: str
    identifiers: list[IdentifierOut]


class SourceArtifactOut(BaseModel):
    slug: str
    title: str
    kind: str
    status: str


class SourceDetailOut(SourceOut):
    artifacts: list[SourceArtifactOut]


class InspectOut(BaseModel):
    """Details detected from an uploaded file (nothing is stored). A detail that could not be
    found is null (authors: empty). `duplicate_of` is the slug of a source with exactly the same
    content; detection is skipped then (every detail is null and `model` is null), because the
    upload would be rejected anyway."""

    title: str | None
    authors: list[str]
    year: int | None
    arxiv_id: str | None
    doi: str | None
    summary: str | None
    model: str | None
    duplicate_of: str | None


class InspectUrlOut(InspectOut):
    """Details detected from a web address (nothing is stored). `url` is the address after
    redirects and `kind` what it holds: "paper" (a PDF, its details detected like an uploaded
    one) or "web_page" (its details read from the page itself; `model` is null). `words` is
    the length of a page's readable text (null for a PDF). `detection_error` says why a PDF's
    details could not be detected; they are then null and are typed by hand."""

    url: str
    kind: str
    words: int | None
    detection_error: str | None


class UrlIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class SourceFromUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    slug: str | None = None
    creators: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list)
    originated_on: str | None = None
    summary: str | None = None
    notes: str | None = None

    @field_validator("originated_on")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        return check_originated_on(_blank_to_none(value))


class SegmentOut(BaseModel):
    id: int
    kind: str
    label: str
    title: str | None
    parent_id: int | None
    position: int
    locator: dict[str, Any]


class SourceConceptOut(BaseModel):
    """One concept the source is linked to. `role`: introduces | uses | assumes; `resolution`:
    how the extraction found it (created | exact | judged); `reviewed`: whether a person has
    reviewed the concept."""

    slug: str
    name: str
    role: str
    resolution: str
    reviewed: bool


def check_originated_on(value: str | None) -> str | None:
    """Accept an ISO 8601 calendar date of reduced precision: YYYY, YYYY-MM or YYYY-MM-DD."""
    if value is None:
        return None
    if not _PARTIAL_DATE.fullmatch(value):
        raise ValueError(f"originated_on must be YYYY, YYYY-MM or YYYY-MM-DD, got {value!r}")
    year, month, day = ([int(p) for p in value.split("-")] + [1, 1])[:3]
    try:
        date(year, month, day)
    except ValueError:
        raise ValueError(f"originated_on is not a valid date: {value!r}") from None
    return value


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    summary: str | None = None
    notes: str | None = None
    creators: list[str] | None = None
    origin_url: str | None = None
    originated_on: str | None = None

    @field_validator("originated_on")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        return check_originated_on(value)


def source_out(source: KnowledgeSource) -> SourceOut:
    return SourceOut(**_source_fields(source))


def _source_fields(source: KnowledgeSource) -> dict[str, Any]:
    return {
        "slug": source.slug,
        "kind": source.kind,
        "title": source.title,
        "creators": list(source.creators or []),
        "originated_on": source.originated_on,
        "summary": source.summary,
        "origin_url": source.origin_url,
        "media_type": source.media_type,
        "content_sha256": source.content_sha256,
        "notes": source.notes,
        "metadata": dict(source.metadata_ or {}),
        "added_at": to_iso(source.added_at),
        "updated_at": to_iso(source.updated_at),
        "identifiers": [IdentifierOut(scheme=i.scheme, value=i.value) for i in source.identifiers],
    }


def segment_out(segment: SourceSegment, manager: SourceSegmentManager) -> SegmentOut:
    return SegmentOut(
        id=segment.id,
        kind=segment.kind,
        label=segment.label,
        title=segment.title,
        parent_id=segment.parent_id,
        position=segment.position,
        locator=manager.locator(segment).model_dump(),
    )


# -- helpers ------------------------------------------------------------------------------


def _blank_to_none(value: str | None) -> str | None:
    """HTML forms send empty strings for untouched optional fields."""
    if value is None or not value.strip():
        return None
    return value


def _parse_identifiers(raw: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not item.strip():
            continue
        scheme, sep, value = item.partition(":")
        scheme, value = scheme.strip(), value.strip()
        if not sep or not scheme or not value:
            raise ValueError(f"identifier must look like scheme:value, got {item!r}")
        if (scheme, value) not in pairs:
            pairs.append((scheme, value))
    return pairs


def _upload_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""


def _media_type(upload: UploadFile) -> str | None:
    guessed = mimetypes.guess_type(upload.filename or "")[0]
    if guessed:
        return guessed
    declared = (upload.content_type or "").split(";")[0].strip()
    return declared or None


# Shared with an exploration's enrichment (`teacher.source_ingest`).
_split_identifiers = split_identifiers
_write_fetched = write_fetched


# -- endpoints ----------------------------------------------------------------------------


@router.post("/sources", status_code=201)
def create_source(
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()],
    title: Annotated[str, Form()],
    slug: Annotated[str | None, Form()] = None,
    creators: Annotated[list[str] | None, Form()] = None,
    identifiers: Annotated[list[str] | None, Form()] = None,
    originated_on: Annotated[str | None, Form()] = None,
    summary: Annotated[str | None, Form()] = None,
    origin_url: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
) -> SourceOut:
    """Upload a file as a new knowledge source (multipart form)."""
    slug = _blank_to_none(slug) or slugify(title)
    creator_list = [c.strip() for c in creators or [] if c.strip()]
    pairs = _parse_identifiers(identifiers or [])
    originated = check_originated_on(_blank_to_none(originated_on))

    manager = KnowledgeSourceManager(session, settings)
    first, rest = _split_identifiers(manager, pairs)

    with tempfile.TemporaryDirectory(prefix="teacher-upload-") as tmp:
        tmp_path = Path(tmp) / f"upload{_upload_suffix(file.filename)}"
        with tmp_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh, _UPLOAD_CHUNK)
        if tmp_path.stat().st_size == 0:
            raise ValueError("uploaded file is empty")
        source = manager.add(
            tmp_path,
            slug=slug,
            kind=kind,
            title=title,
            creators=creator_list,
            originated_on=originated,
            summary=_blank_to_none(summary),
            origin_url=_blank_to_none(origin_url),
            notes=_blank_to_none(notes),
            media_type=_media_type(file),
            identifiers=first or None,
        )
    for scheme, value in rest:
        manager.add_identifier(source, scheme, value)
    StudyHistoryManager(session, settings).log(
        "source_added", source=source, payload={"via": "upload"}
    )
    # Citations: re-match stored references, extract this one's in the background.
    after_source_added(request, background, session, settings, source)
    return source_out(source)


@router.post("/sources/inspect")
def inspect_source(
    request: Request,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> InspectOut:
    """Detect a paper's title, authors, year, arXiv id, DOI and summary from its PDF.

    Stores nothing. Not a PDF, empty or unreadable: 422. Detection failed (credentials, API
    error, refusal, invalid reply): 503. Neither falls back to guessed values.
    """
    media_type = _media_type(file)
    if media_type != "application/pdf":
        raise ValueError(
            "Automatic detection supports PDF files only"
            f"{f' (this file is {media_type})' if media_type else ''}. "
            "Type the title and the other details by hand."
        )
    with tempfile.TemporaryDirectory(prefix="teacher-inspect-") as tmp:
        tmp_path = Path(tmp) / "upload.pdf"
        with tmp_path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh, _UPLOAD_CHUNK)
        if tmp_path.stat().st_size == 0:
            raise ValueError("uploaded file is empty")
        # A short session of its own: the extractor call below can take a while.
        with session_scope(settings) as session:
            existing = KnowledgeSourceManager(session, settings).find_by_content(
                sha256_file(tmp_path)
            )
            duplicate_of = existing.slug if existing is not None else None
        if duplicate_of is not None:
            return InspectOut(
                title=None,
                authors=[],
                year=None,
                arxiv_id=None,
                doi=None,
                summary=None,
                model=None,
                duplicate_of=duplicate_of,
            )
        detected = request.app.state.metadata_extractor.extract(tmp_path, media_type)
    return InspectOut(
        title=detected.title,
        authors=list(detected.authors),
        year=detected.year,
        arxiv_id=detected.arxiv_id,
        doi=detected.doi,
        summary=detected.summary,
        model=detected.model,
        duplicate_of=None,
    )


@router.get("/sources")
def list_sources(
    session: SessionDep,
    settings: SettingsDep,
    kind: Annotated[str | None, Query()] = None,
) -> list[SourceOut]:
    return [source_out(s) for s in KnowledgeSourceManager(session, settings).list(kind=kind)]


@router.get("/sources/{slug}")
def get_source(slug: str, session: SessionDep, settings: SettingsDep) -> SourceDetailOut:
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    artifacts = [
        SourceArtifactOut(slug=a.slug, title=a.title, kind=a.kind, status=a.status)
        for a in source.artifacts
    ]
    return SourceDetailOut(**_source_fields(source), artifacts=artifacts)


@router.patch("/sources/{slug}")
def update_source(
    slug: str, patch: SourcePatch, session: SessionDep, settings: SettingsDep
) -> SourceOut:
    manager = KnowledgeSourceManager(session, settings)
    source = manager.get_by_slug(slug)
    fields = patch.model_dump(exclude_unset=True)
    if fields:
        manager.update(source, **fields)
        StudyHistoryManager(session, settings).log(
            "source_updated", source=source, payload={"fields": sorted(fields)}
        )
        if fields.keys() & {"title", "originated_on", "origin_url"}:
            rematch_library(session, settings)  # what references match may have changed
    return source_out(source)


@router.delete("/sources/{slug}", status_code=204)
def delete_source(slug: str, session: SessionDep, settings: SettingsDep) -> Response:
    manager = KnowledgeSourceManager(session, settings)
    source = manager.get_by_slug(slug)
    StudyHistoryManager(session, settings).log(
        "source_deleted", source=source, payload={"slug": source.slug, "title": source.title}
    )
    manager.delete(source)
    rematch_library(session, settings)  # references to it are unmatched now
    return Response(status_code=204)


@router.get("/sources/{slug}/file", response_class=FileResponse)
def get_source_file(slug: str, session: SessionDep, settings: SettingsDep) -> FileResponse:
    manager = KnowledgeSourceManager(session, settings)
    source = manager.get_by_slug(slug)
    path = manager.primary_path(source)
    if not path.is_file():
        raise NotFoundError(f"primary file of {slug!r} is missing")
    return FileResponse(
        path,
        media_type=source.media_type,
        filename=f"{source.slug}{path.suffix}",
        content_disposition_type="inline",
    )


@router.get("/sources/{slug}/concepts")
def list_source_concepts(
    slug: str, session: SessionDep, settings: SettingsDep
) -> list[SourceConceptOut]:
    """The concepts the source introduces, uses or assumes, by role, then name (empty when no
    concepts have been extracted yet)."""
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    return [
        SourceConceptOut(
            slug=link.concept.slug,
            name=link.concept.name,
            role=link.role,
            resolution=link.resolution,
            reviewed=link.concept.reviewed_at is not None,
        )
        for link in SourceConceptManager(session, settings).for_source(source)
    ]


@router.get("/sources/{slug}/segments")
def list_segments(
    slug: str,
    session: SessionDep,
    settings: SettingsDep,
    kind: Annotated[str | None, Query()] = None,
) -> list[SegmentOut]:
    source = KnowledgeSourceManager(session, settings).get_by_slug(slug)
    manager = SourceSegmentManager(session, settings)
    return [segment_out(s, manager) for s in manager.list(source, kind=kind)]


__all__ = [
    "IdentifierOut",
    "InspectOut",
    "SegmentOut",
    "SourceArtifactOut",
    "SourceConceptOut",
    "SourceDetailOut",
    "SourceOut",
    "SourcePatch",
    "router",
]


@router.post("/sources/inspect-url")
def inspect_url(request: Request, settings: SettingsDep, body: UrlIn) -> InspectUrlOut:
    """Fetch a web address and detect its details. Stores nothing.

    An address that cannot be fetched or read (see `teacher.url_sources`): 422. A PDF's details
    are detected like an uploaded PDF's; if that fails, the reply still describes the address,
    with `detection_error` set and the details null.
    """
    fetched: FetchedSource = request.app.state.url_fetcher.fetch(body.url)
    with tempfile.TemporaryDirectory(prefix="teacher-inspect-") as tmp:
        path = _write_fetched(fetched, tmp)
        with session_scope(settings) as session:
            existing = KnowledgeSourceManager(session, settings).find_by_content(sha256_file(path))
            duplicate_of = existing.slug if existing is not None else None
        out: dict[str, Any] = {
            "title": None, "authors": [], "year": None, "arxiv_id": None, "doi": None,
            "summary": None, "model": None, "duplicate_of": duplicate_of,
            "url": fetched.url, "kind": fetched.kind, "words": fetched.words,
            "detection_error": None,
        }  # fmt: skip
        if duplicate_of is not None:
            return InspectUrlOut(**out)
        try:
            detected = detect_details(fetched, path, request.app.state.metadata_extractor)
        except MetadataUnavailableError as exc:
            if fetched.kind != KIND_PAPER:
                raise
            out["detection_error"] = str(exc)
        else:
            if detected is not None:
                out.update(
                    title=detected.title, authors=list(detected.authors), year=detected.year,
                    arxiv_id=detected.arxiv_id, doi=detected.doi, summary=detected.summary,
                    model=detected.model,
                )  # fmt: skip
    return InspectUrlOut(**out)


@router.post("/sources/from-url", status_code=201)
def create_source_from_url(
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
    body: SourceFromUrl,
) -> SourceOut:
    """Fetch a web address and store it as a new knowledge source: a PDF as it is (kind
    "paper"), a web page as Markdown (kind "web_page"), a YouTube video as its transcript in
    Markdown (kind "web_page"). `origin_url` is the address after redirects (a video's
    canonical watch address); `metadata` records when it was fetched, the address asked for
    and what the server sent, plus the fetcher's `extra_metadata` (for a video: its id,
    channel, duration in seconds, and the transcript's language and whether it was generated
    automatically)."""
    slug = _blank_to_none(body.slug) or slugify(body.title)
    creators = [c.strip() for c in body.creators if c.strip()]
    pairs = _parse_identifiers(body.identifiers)
    manager = KnowledgeSourceManager(session, settings)
    _split_identifiers(manager, pairs)  # taken identifiers fail before anything is fetched

    fetched: FetchedSource = request.app.state.url_fetcher.fetch(body.url)
    source = store_fetched(
        manager,
        fetched,
        slug=slug,
        title=body.title,
        creators=creators,
        originated_on=body.originated_on,
        summary=_blank_to_none(body.summary),
        notes=_blank_to_none(body.notes),
        identifiers=pairs,
    )
    StudyHistoryManager(session, settings).log(
        "source_added", source=source, payload={"via": "url"}
    )
    # Citations: re-match stored references, extract this one's in the background.
    after_source_added(request, background, session, settings, source)
    return source_out(source)
