"""How the knowledge sources relate to each other (`teacher.source_graph`; the rules are in its
docstring): `GET /api/sources/{slug}/related` for one source (builds on, built on by, related,
readiness; `analysed: false` with those `null` when the source has no concept links yet; cites
and cited by, with `cites` `null` until its references are stored) and
`GET /api/source-graph` for every source and edge at once. Reads only.

`/api/source-graph` is outside `/api/sources/...`, so no `/api/sources/{slug}` route can match
it whatever order the routers are included in."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from teacher.source_graph import source_graph, source_relations
from teacher.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["source-graph"])


@router.get("/sources/{slug}/related")
def get_source_relations(slug: str, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """The source's relations to the other sources (404 for an unknown slug)."""
    return source_relations(session, settings, slug)


@router.get("/source-graph")
def get_source_graph(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """Every source as a node (with `analysed` and its reference state) and every builds_on,
    cites and related edge."""
    return source_graph(session, settings)


__all__ = ["router"]
