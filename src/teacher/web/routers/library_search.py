"""Search across the library (the home page's search box): `GET /api/search?q=&limit=`.

-> `{query, words, prefix, total, limit, results}`: `total` items match, the best `limit`
(default 20, at most 50) are given, best first. Each result is one study page, lab or source:
`{kind, slug, title, title_segments, url, authors, year, status, superseded, score, fields,
match: {field, section, block, term, concept, locked, href}, snippet, study}`. Text to show
with the matched words marked comes as segments `[{"text", "mark"}]`, never as HTML.
`study` is the reader's standing with it: for a study page or lab `{state (not_ready |
not_opened | in_progress | complete), active_seconds, last_studied_at, gates_total,
gates_cleared}` (the gates only for a study page), for a source `{state (not_opened | opened),
active_seconds, last_studied_at, study_pages}`.

The matching and ranking rules are in `teacher.search`. A missing, empty or too short query is
a 422 with a message, as is a block file that cannot be read (the search fails with the reason
rather than leaving a page out). Searching is not studying, so nothing is logged.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from teacher.search import DEFAULT_LIMIT, MAX_LIMIT, search
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.reading import MaxAttemptsDep

router = APIRouter(prefix="/api", tags=["search"])


class SegmentOut(BaseModel):
    text: str
    mark: bool


class MatchOut(BaseModel):
    field: str
    section: str | None
    block: str | None
    term: str | None
    concept: str | None
    locked: bool
    href: str | None


class SearchResultOut(BaseModel):
    kind: str
    slug: str
    title: str
    title_segments: list[SegmentOut]
    url: str
    authors: list[str]
    year: str | None
    status: str | None
    superseded: bool
    score: float
    fields: list[str]
    match: MatchOut
    snippet: list[SegmentOut]
    study: dict[str, Any]


class SearchOut(BaseModel):
    query: str
    words: list[str]
    prefix: bool
    total: int
    limit: int
    results: list[SearchResultOut]


@router.get("/search")
def search_library(
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    q: Annotated[str, Query(description="the words to search for")],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SearchOut:
    """Study pages, labs and sources matching every word of `q`, best first."""
    found = search(session, settings, q, max_attempts=max_attempts, limit=limit)
    return SearchOut.model_validate(found.record())


__all__ = ["SearchOut", "router"]
