"""Finding sources for a topic: `POST /api/sources/discover` (`teacher.discovery`).

Body: `{"topic": "<free text>", "level": "intro" | "intermediate" | "advanced"}` or
`{"concept": "<concept slug>", "level": ...}` (exactly one of `topic` and `concept`; `level`
defaults to "intermediate"). The reply is `DiscoveryResult.to_dict()`: the ranked candidates
with their reasons, probe verdicts, library matches and providers, every provider's status, and
the run's model calls with their cost. Nothing is stored or ingested: each candidate is added,
if the reader wants it, through `POST /api/sources/inspect-url` and `POST /api/sources/from-url`.

Errors: a bad body or topic 422, an unknown concept 404, no source finder in this app (or no
Anthropic credentials) 503, every provider failing or a triage reply that does not validate
502. The request takes as long as the search (typically a minute or two); no database session
is held open meanwhile. Every model call is recorded in the cost ledger with purpose
`source_discovery` (`teacher.costs`), as well as listed with its cost in the reply's `usage`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from teacher.costs import cost_scope
from teacher.discovery import (
    DEFAULT_LEVEL,
    LEDGER_PURPOSE,
    DiscoveryQuery,
    DiscoveryUnavailableError,
    concept_query,
    find_sources,
)
from teacher.web.deps import SettingsDep

router = APIRouter(prefix="/api", tags=["sources"])

NO_FINDER = (
    "Finding sources is not available in this app: it was started without a source finder."
)


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = None
    concept: str | None = None
    level: str = DEFAULT_LEVEL


@router.post("/sources/discover")
def discover_sources(request: Request, settings: SettingsDep, body: DiscoverIn) -> dict[str, Any]:
    """Find candidate sources for a topic or a concept (see the module docstring)."""
    topic = body.topic if body.topic is not None and body.topic.strip() else None
    concept = body.concept.strip() if body.concept is not None and body.concept.strip() else None
    if (topic is None) == (concept is None):
        raise ValueError("give either a topic or a concept to find sources for (not both)")
    finder = request.app.state.source_finder
    if finder is None:
        raise DiscoveryUnavailableError(NO_FINDER)
    if concept is not None:
        query = concept_query(settings, concept, body.level)
    else:
        query = DiscoveryQuery(topic, level=body.level)  # type: ignore[arg-type]
    with cost_scope(settings, purpose=LEDGER_PURPOSE):
        return find_sources(settings, finder, query).to_dict()


__all__ = ["DiscoverIn", "router"]
