"""Answers per local day, for the phone summary (`teacher.web.routers.phone`): pure functions,
no database.

A day is a calendar date in the reader's time zone. `day_window` gives the last `days` dates up
to and including today and the instant the first of them began; `count_by_day` puts stored
times (naive UTC) on those dates. `streak` is the number of consecutive days with at least one
answer that ends today, or yesterday when today has none yet (a streak is not broken before the
day is over).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone_named(name: str) -> ZoneInfo:
    """The IANA time zone `name`; ValueError (a 422 in the API) for anything else."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tz must be an IANA time zone name such as Europe/Stockholm")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(
            f"unknown time zone {name!r}: give an IANA name such as Europe/Stockholm"
        ) from err


def local_date(at: datetime, zone: ZoneInfo) -> date:
    """The date in `zone` of `at` (naive UTC)."""
    return at.replace(tzinfo=UTC).astimezone(zone).date()


def day_window(now: datetime, zone: ZoneInfo, days: int) -> tuple[list[date], datetime]:
    """The last `days` dates in `zone` up to and including the one of `now` (naive UTC),
    oldest first, and when the first of them began (naive UTC)."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    today = local_date(now, zone)
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]
    start = datetime.combine(dates[0], time.min, tzinfo=zone).astimezone(UTC).replace(tzinfo=None)
    return dates, start


def count_by_day(times: Iterable[datetime], zone: ZoneInfo, dates: Sequence[date]) -> list[int]:
    """How many of `times` (naive UTC) fall on each of `dates` in `zone`; times on other
    dates are not counted."""
    index = {d: i for i, d in enumerate(dates)}
    counts = [0] * len(dates)
    for at in times:
        i = index.get(local_date(at, zone))
        if i is not None:
            counts[i] += 1
    return counts


def streak(totals: Sequence[int]) -> int:
    """Consecutive days with answers ending on the last day of `totals` (today), or on the day
    before it when today has none (see the module docstring)."""
    days = list(totals)
    if days and days[-1] == 0:
        days.pop()
    count = 0
    for total in reversed(days):
        if total <= 0:
            break
        count += 1
    return count


__all__ = ["count_by_day", "day_window", "local_date", "streak", "zone_named"]
