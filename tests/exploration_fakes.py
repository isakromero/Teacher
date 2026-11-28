"""Fakes and builders for the exploration tests (`teacher.exploration`). Nothing here reaches the
network.

- `brief_reply(...)`, `outline_reply(steps, ...)`: Claude Messages for `Planner` (for the
  conftest's `FakeAnthropic`), from `concept(...)` and `step(...)` dicts.
- `TopicProvider(name, by_topic)`: a source provider whose hits (or error) depend on the
  query's topic; records the queries.
- `explorer(...)`: an `Explorer` from a fake Claude client, a fake embedder and a source
  finder of fakes.
- `graph(session, settings, embedder, *names)`: concepts in the graph, embedded.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from teacher.discovery import ProviderError, ProviderOutput, SourceFinder
from teacher.exploration import Explorer, GraphMatcher, Planner
from tests.discovery_fakes import FakeFetcher, FakeTriager, hit, web_page

MODEL = "claude-sonnet-5-5"


def brief_reply(fake: Any, **overrides: Any) -> Any:
    body = {
        "summary": "How two parties agree on a secret key in public, and why it is safe.",
        "in_scope": ["modular arithmetic for key exchange", "the Diffie-Hellman protocol"],
        "out_of_scope": ["elliptic curves"],
        "level_note": "Start from modular arithmetic; no proofs.",
        "assumed_known": ["school algebra"],
    }
    body.update(overrides)
    return fake.reply(text=json.dumps(body), model=MODEL)


def concept(name: str, *, aliases: Sequence[str] = (), prerequisites: Sequence[str] = (),
            summary: str | None = None, search_query: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "name": name,
        "aliases": list(aliases),
        "summary": summary or f"What {name} is.",
        "prerequisites": list(prerequisites),
        "search_query": search_query or f"{name} tutorial",
    }


def step(title: str, *concepts: dict[str, Any]) -> dict[str, Any]:
    return {"title": title, "concepts": list(concepts)}


PERSPECTIVES = [
    {"perspective": "a practitioner", "questions": ["How do I use it?"]},
    {"perspective": "a theorist", "questions": ["Why is it secure?", "What does it assume?"]},
]

DEFAULT_STEPS = [
    step("Foundations", concept("Modular arithmetic")),
    step("The protocol", concept("Diffie-Hellman key exchange", aliases=["DH"],
                                 prerequisites=["Modular arithmetic"])),  # fmt: skip
]


def outline_body(steps: Sequence[dict[str, Any]] | None = None, *,
                 perspectives: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "perspectives": list(PERSPECTIVES if perspectives is None else perspectives),
        "steps": list(DEFAULT_STEPS if steps is None else steps),
    }


def outline_reply(fake: Any, steps: Sequence[dict[str, Any]] | None = None, **kw: Any) -> Any:
    return fake.reply(text=json.dumps(outline_body(steps, **kw)), model=MODEL)


class TopicProvider:
    """Hits by the query's topic (`by_topic[topic]`: a list of hits, or a ProviderError);
    a topic it does not know gives no hits."""

    def __init__(self, name: str, by_topic: Mapping[str, Any]) -> None:
        self.name = name
        self.by_topic = dict(by_topic)
        self.queries: list[Any] = []
        self._lock = threading.Lock()

    def search(self, query: Any) -> ProviderOutput:
        with self._lock:
            self.queries.append(query)
        outcome = self.by_topic.get(query.topic, [])
        if isinstance(outcome, BaseException):
            raise outcome
        return ProviderOutput(hits=tuple(outcome), detail=f"fake {self.name}")

    @property
    def topics(self) -> list[str]:
        return [q.topic for q in self.queries]


def finder(
    by_topic: Mapping[str, Any],
    pages: Mapping[str, Any] | None = None,
    *,
    score: Callable[[Any], tuple[int, int, int]] = lambda item: (4, 4, 4),
    triager: Any = None,
) -> tuple[SourceFinder, TopicProvider]:
    """A SourceFinder with one `TopicProvider` ("web"), a fetcher of `pages` (default: a
    readable page for every hit's address) and a FakeTriager scoring with `score`."""
    provider = TopicProvider("web", by_topic)
    if pages is None:
        pages = {
            h.url: web_page(h.url)
            for hits in by_topic.values() if not isinstance(hits, BaseException)
            for h in hits
        }  # fmt: skip
    return (
        SourceFinder([provider], fetcher=FakeFetcher(pages), triager=triager or FakeTriager(score)),
        provider,
    )


def explorer(client: Any, embedder: Any, source_finder: Any) -> Explorer:
    return Explorer(
        Planner(client, model=MODEL),
        GraphMatcher(client, embedder=embedder, model=MODEL),
        source_finder,
    )


def graph(session: Any, settings: Any, embedder: Any, *names: str | tuple[str, Sequence[str]]) -> dict[str, Any]:
    """Concepts named `names` (or `(name, aliases)`), with up-to-date embeddings; by name."""
    from teacher.managers import ConceptEmbeddingManager, ConceptManager

    made = {}
    for item in names:
        name, aliases = (item, ()) if isinstance(item, str) else item
        made[name] = ConceptManager(session, settings).create(
            name, f"What {name} is.", "user", aliases=list(aliases)
        )
    session.flush()
    ConceptEmbeddingManager(session, settings).embed_stale(embedder)
    session.commit()
    return made


__all__ = [
    "MODEL",
    "PERSPECTIVES",
    "ProviderError",
    "TopicProvider",
    "brief_reply",
    "concept",
    "explorer",
    "finder",
    "graph",
    "hit",
    "outline_body",
    "outline_reply",
    "step",
]
