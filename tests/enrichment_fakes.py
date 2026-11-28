"""Builders for the enrichment tests (`teacher.exploration.enrichment`). Nothing here reaches the
network.

- `outline(key, name, step, prerequisites=(), aliases=())`: an outline concept for
  `completed_exploration`.
- `candidate(key, url, ...)`: a discovered candidate (as `record_discovery` takes it).
- `completed_exploration(settings, concepts, found, matched=..., owner=None)`: a completed
  exploration stored directly through the manager: `concepts` in study order, `found` maps a
  concept key to its candidates, `matched` maps a concept key to a graph concept id (exact).
- `enricher(...)`: an `Enricher` of fakes: a `FakeFetcher` of `pages`, a fake metadata extractor
  (or a real `MetadataExtractor` over a fake client), a `ConceptExtractor` and a `GraphMatcher`
  over fake Claude clients, sharing one fake embedder.
- `page(url, text)`: a fetched web page; `pdf(url, content)`: a fetched PDF.
- `cand(name, ...)`: a concept extraction candidate (for `FakeAnthropic.concepts_reply`).
- `approve(settings, exploration_id, *keys)`, `drop(settings, exploration_id, key)`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from teacher.concept_extraction import ConceptExtractor
from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.exploration import Enricher, GraphMatcher
from teacher.managers import ExplorationManager
from teacher.url_sources import KIND_PAPER, FetchedSource
from tests.discovery_fakes import FakeFetcher, web_page

MODEL = "claude-sonnet-5-5"


def outline(key: str, name: str, step: int, prerequisites: Sequence[str] = (),
            aliases: Sequence[str] = ()) -> dict[str, Any]:  # fmt: skip
    return {"key": key, "name": name, "step": step, "aliases": list(aliases),
            "summary": f"What {name} is.", "prerequisites": list(prerequisites),
            "search_query": f"{name} tutorial"}  # fmt: skip


def candidate(key: str, url: str, *, title: str | None = None, overall: float = 4.0,
              arxiv_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "candidate_key": key, "url": url, "canonical_urls": [url], "landing_url": None,
        "arxiv_id": arxiv_id, "doi": None, "title": title or f"About {key}",
        "authors": ["A. Writer"], "year": 2020, "summary": f"A text about {key}.",
        "found_by": [], "verdict": "readable", "verdict_reason": "readable",
        "probe": {"verdict": "readable"}, "library": [], "attempt": 1,
        "triage": {"overall": overall}, "overall": overall, "fits": True,
    }  # fmt: skip


def completed_exploration(
    settings: Settings,
    concepts: Sequence[dict[str, Any]],
    found: Mapping[str, Sequence[dict[str, Any]]] = {},
    *,
    matched: Mapping[str, int] = {},
    steps: Sequence[str] = ("Foundations", "Next", "Last"),
    owner: int | None = None,
) -> int:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=owner)
        row = m.create("Diffie-Hellman", "intro")
        used_steps = max(c["step"] for c in concepts)
        rows = m.store_outline(
            row, questions=[], steps=list(steps[:used_steps]),
            concepts=[{**c, "position": i} for i, c in enumerate(concepts, start=1)],
        )  # fmt: skip
        by_key = {r.key: r for r in rows}
        for r in rows:
            concept_id = matched.get(r.key)
            m.store_match(
                r, concept_id=concept_id, method="exact" if concept_id else "new",
                reason="exact name match" if concept_id else None, detail=None,
                coverage="thin" if concept_id else "new", needs_sources=True,
            )  # fmt: skip
        for key, candidates in found.items():
            m.record_discovery(row, by_key[key], searches=[], candidates=list(candidates),
                               log=[], error=None)  # fmt: skip
        m.complete(row)
        return row.id


def approve(settings: Settings, exploration_id: int, *keys: str, owner: int | None = None) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=owner)
        row = m.get(exploration_id)
        for c in m.candidates(row):
            if c.candidate_key in keys:
                m.decide(row, c, "approved")


def drop(settings: Settings, exploration_id: int, key: str) -> None:
    with session_scope(settings) as session:
        m = ExplorationManager(session, settings, owner=None)
        row = m.get(exploration_id)
        m.set_dropped(row, next(c for c in m.concepts(row) if c.key == key), True)


def page(url: str, text: str, *, title: str | None = None) -> FetchedSource:
    return web_page(url, text=text, title=title)


def pdf(url: str, content: bytes) -> FetchedSource:
    return FetchedSource(
        requested_url=url, url=url, kind=KIND_PAPER, media_type="application/pdf",
        suffix=".pdf", content=content, original_media_type="application/pdf",
    )  # fmt: skip


def cand(name: str, role: str = "introduces", aliases: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "name": name, "aliases": list(aliases), "summary": f"The idea of {name}.",
        "role": role, "evidence": None if role == "assumes" else "as the text says",
    }  # fmt: skip


class FakeMetadata:
    """A metadata extractor whose details are given; records the paths it read."""

    def __init__(self, **details: Any) -> None:
        self.details = details
        self.calls: list[str] = []

    def extract(self, path: Any, media_type: str | None) -> Any:
        from teacher.metadata import DetectedMetadata

        self.calls.append(str(path))
        values = {"title": None, "authors": (), "year": None, "arxiv_id": None, "doi": None,
                  "summary": None, "model": "fake-metadata"}  # fmt: skip
        values.update(self.details)
        return DetectedMetadata(**values)


def enricher(
    *,
    pages: Mapping[str, Any],
    extract_client: Any,
    judge_client: Any,
    embedder: Any,
    metadata: Any = None,
) -> tuple[Enricher, FakeFetcher]:
    fetcher = FakeFetcher(dict(pages))
    return (
        Enricher(
            fetcher=fetcher,
            metadata_extractor=metadata if metadata is not None else FakeMetadata(),
            concept_extractor=ConceptExtractor(extract_client, embedder=embedder, model=MODEL),
            matcher=GraphMatcher(judge_client, embedder=embedder, model=MODEL),
        ),
        fetcher,
    )


__all__ = [
    "MODEL",
    "FakeMetadata",
    "approve",
    "cand",
    "candidate",
    "completed_exploration",
    "drop",
    "enricher",
    "outline",
    "page",
    "pdf",
]
