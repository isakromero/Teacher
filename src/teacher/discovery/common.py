"""What every part of source discovery shares: the query, the reader levels, the errors and the
record of each model call with its cost.

Model calls and the cost ledger. Every Claude call of a discovery is recorded in the cost
ledger (`teacher.costs`, table `api_calls`) right after it returns, like every other API call of
the app: the call sites take the current meter before calling (`require_meter`: without one the
call is not sent) and record the reply with purpose `source_discovery` (unless an enclosing
scope names the job: an exploration's calls are `exploration` calls, `teacher.exploration`).
The result also carries each call priced when it returned (`ModelCall`, with
`teacher.pricing.cost`, the same table the ledger uses) and their total (`DiscoveryUsage`), so
a caller can show what one search cost. Web searches are billed per search on top of the
tokens; the price table has no rate for them, so they are counted (`web_search_requests`, also
stored on the ledger row) and never priced (`search_fees_priced` False), not counted as zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from teacher import pricing
from teacher.costs import anthropic_usage, web_search_requests
from teacher.pricing import TokenUsage

#: The reader levels a query can name, and what each tells the models.
LEVELS: dict[str, str] = {
    "intro": (
        "new to the field: needs intuition, worked examples and plain language first, and "
        "assumes no background beyond school mathematics"
    ),
    "intermediate": (
        "knows the basics of the field and its standard notation, and wants a thorough "
        "explanation with the reasoning behind it"
    ),
    "advanced": (
        "comfortable with the research literature: wants depth, precise definitions, proofs or "
        "the primary sources"
    ),
}
DEFAULT_LEVEL = "intermediate"
#: How the levels are named in the UI (the Add page's level choice).
LEVEL_LABELS: dict[str, str] = {
    "intro": "Introductory",
    "intermediate": "Intermediate",
    "advanced": "Advanced",
}
MAX_TOPIC_CHARS = 300
#: The cost-ledger purpose of a discovery's calls (`teacher.costs`), unless an enclosing scope
#: names the job (an exploration's are `exploration`).
LEDGER_PURPOSE = "source_discovery"
MAX_ALIASES = 8
MAX_SUMMARY_CHARS = 1500


class DiscoveryError(Exception):
    """Discovery failed; the message says why (HTTP 502). Never contains a secret. `calls` are
    the model calls made before it failed (their cost was spent; the message states it), and
    `providers` the providers' statuses when they had run (`service.ProviderStatus`: every
    provider failing, or triage failing after they ran), so a caller can record each of them."""

    def __init__(
        self, message: str, *, calls: Sequence[ModelCall] = (), providers: Sequence[Any] = ()
    ) -> None:
        super().__init__(message)
        self.calls = tuple(calls)
        self.providers = tuple(providers)


class DiscoveryUnavailableError(DiscoveryError):
    """Discovery cannot run here: no finder configured, no credentials (HTTP 503)."""


class ProviderError(DiscoveryError):
    """One provider failed. Caught per provider: the result shows it next to the others, with
    the calls it made before failing (`calls`)."""


class AllProvidersFailedError(DiscoveryError):
    """Every provider failed, so there is nothing to show (HTTP 502)."""


class TriageError(DiscoveryError):
    """The triage call failed or its reply did not validate (HTTP 502)."""


class TriageUnavailableError(TriageError, DiscoveryUnavailableError):
    """Triage cannot run: no Anthropic credentials (HTTP 503)."""


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


@dataclass(frozen=True)
class DiscoveryQuery:
    """What to find sources for: a free-text `topic` (or a concept's name), the concept's
    `aliases` and `summary` when it is one (`concept_slug`), and the reader `level` (a key of
    `LEVELS`). ValueError for an empty or over-long topic, an unknown level, or bad aliases."""

    topic: str
    level: str = DEFAULT_LEVEL
    aliases: tuple[str, ...] = ()
    summary: str | None = None
    concept_slug: str | None = None

    def __post_init__(self) -> None:
        topic = _clean(self.topic) if isinstance(self.topic, str) else None
        if topic is None:
            raise ValueError("the topic is empty: say what to find sources for")
        if len(topic) > MAX_TOPIC_CHARS:
            raise ValueError(f"the topic is longer than {MAX_TOPIC_CHARS} characters")
        if self.level not in LEVELS:
            raise ValueError(f"unknown level {self.level!r}; choose one of: {', '.join(LEVELS)}")
        if isinstance(self.aliases, str) or not all(isinstance(a, str) for a in self.aliases):
            raise ValueError("aliases must be a sequence of strings")
        aliases = tuple(
            dict.fromkeys(
                a for a in (_clean(a) for a in self.aliases) if a and a.lower() != topic.lower()
            )
        )[:MAX_ALIASES]
        summary = _clean(self.summary)
        if summary is not None and len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + " ..."
        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "summary", summary)

    @classmethod
    def for_concept(cls, concept: Any, level: str = DEFAULT_LEVEL) -> DiscoveryQuery:
        """The query for a `teacher.db.models.Concept` row (its name, aliases and summary).
        Read it inside the session that loaded it."""
        return cls(
            topic=concept.name,
            level=level,
            aliases=tuple(a.alias for a in concept.aliases),
            summary=concept.summary,
            concept_slug=concept.slug,
        )

    @property
    def terms(self) -> tuple[str, ...]:
        """The topic and its aliases: what the providers search for."""
        return (self.topic, *self.aliases)

    def describe(self) -> str:
        """The query as the prompts state it."""
        lines = [f"Topic: {self.topic}"]
        if self.aliases:
            lines.append(f"Also known as: {'; '.join(self.aliases)}")
        if self.summary:
            lines.append(f"What it is: {self.summary}")
        lines.append(f"Reader level: {self.level} (the reader is {LEVELS[self.level]})")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "level": self.level,
            "aliases": list(self.aliases),
            "summary": self.summary,
            "concept_slug": self.concept_slug,
        }


# -- model calls and their cost ---------------------------------------------------------------


@dataclass(frozen=True)
class ModelCall:
    """One Claude call of a discovery run, priced when it returned (see the module docstring).
    `cost_usd` is None exactly when the model has no known price; it never includes web search
    fees (`web_search_requests` counts the searches)."""

    step: str
    model: str
    usage: TokenUsage
    cost_usd: float | None
    priced_on: str | None
    upper_bound: bool
    web_search_requests: int = 0
    response_id: str | None = None

    @classmethod
    def from_message(cls, message: Any, *, step: str) -> ModelCall:
        model = str(message.model)
        usage = anthropic_usage(message)
        priced = pricing.cost(model, usage)
        return cls(
            step=step,
            model=model,
            usage=usage,
            cost_usd=priced.usd if priced is not None else None,
            priced_on=priced.checked_on if priced is not None else None,
            upper_bound=priced.upper_bound if priced is not None else False,
            web_search_requests=web_search_requests(message),
            response_id=getattr(message, "id", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "model": self.model,
            "input_tokens": self.usage.input_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": round(self.cost_usd, 6) if self.cost_usd is not None else None,
            "priced_on": self.priced_on,
            "upper_bound": self.upper_bound,
            "web_search_requests": self.web_search_requests,
            "response_id": self.response_id,
        }


@dataclass(frozen=True)
class DiscoveryUsage:
    """Every model call of a run and their total (see the module docstring). The same calls are
    in the cost ledger; `search_fees_priced` is always False (no rate for web searches)."""

    calls: tuple[ModelCall, ...] = ()
    search_fees_priced: bool = False

    @classmethod
    def of(cls, calls: Iterable[ModelCall]) -> DiscoveryUsage:
        return cls(calls=tuple(calls))

    @property
    def total_usd(self) -> float:
        """The known token cost of the calls (unpriced calls and search fees not included)."""
        return sum(c.cost_usd for c in self.calls if c.cost_usd is not None)

    @property
    def unpriced_models(self) -> tuple[str, ...]:
        return tuple(sorted({c.model for c in self.calls if c.cost_usd is None}))

    @property
    def web_search_requests(self) -> int:
        return sum(c.web_search_requests for c in self.calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_usd, 6),
            "calls": [c.to_dict() for c in self.calls],
            "unpriced_calls": sum(1 for c in self.calls if c.cost_usd is None),
            "unpriced_models": list(self.unpriced_models),
            "upper_bound": any(c.upper_bound for c in self.calls),
            "web_search_requests": self.web_search_requests,
            "search_fees_priced": self.search_fees_priced,
        }


def first_error(exc: Any) -> str:
    """'where: message' of a pydantic ValidationError's first error."""
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ())) or "reply"
    return f"{where}: {err.get('msg', 'invalid')}"


def stop_problem(message: Any, *, what: str, max_tokens: int) -> str | None:
    """Why a reply that should have ended normally did not, or None when it did."""
    stop = message.stop_reason
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        return f"Claude declined the {what} (stop_reason 'refusal', category {category})"
    if stop == "max_tokens":
        return f"Claude's {what} reply was cut off at max_tokens={max_tokens}"
    return None


__all__: Sequence[str] = [
    "DEFAULT_LEVEL",
    "LEDGER_PURPOSE",
    "LEVELS",
    "AllProvidersFailedError",
    "DiscoveryError",
    "DiscoveryQuery",
    "DiscoveryUnavailableError",
    "DiscoveryUsage",
    "ModelCall",
    "ProviderError",
    "TriageError",
    "TriageUnavailableError",
    "first_error",
    "stop_problem",
]
