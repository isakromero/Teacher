"""The review queue's schedule, order and form rotation: pure functions, no database.

Schedule (expanding intervals, in days):
- A cleared question's first review is due `FIRST_INTERVAL_CLEAN` days after it was cleared on
  the page when its first attempt there was a clean pass, else `FIRST_INTERVAL` days after
  (a partial, a failure, or a revealed answer: it needs to come back sooner).
- Each review is one attempt, graded like the page (no retry loop). A passed review multiplies
  the interval by `GROWTH` (capped at `MAX_INTERVAL`); a failed one resets it to
  `FIRST_INTERVAL`; a partial one (a written answer that met some required criteria) keeps it.
  The next review is due that interval after the review, wherever the review fell (early or
  late).

Order of the due items (`order_due`): confident errors first (the last answer, on the page or
in a review, was not a pass although the reader said "Certain", i.e. confidence "high"), then
the most overdue; then interleaved (`interleave`) so that two items in a row come from
different study pages whenever another page has a due item left.

Forms (`form_for`): with a bank of `n` variants, reviews alternate between a variant and the
original, starting with a variant (the original was just answered on the page): review 0 asks
variant 1, review 1 the original, review 2 variant 2, ... cycling through the variants. With no
variants every review asks the original.

Option order (`option_order`): a multiple-choice question's options are shown in a fresh random
order each time it is asked; the order is a permutation of the form's option indexes (shown
position -> option index), different from the previous one whenever the options allow.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

FIRST_INTERVAL = 1.0  # days: after a first attempt that was not a clean pass, and after a lapse
FIRST_INTERVAL_CLEAN = 3.0  # days: after a clean first pass on the page
GROWTH = 2.2  # a passed review multiplies the interval by this
MAX_INTERVAL = 180.0  # days

T = TypeVar("T")

_VERDICTS = ("passed", "partial", "failed")


def _check_verdict(verdict: str) -> str:
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be one of {list(_VERDICTS)}, got {verdict!r}")
    return verdict


def _check_interval(interval: float) -> float:
    if isinstance(interval, bool) or not isinstance(interval, int | float) or not interval > 0:
        raise ValueError(f"an interval is a positive number of days, got {interval!r}")
    return float(interval)


def first_interval(first_clean: bool) -> float:
    """Days from clearing a question on the page to its first review."""
    return FIRST_INTERVAL_CLEAN if first_clean else FIRST_INTERVAL


def next_interval(interval: float, verdict: str) -> float:
    """The interval after a review with `verdict`, given the interval before it."""
    interval = _check_interval(interval)
    verdict = _check_verdict(verdict)
    if verdict == "passed":
        return min(interval * GROWTH, MAX_INTERVAL)
    if verdict == "failed":
        return FIRST_INTERVAL
    return interval  # partial: neither grown nor reset


def due_after(at: datetime, interval: float) -> datetime:
    """When an item answered (or cleared) at `at` with `interval` is next due."""
    return at + timedelta(days=_check_interval(interval))


@dataclass(frozen=True, slots=True)
class Reschedule:
    """The schedule after one review."""

    interval_days: float
    due_at: datetime


def reschedule(interval: float, verdict: str, at: datetime) -> Reschedule:
    """The new interval and due time after a review with `verdict` at `at`."""
    new = next_interval(interval, verdict)
    return Reschedule(new, due_after(at, new))


def is_confident_error(verdict: str, confidence: str | None) -> bool:
    """A wrong (or partly right) answer given with confidence "high" ("Certain")."""
    return _check_verdict(verdict) != "passed" and confidence == "high"


def priority(*, confident_error: bool, due_at: datetime) -> tuple[bool, datetime]:
    """Sort key of a due item: confident errors first, then the most overdue."""
    return (not confident_error, due_at)


def interleave(
    items: Sequence[T], page_of: Callable[[T], Hashable], *, previous: Hashable | None = None
) -> list[T]:
    """`items` (already in priority order) reordered so that no two neighbours share a page
    when it can be helped: each step takes the highest-priority item whose page differs from
    the page just taken (the first step from `previous`, the page of the last review), or the
    highest-priority item left when every remaining one is on that page."""
    left = list(items)
    out: list[T] = []
    last = previous
    while left:
        index = next((i for i, item in enumerate(left) if page_of(item) != last), 0)
        item = left.pop(index)
        out.append(item)
        last = page_of(item)
    return out


def form_for(reviews_done: int, variants: int) -> int | None:
    """Which form review number `reviews_done` (0 for the first review) asks: a variant's
    position (1-based) or None for the original."""
    if isinstance(reviews_done, bool) or not isinstance(reviews_done, int) or reviews_done < 0:
        raise ValueError(f"reviews_done must be a non-negative int, got {reviews_done!r}")
    if isinstance(variants, bool) or not isinstance(variants, int) or variants < 0:
        raise ValueError(f"variants must be a non-negative int, got {variants!r}")
    if variants == 0 or reviews_done % 2 == 1:
        return None
    return (reviews_done // 2) % variants + 1


def option_order(
    count: int, rng: random.Random, *, previous: Sequence[int] | None = None
) -> list[int]:
    """A random permutation of `range(count)` (shown position -> option index), different from
    `previous` whenever `count` > 1."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValueError(f"a multiple-choice question has at least 2 options, got {count!r}")
    order = list(range(count))
    for _ in range(100):
        rng.shuffle(order)
        if previous is None or list(previous) != order:
            return order
    raise RuntimeError("could not draw an option order different from the previous one")


__all__ = [
    "FIRST_INTERVAL",
    "FIRST_INTERVAL_CLEAN",
    "GROWTH",
    "MAX_INTERVAL",
    "Reschedule",
    "due_after",
    "first_interval",
    "form_for",
    "interleave",
    "is_confident_error",
    "next_interval",
    "option_order",
    "priority",
    "reschedule",
]
