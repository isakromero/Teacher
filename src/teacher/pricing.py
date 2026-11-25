"""What a model call costs in US dollars: a price table and a pure function over it.

The ledger (`teacher.costs`) prices every call when it is recorded and stores the figure, so a
stored cost never moves when this table changes; the table only decides the cost of calls made
from now on (and of past calls the backfill adds, `teacher.cost_backfill`).

The table (`PRICES`) has one entry per model, in US dollars per million tokens, each with the
date it was checked and where:

- Claude (`anthropic`): fresh input, cache writes at the 5-minute TTL, cache writes at the
  1-hour TTL, cache reads, and output. Source: the pricing reference of the bundled
  `claude-api` skill (Claude Code 2.1.328; model table cached 2026-11-03), read on 2026-11-24:
  Claude Fable 5.2 in `shared/models.md`, Claude Opus 5.5, Claude Sonnet 5.5 and Claude Haiku
  5.5 in `shared/model-migration.md` ("Migrating to ..." -> Pricing). These are first-party
  Claude API rates (no batch, no fast mode, no data-residency surcharge: the app uses none of
  them).
  Claude Haiku 5.5 has two rate cards, chosen by the length of the prompt (fresh input plus
  cache writes plus cache reads): $0.10 / $0.50 when the prompt is at most 100,000 tokens and
  $0.50 / $2.50 above that, with cache reads at 0.1x, 5-minute writes at 1.25x and 1-hour
  writes at 2x the input rate of the card that applies (`LONG_PROMPT_TOKENS`).
- OpenAI embeddings (`openai`): input only. Source: https://developers.openai.com/api/docs/pricing
  (the "Specialized models" table: text-embedding-3-small, $0.02 input), read on 2026-11-24.

Cache-write TTL. Writes at the 1-hour TTL cost more than writes at the 5-minute TTL, so a
call's usage says how many of its cache-write tokens were at the 1-hour TTL
(`TokenUsage.cache_write_1h_tokens`, from the API's `usage.cache_creation`). What the app
sends: the study-page generator, the page checks and the concept extractor mark the source
document with the default 5-minute TTL; the tutor marks the document with the 1-hour TTL and
the growing history with the 5-minute TTL (`teacher.tutor`); the other calls set no cache
breakpoint at all. When the split is unknown (`cache_write_1h_tokens` is None, e.g. a usage
without `cache_creation`) and there were cache writes, every write is priced at the 1-hour
rate and the cost is marked as an upper bound (`Cost.upper_bound`).

`upper_bound_cost(model, input, output, cache_ttl=...)` is for past calls of which only the
combined input count was kept: every input token at the dearest rate the request could have
been billed at (see its docstring); the backfill stores that figure marked as an estimate.

`cost(model, usage)` returns None for a model that is not in the table: the caller records the
call with its tokens and no cost, and every total that includes it says so (never a silent 0).
Models used by the app: the configured defaults (README "Configuration": claude-sonnet-5-5,
claude-opus-5-5, claude-haiku-5-5, text-embedding-3-small) and the models a page can be
generated with in the UI (`teacher.config.GENERATION_MODEL_CHOICES`, which adds
claude-fable-5-2). A model configured through an environment variable that is not listed here
is recorded unpriced.
"""

from __future__ import annotations

from dataclasses import dataclass

ANTHROPIC = "anthropic"
OPENAI = "openai"
PROVIDERS = (ANTHROPIC, OPENAI)

PER = 1_000_000  # rates are per million tokens
LONG_PROMPT_TOKENS = 100_000  # Claude Haiku 5.5: above this many prompt tokens, the long card

CLAUDE_SKILL_SOURCE = (
    "bundled claude-api skill (Claude Code 2.1.328), shared/model-migration.md and "
    "shared/models.md"
)
OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
CHECKED_ON = "2026-11-24"


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens of each kind."""

    input: float
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0


@dataclass(frozen=True)
class ModelPrice:
    """One model's prices: `rates`, and `long_prompt` rates for prompts of more than
    `LONG_PROMPT_TOKENS` tokens where the model has a second card; when and where checked."""

    provider: str
    rates: Rates
    checked_on: str
    source: str
    long_prompt: Rates | None = None


@dataclass(frozen=True)
class TokenUsage:
    """The token breakdown of one call. `input_tokens` is fresh (uncached) input only;
    `cache_write_tokens` all cache writes, of which `cache_write_1h_tokens` at the 1-hour TTL
    (None: the split is unknown); `cache_read_tokens` input read from the cache."""

    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int | None = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens, self.cache_write_tokens, self.cache_read_tokens, self.output_tokens
        )
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in counts):
            raise ValueError(f"token counts must be non-negative integers, got {self}")
        split = self.cache_write_1h_tokens
        if split is not None and (
            not isinstance(split, int) or not 0 <= split <= self.cache_write_tokens
        ):
            raise ValueError(
                f"cache_write_1h_tokens must be between 0 and cache_write_tokens, got {self}"
            )

    @property
    def prompt_tokens(self) -> int:
        """The whole prompt: fresh input, cache writes and cache reads."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


@dataclass(frozen=True)
class Cost:
    """A priced call: `usd`, the date the price was checked, and whether `usd` is an upper
    bound (the cache-write TTL split was unknown and every write was priced at 1 hour)."""

    usd: float
    checked_on: str
    upper_bound: bool = False


def _claude(
    input: float, output: float, *, write_5m: float, write_1h: float, read: float
) -> Rates:
    return Rates(
        input=input, cache_write_5m=write_5m, cache_write_1h=write_1h, cache_read=read,
        output=output,
    )


PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-2": ModelPrice(
        ANTHROPIC,
        _claude(8.00, 40.00, write_5m=10.00, write_1h=16.00, read=0.20),
        CHECKED_ON,
        CLAUDE_SKILL_SOURCE,
    ),
    "claude-opus-5-5": ModelPrice(
        ANTHROPIC,
        _claude(4.00, 20.00, write_5m=5.00, write_1h=8.00, read=0.20),
        CHECKED_ON,
        CLAUDE_SKILL_SOURCE,
    ),
    "claude-sonnet-5-5": ModelPrice(
        ANTHROPIC,
        _claude(2.00, 10.00, write_5m=2.50, write_1h=4.00, read=0.10),
        CHECKED_ON,
        CLAUDE_SKILL_SOURCE,
    ),
    "claude-haiku-5-5": ModelPrice(
        ANTHROPIC,
        _claude(0.10, 0.50, write_5m=0.125, write_1h=0.20, read=0.01),
        CHECKED_ON,
        CLAUDE_SKILL_SOURCE,
        long_prompt=_claude(0.50, 2.50, write_5m=0.625, write_1h=1.00, read=0.05),
    ),
    "text-embedding-3-small": ModelPrice(
        OPENAI, Rates(input=0.02), CHECKED_ON, OPENAI_PRICING_SOURCE
    ),
}


def price_of(model: str) -> ModelPrice | None:
    """The table entry for `model`, or None when it has no known price."""
    return PRICES.get(model)


def cost(model: str, usage: TokenUsage) -> Cost | None:
    """What a call to `model` with `usage` cost, or None for a model not in `PRICES`.

    Pure: the same arguments always give the same figure. ValueError when `usage` has token
    kinds the model is not billed for (an embedding with output or cache tokens)."""
    price = PRICES.get(model)
    if price is None:
        return None
    rates = price.rates
    if price.long_prompt is not None and usage.prompt_tokens > LONG_PROMPT_TOKENS:
        rates = price.long_prompt
    if price.provider == OPENAI and (
        usage.cache_write_tokens or usage.cache_read_tokens or usage.output_tokens
    ):
        raise ValueError(f"{model} is billed for input tokens only, got {usage}")
    writes_1h = usage.cache_write_1h_tokens
    upper_bound = writes_1h is None and usage.cache_write_tokens > 0
    if writes_1h is None:
        writes_1h = usage.cache_write_tokens  # unknown split: all at the dearer 1-hour rate
    writes_5m = usage.cache_write_tokens - writes_1h
    usd = (
        usage.input_tokens * rates.input
        + writes_5m * rates.cache_write_5m
        + writes_1h * rates.cache_write_1h
        + usage.cache_read_tokens * rates.cache_read
        + usage.output_tokens * rates.output
    ) / PER
    return Cost(usd=usd, checked_on=price.checked_on, upper_bound=upper_bound)


CACHE_TTLS = (None, "5m", "1h")


def upper_bound_cost(
    model: str, input_tokens: int, output_tokens: int, *, cache_ttl: str | None
) -> Cost | None:
    """The most a call can have cost when only its combined input count is known (fresh input,
    cache writes and cache reads added together, as the app recorded tokens before the ledger).

    Pricing all of it as fresh input is not an upper bound: a cache write costs more than fresh
    input (1.25x at 5 minutes, 2x at 1 hour). So the bound prices every input token at the
    dearest rate the request could have been billed at: the cache-write rate of the longest TTL
    it set (`cache_ttl` "5m" or "1h"), or the fresh-input rate for a request that set no cache
    breakpoint (`cache_ttl` None), for which the figure is then exact rather than a bound
    (`Cost.upper_bound` False). None for a model not in `PRICES`."""
    if cache_ttl not in CACHE_TTLS:
        raise ValueError(f"cache_ttl must be one of {CACHE_TTLS}, got {cache_ttl!r}")
    if cache_ttl is None:
        return cost(model, TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens))
    priced = cost(
        model,
        TokenUsage(
            cache_write_tokens=input_tokens,
            cache_write_1h_tokens=input_tokens if cache_ttl == "1h" else 0,
            output_tokens=output_tokens,
        ),
    )
    if priced is None:
        return None
    return Cost(usd=priced.usd, checked_on=priced.checked_on, upper_bound=True)


__all__ = [
    "ANTHROPIC",
    "CHECKED_ON",
    "LONG_PROMPT_TOKENS",
    "OPENAI",
    "PRICES",
    "PROVIDERS",
    "Cost",
    "ModelPrice",
    "Rates",
    "TokenUsage",
    "cost",
    "price_of",
    "upper_bound_cost",
]
