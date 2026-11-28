"""Fakes for the source discovery tests (`teacher.discovery`): providers, a fetcher, a triager
and Claude replies. Nothing here reaches the network.

- `FakeProvider(name, hits=..., error=..., delay=...)`: returns `hits` (or raises `error`).
- `FakeFetcher({url: FetchedSource | UrlSourceError})`: `fetch(url)` returns or raises what is
  mapped (an unmapped address is a `fetch_failed` error); records `calls`.
- `FakeTriager(score=...)`: scores every item with `score(item) -> (fit, level, teaching)`.
- `web_page(url, words)`, `hit(...)`, `model_call(...)`: small builders.
- `web_reply(sources, ...)`: a Claude Message as the web search call returns it (a search, its
  results, and the `submit_sources` call), for `FakeAnthropic`.
- `triage_reply(scores)`: a structured-output triage Message.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from anthropic.types import Message

from teacher.discovery import (
    ModelCall,
    ProviderError,
    ProviderHit,
    ProviderOutput,
    TriageItem,
    TriageOutcome,
    TriageScore,
)
from teacher.pricing import TokenUsage
from teacher.url_sources import KIND_WEB_PAGE, FetchedSource, UrlSourceError


def model_call(step: str = "web_search", *, cost: float | None = 0.01, searches: int = 0,
               model: str = "claude-sonnet-5-5") -> ModelCall:  # fmt: skip
    return ModelCall(
        step=step,
        model=model,
        usage=TokenUsage(input_tokens=100, output_tokens=10),
        cost_usd=cost,
        priced_on="2026-11-24" if cost is not None else None,
        upper_bound=False,
        web_search_requests=searches,
    )


def hit(provider: str, rank: int, url: str, title: str | None = None, **kw: Any) -> ProviderHit:
    return ProviderHit(
        provider=provider,
        rank=rank,
        url=url,
        title=title or f"Title of {url}",
        reason=kw.pop("reason", f"{provider} found it"),
        **kw,
    )


class FakeProvider:
    def __init__(
        self,
        name: str,
        hits: Sequence[ProviderHit] = (),
        *,
        error: ProviderError | None = None,
        delay: float = 0.0,
        calls: Sequence[ModelCall] = (),
    ) -> None:
        self.name = name
        self.hits = tuple(hits)
        self.error = error
        self.delay = delay
        self.calls = tuple(calls)
        self.queries: list[Any] = []

    def search(self, query: Any) -> ProviderOutput:
        self.queries.append(query)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ProviderOutput(hits=self.hits, calls=self.calls, detail=f"fake {self.name}")


def web_page(url: str, words: int = 800, *, title: str | None = None, text: str | None = None) -> FetchedSource:
    from teacher.url_sources import PageDetails

    body = text if text is not None else " ".join(f"word{n}" for n in range(words))
    return FetchedSource(
        requested_url=url,
        url=url,
        kind=KIND_WEB_PAGE,
        media_type="text/markdown",
        suffix=".md",
        content=(body + "\n").encode("utf-8"),
        original_media_type="text/html",
        details=PageDetails(title=title),
        words=len(body.split()),
    )


class FakeFetcher:
    def __init__(self, pages: Mapping[str, FetchedSource | UrlSourceError], *, delay: float = 0.0) -> None:
        self.pages = dict(pages)
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def fetch(self, url: str) -> FetchedSource:
        with self._lock:
            self.calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            outcome = self.pages.get(url) or UrlSourceError(
                f"{url} could not be fetched (not in the fake)", reason="fetch_failed"
            )
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        finally:
            with self._lock:
                self.active -= 1


class FakeTriager:
    def __init__(
        self,
        score: Callable[[TriageItem], tuple[int, int, int]] = lambda item: (4, 4, 4),
        *,
        error: Exception | None = None,
    ) -> None:
        self.score = score
        self.error = error
        self.seen: list[list[TriageItem]] = []

    def triage(self, query: Any, items: Sequence[TriageItem]) -> TriageOutcome:
        self.seen.append(list(items))
        if self.error is not None:
            raise self.error
        scores = {}
        for item in items:
            fit, level, teaching = self.score(item)
            scores[item.label] = TriageScore(
                fit, level, teaching, "teaches", f"judged {item.url}", "claude-haiku-5-5"
            )
        return TriageOutcome(scores=scores, call=model_call("triage", model="claude-haiku-5-5"))


# -- Claude replies -----------------------------------------------------------------------------


def _message(content: list[dict[str, Any]], *, stop_reason: str, model: str, searches: int = 0,
             stop_details: dict[str, Any] | None = None) -> Message:  # fmt: skip
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_details": stop_details,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 2000,
                "output_tokens": 300,
                "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": 0},
            },
        }
    )


def web_reply(
    sources: Sequence[Mapping[str, Any]] | None = None,
    *,
    submit: Any = "default",
    search_error: str | None = None,
    stop_reason: str = "tool_use",
    searches: int = 1,
    extra_tool: str | None = None,
    model: str = "claude-sonnet-5-5",
    stop_details: dict[str, Any] | None = None,
) -> Message:
    """A web search reply: one search with its results (or an error), then `submit_sources`
    with `sources` (or exactly the input `submit`, or no call when `submit` is None)."""
    content: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
    ]
    if search_error is not None:
        result: Any = {"type": "web_search_tool_result_error", "error_code": search_error}
    else:
        result = [
            {"type": "web_search_result", "url": s["url"], "title": s.get("title", ""),
             "encrypted_content": "enc", "page_age": None}
            for s in (sources or [])
        ]  # fmt: skip
    content.append({"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": result})
    if extra_tool is not None:
        content.append({"type": "tool_use", "id": "toolu_x", "name": extra_tool, "input": {}})
    if submit == "default":
        submit = {"sources": [dict(s) for s in (sources or [])]}
    if submit is not None:
        content.append({"type": "tool_use", "id": "toolu_1", "name": "submit_sources", "input": submit})
    return _message(content, stop_reason=stop_reason, model=model, searches=searches,
                    stop_details=stop_details)  # fmt: skip


def paused_reply(*, searches: int = 3) -> Message:
    return _message(
        [{"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_search", "input": {"query": "q"}}],
        stop_reason="pause_turn",
        model="claude-sonnet-5-5",
        searches=searches,
    )


def triage_reply(scores: Any, *, stop_reason: str = "end_turn", text: str | None = None) -> Message:
    body = text if text is not None else json.dumps({"scores": scores})
    return _message(
        [{"type": "text", "text": body}], stop_reason=stop_reason, model="claude-haiku-5-5"
    )


def score(label: str, fit: int = 4, level: int = 4, teaching: int = 4, *,
          treatment: str = "teaches", reason: str = "Explains it well.") -> dict[str, Any]:  # fmt: skip
    return {"candidate": label, "topic_fit": fit, "level_match": level, "teaching": teaching,
            "treatment": treatment, "reason": reason}  # fmt: skip
