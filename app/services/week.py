"""Her week: the next lesson, the next event, the next task, the next listing.

One of each at most, and each one a record she owns or the engine's own pick
— nothing is filled in to make the row look complete. Read from one context
(`recommendation.load_context`, through the listings reader) so this row can
never disagree with the cabinet under it or the Coach beside it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EnrollmentStatus
from app.schemas.week import WeekLesson, WeekOpportunity, WeekRead
from app.services import events
from app.services import opportunities as listings
from app.services import recommendation as engine
from app.services.ai_coach import next_lesson

_OPEN = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)
_NEVER = datetime.min.replace(tzinfo=UTC)
_NO_DEADLINE = datetime.max.replace(tzinfo=UTC)


async def week_for(session: AsyncSession, user_id: uuid.UUID) -> WeekRead:
    reader = await listings.reader_for(session, user_id)
    ctx = reader.ctx

    # "Where she left off" is the course she touched last — not the one she is
    # furthest along, which is how the engine ranks courses to recommend.
    lesson = None
    enrolled = sorted(
        (e for e in ctx.enrollments.values() if e.status in _OPEN and e.program_id in ctx.programs),
        key=lambda e: e.last_activity_at or e.started_at or _NEVER,
        reverse=True,
    )
    if enrolled:
        enrollment = enrolled[0]
        program = ctx.programs[enrollment.program_id]
        upcoming = await next_lesson(session, ctx, program.id)
        lesson = WeekLesson(
            program_id=program.id,
            program_slug=program.slug,
            program_title_i18n=program.title_i18n,
            lesson_title_i18n=upcoming.title_i18n if upcoming else {},
            progress=enrollment.progress_percent,
        )

    recommended, own = await events.her_events(session, user_id, limit=1, reader=reader)
    if own:
        event, event_why = own[0], "yours"
    elif recommended:
        event, event_why = recommended[0], "suggested"
    else:
        event, event_why = None, None

    step = engine.task_step(ctx)

    opportunity = None
    kept = sorted(
        (item for item in ctx.opportunities if item.id in reader.saved),
        key=lambda item: item.deadline or _NO_DEADLINE,
    )
    if kept:
        item = kept[0]
        opportunity = WeekOpportunity(
            id=item.id,
            type=item.type,
            title_i18n=item.title_i18n,
            organisation=item.organisation,
            deadline=item.deadline,
            why="saved",
        )
    else:
        suggestions = engine.opportunity_suggestions(ctx)
        if suggestions:
            best = suggestions[0]
            opportunity = WeekOpportunity(
                id=best.id,
                type=best.type,
                title_i18n=best.title_i18n,
                organisation=best.organisation,
                deadline=best.deadline,
                why="suggested",
            )

    return WeekRead(
        lesson=lesson,
        event=event,
        event_why=event_why,
        task=step.task if step else None,
        opportunity=opportunity,
    )
