"""Answers per local day (`teacher.answer_days`), for the phone summary."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from teacher.answer_days import count_by_day, day_window, local_date, streak, zone_named

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@pytest.mark.parametrize("name", ["", "  ", "Mars/Olympus", "../etc", "UTC+2"])
def test_unknown_zones_are_errors(name: str) -> None:
    with pytest.raises(ValueError, match="IANA"):
        zone_named(name)


def test_known_zones() -> None:
    assert zone_named("Europe/Stockholm") == STOCKHOLM
    assert zone_named("UTC") == ZoneInfo("UTC")


def test_local_date_crosses_midnight() -> None:
    # 23:30 UTC on 26 Nov is 00:30 on 27 Nov in Stockholm (UTC+1 in winter)
    assert local_date(datetime(2026, 11, 26, 23, 30), STOCKHOLM) == date(2026, 11, 27)
    assert local_date(datetime(2026, 11, 26, 22, 59), STOCKHOLM) == date(2026, 11, 26)
    # and 03:00 UTC is still the day before in Los Angeles
    assert local_date(datetime(2026, 11, 27, 3, 0), ZoneInfo("America/Los_Angeles")) == date(
        2026, 11, 26
    )


def test_day_window() -> None:
    dates, start = day_window(datetime(2026, 11, 26, 12, 0), STOCKHOLM, 3)
    assert dates == [date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)]
    assert start == datetime(2026, 11, 23, 23, 0)  # midnight in Stockholm, as UTC
    for bad in (0, -1, True, 2.0):
        with pytest.raises(ValueError):
            day_window(datetime(2026, 11, 26), STOCKHOLM, bad)  # type: ignore[arg-type]


def test_day_window_over_a_dst_change() -> None:
    # Stockholm moved to summer time on 29 March 2026: the window starts at 23:00 UTC the day
    # before its first day in winter time
    dates, start = day_window(datetime(2026, 3, 30, 12, 0), STOCKHOLM, 3)
    assert dates[0] == date(2026, 3, 28) and start == datetime(2026, 3, 27, 23, 0)
    times = [datetime(2026, 3, 28, 22, 30), datetime(2026, 3, 29, 22, 30)]  # 23:30 CET, 00:30 CEST
    assert count_by_day(times, STOCKHOLM, dates) == [1, 0, 1]


def test_count_by_day_ignores_other_days() -> None:
    dates = [date(2026, 11, 25), date(2026, 11, 26)]
    times = [
        datetime(2026, 11, 24, 23, 0),  # 25 Nov 00:00 local
        datetime(2026, 11, 25, 22, 59),  # 25 Nov 23:59
        datetime(2026, 11, 25, 23, 0),  # 26 Nov 00:00
        datetime(2026, 11, 26, 23, 0),  # 27 Nov: not in the window
        datetime(2026, 11, 20, 12, 0),  # before it
    ]
    assert count_by_day(times, STOCKHOLM, dates) == [2, 1]


@pytest.mark.parametrize(
    ("totals", "expected"),
    [
        ([], 0),
        ([0, 0, 0], 0),
        ([1, 1, 1], 3),
        ([1, 0, 1], 1),
        ([2, 3, 0], 2),  # today not yet: the streak to yesterday still counts
        ([1, 0, 0], 0),
        ([5], 1),
    ],
)
def test_streak(totals: list[int], expected: int) -> None:
    assert streak(totals) == expected
