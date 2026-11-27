"""The phone's summary: one cheap read for a home-screen widget, reminder scheduling and a daily
goal or streak.

GET /api/phone/summary?tz=<IANA time zone>
==========================================

Under the account gate like every /api route (401 without a session). Read-only for the
reader's data (it does create the review items of newly cleared questions, as every read of the
review queue does, so that its count agrees with the queue's). `tz` is required: missing,
blank or not an IANA zone name is a 422 (never a fallback to UTC). Time is `app.state.clock`
(as the review queue's). Answers, reviews and history are shared by every account (as
everywhere else but highlights), so the counts are the app's, whoever asks. Timestamps are ISO
8601 in UTC with `+00:00`; dates are `YYYY-MM-DD` in `tz`.

200 body (`PhoneSummaryOut`):

    {
      "now": "<ISO>", "tz": "Europe/Stockholm", "today": "2026-11-26",
      "due_now": 3,                 # review questions due now: the same number as
                                    # GET /api/review/queue's `due` (and the header's count)
      "review": {
        "cap": 20,                  # the queue's default daily cap
        "done_today": 2,            # reviews answered since the start of today in tz
        "remaining_today": 18,      # = the queue's remaining_today with the default cap
        "items_total": 41,          # questions in the review queue, due or not
        "next_due_at": "<ISO>" | null   # the earliest due time of a question not due yet
      },
      "upcoming_due": {             # questions not due yet that become due within 7 days
        "window_days": 7,
        "until": "<ISO>",           # now + 7 days
        "total": 12,                # how many there are
        "limit": 100,               # at most this many times are listed
        "times": ["<ISO>", ...]     # their due times, soonest first (one per question; equal
                                    # times repeat), the first `limit` of them
      },
      "daily_answers": {            # the last 60 days in tz, oldest first, every day listed
        "days": 60, "first_day": "2026-09-28", "last_day": "2026-11-26",
        "items": [
          {"date": "2026-09-28",
           "page_answers": 4,       # attempts at a study page's gates (any page, any gate
                                    # type), by when they were made: an attempt uploaded from
                                    # the phone counts on the day it was made offline
           "review_answers": 1,     # review answers (likewise, by when they were made)
           "total": 5},
          ...
        ],
        "streak_days": 3            # consecutive days with total > 0 ending today, or
                                    # yesterday while today has none yet
      },
      "continue": <item> | null     # the page to resume: the first item of
                                    # GET /api/history/continue (same shape: {artifact, url,
                                    # last_studied_at, active_seconds}); null before any study
    }

An answer recorded with a time later than now (an offline upload's clock may be up to 5 minutes
ahead) is not counted until its day is among the 60.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from teacher.answer_days import count_by_day, day_window, streak, zone_named
from teacher.managers import BlockResponseManager
from teacher.managers.review import DEFAULT_DAILY_CAP, ReviewManager
from teacher.web.common import to_iso
from teacher.web.deps import SessionDep, SettingsDep
from teacher.web.routers.history import ContinueItemOut, continue_items
from teacher.web.routers.reading import MaxAttemptsDep
from teacher.web.routers.review import ClockDep, day_start

router = APIRouter(prefix="/api/phone", tags=["phone"])

SUMMARY_DAYS = 60
UPCOMING_WINDOW = timedelta(days=7)
UPCOMING_LIMIT = 100


class ReviewSummaryOut(BaseModel):
    cap: int
    done_today: int
    remaining_today: int
    items_total: int
    next_due_at: str | None


class UpcomingOut(BaseModel):
    window_days: int
    until: str
    total: int
    limit: int
    times: list[str]


class DayOut(BaseModel):
    date: str
    page_answers: int
    review_answers: int
    total: int


class DailyAnswersOut(BaseModel):
    days: int
    first_day: str
    last_day: str
    items: list[DayOut]
    streak_days: int


class PhoneSummaryOut(BaseModel):
    now: str
    tz: str
    today: str
    due_now: int
    review: ReviewSummaryOut
    upcoming_due: UpcomingOut
    daily_answers: DailyAnswersOut
    # `continue` is a Python keyword: the field is `continue_`, sent as "continue".
    continue_: ContinueItemOut | None = Field(serialization_alias="continue")


@router.get("/summary")
def phone_summary(
    session: SessionDep,
    settings: SettingsDep,
    max_attempts: MaxAttemptsDep,
    clock: ClockDep,
    tz: Annotated[str, Query(min_length=1, description="the reader's IANA time zone")],
) -> PhoneSummaryOut:
    """Review counts, upcoming due times, answers per day and the page to resume."""
    zone = zone_named(tz)
    now = clock()
    reviews = ReviewManager(session, settings)
    reviews.sync(max_attempts=max_attempts)
    queue = reviews.queue(now=now, day_start=day_start(now, tz), cap=DEFAULT_DAILY_CAP)
    until = now + UPCOMING_WINDOW
    upcoming = [v.item.due_at for v in queue.waiting if v.item.due_at <= until]

    dates, since = day_window(now, zone, SUMMARY_DAYS)
    pages = count_by_day(BlockResponseManager(session, settings).answer_times(since), zone, dates)
    review_days = count_by_day(reviews.answer_times(since), zone, dates)
    totals = [p + r for p, r in zip(pages, review_days, strict=True)]
    resume = continue_items(session, settings, max_attempts, 1)

    return PhoneSummaryOut(
        now=to_iso(now) or "",
        tz=tz,
        today=dates[-1].isoformat(),
        due_now=queue.due,
        review=ReviewSummaryOut(
            cap=queue.cap,
            done_today=queue.done_today,
            remaining_today=queue.remaining_today,
            items_total=queue.items_total,
            next_due_at=to_iso(queue.next_due_at),
        ),
        upcoming_due=UpcomingOut(
            window_days=UPCOMING_WINDOW.days,
            until=to_iso(until) or "",
            total=len(upcoming),
            limit=UPCOMING_LIMIT,
            times=[to_iso(t) or "" for t in upcoming[:UPCOMING_LIMIT]],
        ),
        daily_answers=DailyAnswersOut(
            days=SUMMARY_DAYS,
            first_day=dates[0].isoformat(),
            last_day=dates[-1].isoformat(),
            items=[
                DayOut(date=d.isoformat(), page_answers=p, review_answers=r, total=p + r)
                for d, p, r in zip(dates, pages, review_days, strict=True)
            ],
            streak_days=streak(totals),
        ),
        continue_=resume[0] if resume else None,
    )


__all__ = ["PhoneSummaryOut", "SUMMARY_DAYS", "UPCOMING_LIMIT", "UPCOMING_WINDOW", "router"]
