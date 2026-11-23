"""The review queue's pure rules (`teacher.review_schedule`): intervals, order, forms, option order."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from teacher.review_schedule import (
    FIRST_INTERVAL,
    FIRST_INTERVAL_CLEAN,
    GROWTH,
    MAX_INTERVAL,
    due_after,
    first_interval,
    form_for,
    interleave,
    is_confident_error,
    next_interval,
    option_order,
    priority,
    reschedule,
)

T0 = datetime(2026, 3, 1, 9, 0, 0)


def test_the_constants() -> None:
    assert (FIRST_INTERVAL, FIRST_INTERVAL_CLEAN, GROWTH, MAX_INTERVAL) == (1.0, 3.0, 2.2, 180.0)


def test_first_review_is_sooner_after_a_first_attempt_that_was_not_clean() -> None:
    assert first_interval(True) == 3.0
    assert first_interval(False) == 1.0
    assert due_after(T0, first_interval(True)) == T0 + timedelta(days=3)
    assert due_after(T0, 2.5) == T0 + timedelta(days=2, hours=12)


@pytest.mark.parametrize(
    ("interval", "verdict", "expected"),
    [
        (1.0, "passed", 2.2),
        (3.0, "passed", 6.6),
        (6.6, "passed", 14.52),
        (100.0, "passed", 180.0),  # capped
        (180.0, "passed", 180.0),
        (14.52, "failed", 1.0),  # a lapse starts over
        (1.0, "failed", 1.0),
        (6.6, "partial", 6.6),  # kept
    ],
)
def test_next_interval(interval: float, verdict: str, expected: float) -> None:
    assert next_interval(interval, verdict) == pytest.approx(expected)


def test_a_run_of_reviews_expands_and_resets() -> None:
    interval, at = first_interval(True), T0
    seen = []
    for verdict in ("passed", "passed", "partial", "failed", "passed"):
        at = at + timedelta(days=interval)  # answered exactly when due
        step = reschedule(interval, verdict, at)
        assert step.due_at == at + timedelta(days=step.interval_days)
        interval = step.interval_days
        seen.append(round(interval, 3))
    assert seen == [6.6, 14.52, 14.52, 1.0, 2.2]


def test_bad_schedule_input_is_an_error() -> None:
    with pytest.raises(ValueError, match="verdict"):
        next_interval(1.0, "maybe")
    for bad in (0, -1.0, True, "1"):
        with pytest.raises(ValueError, match="positive number of days"):
            next_interval(bad, "passed")  # type: ignore[arg-type]


def test_confident_errors_come_first_then_the_most_overdue() -> None:
    assert is_confident_error("failed", "high") and is_confident_error("partial", "high")
    assert not is_confident_error("passed", "high") and not is_confident_error("failed", "medium")
    assert not is_confident_error("failed", None)
    items = [
        ("a", False, T0 - timedelta(days=1)),
        ("b", True, T0),
        ("c", False, T0 - timedelta(days=5)),
        ("d", True, T0 - timedelta(days=2)),
    ]
    ordered = sorted(items, key=lambda i: priority(confident_error=i[1], due_at=i[2]))
    assert [name for name, *_ in ordered] == ["d", "b", "c", "a"]


def test_interleave_mixes_pages_whenever_it_can() -> None:
    items = [("p1", 1), ("p1", 2), ("p1", 3), ("p2", 4), ("p3", 5)]
    out = interleave(items, lambda i: i[0])
    assert out == [("p1", 1), ("p2", 4), ("p1", 2), ("p3", 5), ("p1", 3)]
    # the page of the last review counts as the previous one
    assert interleave(items, lambda i: i[0], previous="p1")[0] == ("p2", 4)
    # one page only: the order stays
    assert interleave(items[:3], lambda i: i[0], previous="p1") == items[:3]
    assert interleave([], lambda i: i) == []


def test_forms_alternate_between_a_variant_and_the_original() -> None:
    assert [form_for(n, 2) for n in range(6)] == [1, None, 2, None, 1, None]
    assert [form_for(n, 1) for n in range(4)] == [1, None, 1, None]
    assert [form_for(n, 0) for n in range(3)] == [None, None, None]
    with pytest.raises(ValueError):
        form_for(-1, 2)
    with pytest.raises(ValueError):
        form_for(0, -1)


def test_option_order_is_a_fresh_permutation() -> None:
    rng = random.Random(7)
    seen = set()
    previous = None
    for _ in range(30):
        order = option_order(4, rng, previous=previous)
        assert sorted(order) == [0, 1, 2, 3]
        assert order != previous
        seen.add(tuple(order))
        previous = order
    assert len(seen) > 5
    assert option_order(2, random.Random(1), previous=[0, 1]) == [1, 0]
    with pytest.raises(ValueError, match="at least 2 options"):
        option_order(1, rng)
