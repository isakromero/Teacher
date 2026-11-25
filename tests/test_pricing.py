"""The price table and the pure cost function (`teacher.pricing`)."""

from __future__ import annotations

import pytest

from teacher.config import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_GENERATION_MODEL,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_METADATA_MODEL,
    DEFAULT_TUTOR_MODEL,
    GENERATION_MODEL_CHOICES,
)
from teacher.pricing import (
    LONG_PROMPT_TOKENS,
    PRICES,
    TokenUsage,
    cost,
    upper_bound_cost,
)

M = 1_000_000


def test_every_model_the_app_uses_or_offers_has_a_price() -> None:
    used = {
        DEFAULT_GENERATION_MODEL, DEFAULT_JUDGE_MODEL, DEFAULT_METADATA_MODEL,
        DEFAULT_TUTOR_MODEL, DEFAULT_EMBEDDING_MODEL,
        *(model for model, _label in GENERATION_MODEL_CHOICES),
    }
    assert used <= set(PRICES)
    for price in PRICES.values():
        assert price.checked_on == "2026-11-24" and price.source


@pytest.mark.parametrize(
    ("model", "rates"),
    [  # input, 5-minute write, 1-hour write, read, output: dollars per million tokens
        ("claude-fable-5-2", (8.00, 10.00, 16.00, 0.20, 40.00)),
        ("claude-opus-5-5", (4.00, 5.00, 8.00, 0.20, 20.00)),
        ("claude-sonnet-5-5", (2.00, 2.50, 4.00, 0.10, 10.00)),
        ("claude-haiku-5-5", (0.10, 0.125, 0.20, 0.01, 0.50)),
    ],
)
def test_each_token_kind_has_its_own_rate(model: str, rates: tuple[float, ...]) -> None:
    fresh, write_5m, write_1h, read, out = rates
    n = 100_000  # a prompt of at most LONG_PROMPT_TOKENS: Haiku's short-prompt card
    kinds = {
        fresh: TokenUsage(input_tokens=n),
        write_5m: TokenUsage(cache_write_tokens=n, cache_write_1h_tokens=0),
        write_1h: TokenUsage(cache_write_tokens=n, cache_write_1h_tokens=n),
        read: TokenUsage(cache_read_tokens=n),
        out: TokenUsage(output_tokens=n),
    }
    for rate, usage in kinds.items():
        priced = cost(model, usage)
        assert priced.usd == pytest.approx(rate * n / M), (rate, usage)
        assert not priced.upper_bound


def test_a_breakdown_adds_up() -> None:
    usage = TokenUsage(
        input_tokens=556, cache_write_tokens=40_399, cache_write_1h_tokens=0,
        cache_read_tokens=1_000, output_tokens=5_017,
    )
    expected = (556 * 4 + 40_399 * 5 + 1_000 * 0.20 + 5_017 * 20) / M
    assert cost("claude-opus-5-5", usage).usd == pytest.approx(expected)
    # The 1-hour share of the writes is priced at its own rate.
    split = TokenUsage(cache_write_tokens=1_000, cache_write_1h_tokens=400)
    assert cost("claude-opus-5-5", split).usd == pytest.approx((600 * 5 + 400 * 8) / M)


def test_haiku_has_a_dearer_card_for_long_prompts() -> None:
    short = TokenUsage(input_tokens=LONG_PROMPT_TOKENS, output_tokens=1_000)
    long = TokenUsage(input_tokens=LONG_PROMPT_TOKENS - 10, cache_read_tokens=20,
                      output_tokens=1_000)
    assert cost("claude-haiku-5-5", short).usd == pytest.approx(
        (LONG_PROMPT_TOKENS * 0.10 + 1_000 * 0.50) / M
    )
    # The prompt is fresh input plus cache writes plus cache reads: over the threshold here.
    assert cost("claude-haiku-5-5", long).usd == pytest.approx(
        ((LONG_PROMPT_TOKENS - 10) * 0.50 + 20 * 0.05 + 1_000 * 2.50) / M
    )
    # The other models have one card.
    big = TokenUsage(input_tokens=500_000)
    assert cost("claude-sonnet-5-5", big).usd == pytest.approx(1.0)


def test_unknown_split_of_cache_writes_is_priced_as_an_upper_bound() -> None:
    usage = TokenUsage(cache_write_tokens=1_000, cache_write_1h_tokens=None, output_tokens=10)
    priced = cost("claude-opus-5-5", usage)
    assert priced.upper_bound
    assert priced.usd == pytest.approx((1_000 * 8 + 10 * 20) / M)
    # Without cache writes the split does not matter.
    assert not cost("claude-opus-5-5", TokenUsage(input_tokens=5, cache_write_1h_tokens=None)).upper_bound


def test_embeddings_are_input_only() -> None:
    assert cost("text-embedding-3-small", TokenUsage(input_tokens=M)).usd == pytest.approx(0.02)
    with pytest.raises(ValueError, match="input tokens only"):
        cost("text-embedding-3-small", TokenUsage(input_tokens=10, output_tokens=1))


def test_an_unknown_model_has_no_cost() -> None:
    assert cost("claude-unknown-9", TokenUsage(input_tokens=10)) is None
    assert upper_bound_cost("claude-unknown-9", 10, 1, cache_ttl="5m") is None


def test_token_counts_are_checked() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TokenUsage(input_tokens=-1)
    with pytest.raises(ValueError, match="between 0 and cache_write_tokens"):
        TokenUsage(cache_write_tokens=5, cache_write_1h_tokens=6)


def test_upper_bound_of_combined_input() -> None:
    # No cache breakpoint: the combined count is all fresh input, so the figure is exact.
    exact = upper_bound_cost("claude-haiku-5-5", 2_000, 300, cache_ttl=None)
    assert not exact.upper_bound
    assert exact.usd == pytest.approx((2_000 * 0.10 + 300 * 0.50) / M)
    # With a breakpoint every input token is priced at the cache-write rate of its TTL: dearer
    # than fresh input, which a cache write costs more than.
    five = upper_bound_cost("claude-sonnet-5-5", 1_000, 100, cache_ttl="5m")
    hour = upper_bound_cost("claude-opus-5-5", 239_208, 1_228, cache_ttl="1h")
    assert five.upper_bound and hour.upper_bound
    assert five.usd == pytest.approx((1_000 * 2.50 + 100 * 10) / M)
    assert hour.usd == pytest.approx((239_208 * 8 + 1_228 * 20) / M)
    assert five.usd > cost("claude-sonnet-5-5", TokenUsage(input_tokens=1_000, output_tokens=100)).usd
    with pytest.raises(ValueError, match="cache_ttl"):
        upper_bound_cost("claude-opus-5-5", 1, 1, cache_ttl="2h")
