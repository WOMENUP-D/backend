"""Daily activity, streaks and points.

Section 11 asks for a light gamification model, and light is the operative
word: everything here is *derived* from things the woman actually did — a plan
step finished, a programme started, a diagnostic taken, an application sent —
rather than from a separate score she is invited to farm. There is no new table
and nothing to inflate; delete a plan item and the day it earned disappears
with it.

The streak deliberately tolerates today being empty. A streak that breaks at
00:00 before she has had her morning punishes her for the clock rather than for
stopping, and the whole point of showing it is encouragement.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Assessment
from app.models.audit import AiInteraction
from app.models.opportunity import Application
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.program import Enrollment

# What each kind of act is worth. Finishing something outranks starting it, and
# nothing is worth so much that one action dwarfs a week of steady work.
POINTS = {
    "plan_item": 10,
    "program_started": 5,
    "program_completed": 50,
    "assessment": 20,
    "application": 15,
    "assistant": 2,
}

# How many acts in a day fill the square. Four is a full day, not forty: the
# calendar should reward showing up, not grinding.
LEVEL_THRESHOLDS = (1, 2, 4, 7)


@dataclass(slots=True)
class ActivitySummary:
    days: list[dict] = field(default_factory=list)
    total_actions: int = 0
    active_days: int = 0
    points: int = 0
    current_streak: int = 0
    best_streak: int = 0
    from_date: date = field(default_factory=date.today)
    to_date: date = field(default_factory=date.today)


def _level(count: int) -> int:
    """0 for an empty day, 1..4 for increasingly busy ones."""
    level = 0
    for threshold in LEVEL_THRESHOLDS:
        if count >= threshold:
            level += 1
    return level


def _streaks(active: set[date], today: date) -> tuple[int, int]:
    """Current and best run of consecutive active days.

    The current run may end today or yesterday — see the module docstring.
    """
    if not active:
        return 0, 0

    best = 0
    for day in active:
        if day - timedelta(days=1) in active:
            continue  # not the start of a run
        length = 1
        while day + timedelta(days=length) in active:
            length += 1
        best = max(best, length)

    anchor = today if today in active else today - timedelta(days=1)
    current = 0
    while anchor - timedelta(days=current) in active:
        current += 1

    return current, best


async def summary(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    days: int = 365,
    year: int | None = None,
) -> ActivitySummary:
    """Per-day activity for the calendar, plus the numbers above it.

    `year` renders a calendar year from 1 January up to today, which is how a
    year of effort is actually read — a rolling 365-day window starts in the
    middle of a month and gives no anchor to compare against. `days` remains
    for a plain rolling window.
    """
    to_date = datetime.now(UTC).date()
    if year is not None:
        from_date = date(year, 1, 1)
        if year < to_date.year:
            to_date = date(year, 12, 31)
    else:
        from_date = to_date - timedelta(days=days - 1)
    since = datetime.combine(from_date, datetime.min.time(), tzinfo=UTC)

    counts: dict[date, int] = defaultdict(int)
    points = 0

    async def tally(stmt, kind: str) -> None:
        nonlocal points
        for moment in (await session.execute(stmt)).scalars():
            if moment is None:
                continue
            day = moment.astimezone(UTC).date() if moment.tzinfo else moment.date()
            if day < from_date or day > to_date:
                continue
            counts[day] += 1
            points += POINTS[kind]

    await tally(
        select(PlanItem.completed_at)
        .join(DevelopmentPlan, DevelopmentPlan.id == PlanItem.plan_id)
        .where(DevelopmentPlan.user_id == user_id, PlanItem.completed_at.is_not(None)),
        "plan_item",
    )
    await tally(
        select(Enrollment.started_at).where(
            Enrollment.user_id == user_id, Enrollment.started_at.is_not(None)
        ),
        "program_started",
    )
    await tally(
        select(Enrollment.completed_at).where(
            Enrollment.user_id == user_id, Enrollment.completed_at.is_not(None)
        ),
        "program_completed",
    )
    await tally(
        select(Assessment.completed_at).where(
            Assessment.user_id == user_id, Assessment.completed_at.is_not(None)
        ),
        "assessment",
    )
    await tally(
        select(Application.submitted_at).where(
            Application.user_id == user_id, Application.submitted_at.is_not(None)
        ),
        "application",
    )
    await tally(
        select(AiInteraction.created_at).where(
            AiInteraction.user_id == user_id, AiInteraction.created_at >= since
        ),
        "assistant",
    )

    current, best = _streaks(set(counts), to_date)

    grid = []
    cursor = from_date
    while cursor <= to_date:
        count = counts.get(cursor, 0)
        grid.append({"date": cursor.isoformat(), "count": count, "level": _level(count)})
        cursor += timedelta(days=1)

    return ActivitySummary(
        days=grid,
        total_actions=sum(counts.values()),
        active_days=len(counts),
        points=points,
        current_streak=current,
        best_streak=best,
        from_date=from_date,
        to_date=to_date,
    )
