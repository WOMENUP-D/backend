"""The Development Score after the diagnostic: points earned by doing things.

The diagnostic measures where she starts. What she does afterwards — a course
finished, a task passed, a CV written, an application sent — is evidence that a
dimension has moved, and this module turns that evidence into points:

* **Only real events.** Callers are the services that already record the event
  (course completion, a passed evaluation, a career-history entry, a submitted
  application). Nothing here reacts to a page view or a button.
* **Once per source.** One row per (attempt, dimension, kind, source), so the
  same course, task or listing is never counted twice — re-ticking a lesson or
  re-applying earns nothing.
* **Capped.** At most `CAP_PER_DIMENSION` points on top of the measured value,
  and never above 100. Farming a dimension stops paying long before it means
  anything.
* **Tied to the measurement.** Adjustments build on the attempt that last
  measured the dimension. A new diagnostic is a fresh reading, so earlier
  adjustments stop counting instead of being stacked on top of it.

Adjustments only apply on top of a version-2 attempt, which records what it
measured; an older reading has no clean base to add to.
"""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProgramCategory, ScoreDimension
from app.models.assessment import Assessment, DevelopmentScore, ScoreAdjustment
from app.models.program import Program

CAP_PER_DIMENSION = 20.0

#: The one dimension a finished course moves. One, not every dimension that
#: lists the category, so a single course cannot lift two areas at once.
PROGRAM_CATEGORY_DIMENSION: dict[ProgramCategory, ScoreDimension] = {
    ProgramCategory.VOCATIONAL_SKILLS: ScoreDimension.EDUCATION_SKILLS,
    ProgramCategory.DIGITAL_SAFETY: ScoreDimension.DIGITAL_SKILLS,
    ProgramCategory.ENTREPRENEURSHIP: ScoreDimension.ENTREPRENEURSHIP,
    ProgramCategory.FINANCIAL_LITERACY: ScoreDimension.FINANCIAL_LITERACY,
    ProgramCategory.HEALTH: ScoreDimension.HEALTHY_LIFESTYLE,
    ProgramCategory.PARENTING: ScoreDimension.FAMILY_PARENTING,
    ProgramCategory.ETHICS_CULTURE: ScoreDimension.FAMILY_PARENTING,
    ProgramCategory.LEADERSHIP: ScoreDimension.LEADERSHIP,
    ProgramCategory.INTERNATIONAL: ScoreDimension.LEADERSHIP,
    ProgramCategory.VOLUNTEERING: ScoreDimension.LEADERSHIP,
    ProgramCategory.MENTORSHIP_NETWORKING: ScoreDimension.EMPLOYMENT,
    ProgramCategory.LEGAL_LITERACY: ScoreDimension.EMPLOYMENT,
}

PROGRAM_POINTS = 4.0
#: A passed practical task, by the dimension it practises: a business plan or
#: a product card is a step towards a business, a budget or a CV is a concrete
#: financial or career step, the rest is practice.
TASK_POINTS: dict[ScoreDimension, float] = {
    ScoreDimension.ENTREPRENEURSHIP: 5.0,
    ScoreDimension.FINANCIAL_LITERACY: 3.0,
    ScoreDimension.EMPLOYMENT: 3.0,
}
TASK_POINTS_DEFAULT = 2.0
CV_POINTS = 4.0
APPLICATION_POINTS = 3.0

_CANONICAL = list(ScoreDimension)


async def award(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    dimension: ScoreDimension,
    kind: str,
    source_type: str,
    source_id: str,
    points: float,
) -> bool:
    """Record points for one event and refresh the dimension. False when the
    event earns nothing: no v2 measurement to build on, or already counted."""
    score = await session.scalar(
        select(DevelopmentScore).where(
            DevelopmentScore.user_id == user_id, DevelopmentScore.dimension == dimension
        )
    )
    if score is None or score.assessment_id is None:
        return False
    attempt = await session.get(Assessment, score.assessment_id)
    measured = (attempt.dimension_scores or {}).get(dimension.value) if attempt else None
    if measured is None:
        return False

    inserted = await session.scalar(
        insert(ScoreAdjustment)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            assessment_id=attempt.id,
            dimension=dimension.value,
            kind=kind,
            source_type=source_type,
            source_id=source_id,
            points=points,
        )
        .on_conflict_do_nothing(constraint="uq_score_adjustments_source")
        .returning(ScoreAdjustment.id)
    )
    if inserted is None:
        return False

    earned = await session.scalar(
        select(func.coalesce(func.sum(ScoreAdjustment.points), 0.0)).where(
            ScoreAdjustment.assessment_id == attempt.id,
            ScoreAdjustment.dimension == dimension,
        )
    )
    score.current = adjusted(float(measured), float(earned or 0.0))
    await session.flush()
    return True


def adjusted(measured: float, earned: float) -> float:
    """The measured value plus what she earned, capped and kept within 0-100."""
    return float(max(0.0, min(100.0, measured + min(earned, CAP_PER_DIMENSION))))


# --- the events ---------------------------------------------------------------


async def on_program_completed(
    session: AsyncSession, *, user_id: uuid.UUID, program: Program
) -> bool:
    dimension = PROGRAM_CATEGORY_DIMENSION.get(program.category)
    if dimension is None:
        return False
    return await award(
        session,
        user_id=user_id,
        dimension=dimension,
        kind="program_completed",
        source_type="program",
        source_id=str(program.id),
        points=PROGRAM_POINTS,
    )


async def task_dimension(session: AsyncSession, labels: list[str]) -> ScoreDimension | None:
    """The dimension a task practises most, read from its skills' dimensions."""
    from app.services import skills as skill_service

    if not labels:
        return None
    index = await skill_service.SkillIndex.load(session, labels)
    skills = {skill.id: skill for label in labels if (skill := index.get(label)) is not None}
    counts: Counter[ScoreDimension] = Counter()
    for skill in skills.values():
        for value in skill.dimensions or []:
            try:
                counts[ScoreDimension(value)] += 1
            except ValueError:
                continue
    if not counts:
        return None
    return min(counts, key=lambda d: (-counts[d], _CANONICAL.index(d)))


async def on_task_passed(
    session: AsyncSession, *, user_id: uuid.UUID, task_id: uuid.UUID, labels: list[str]
) -> bool:
    dimension = await task_dimension(session, labels)
    if dimension is None:
        return False
    return await award(
        session,
        user_id=user_id,
        dimension=dimension,
        kind="task_passed",
        # The task, not the attempt: passing the same task again is not new work.
        source_type="task",
        source_id=str(task_id),
        points=TASK_POINTS.get(dimension, TASK_POINTS_DEFAULT),
    )


async def on_cv_updated(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Her CV gained an entry. Counted once per measurement, however many."""
    return await award(
        session,
        user_id=user_id,
        dimension=ScoreDimension.EMPLOYMENT,
        kind="cv_updated",
        source_type="cv",
        source_id="career_history",
        points=CV_POINTS,
    )


async def on_application_submitted(
    session: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID
) -> bool:
    return await award(
        session,
        user_id=user_id,
        dimension=ScoreDimension.EMPLOYMENT,
        kind="applied",
        source_type="opportunity",
        source_id=str(opportunity_id),
        points=APPLICATION_POINTS,
    )
