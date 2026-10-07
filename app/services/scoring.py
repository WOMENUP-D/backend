"""WomanUP Development Score.

A 0-100 composite over the eight dimensions defined in section 03. Each
dimension keeps baseline / current / target so progress is measurable against
where the user started, not against an absolute ideal.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ScoreDimension
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    DevelopmentScore,
)

#: Each dimension's share of the composite. Equal by design since diagnostic v2;
#: still sent with every dimension so the cabinet need not hard-code it.
DIMENSION_WEIGHT = round(1 / len(ScoreDimension), 4)


def composite_score(dimension_scores: dict[ScoreDimension, float]) -> float:
    """The plain mean of the dimensions actually present.

    Every dimension counts the same (diagnostic v2): (EDU + CAR + … + LEA) / 8.
    Averaging over what is present keeps a partial reading comparable — a
    record from before digital skills existed has seven dimensions, not a zero.
    """
    present = [v for v in dimension_scores.values() if v is not None]
    if not present:
        return 0.0
    return round(sum(present) / len(present), 2)


def weakest_dimensions(
    dimension_scores: dict[ScoreDimension, float], limit: int = 3
) -> list[ScoreDimension]:
    """Lowest-scoring dimensions — the AI planner's starting point."""
    return [d for d, _ in sorted(dimension_scores.items(), key=lambda kv: kv[1])[:limit]]


def default_target(current: float) -> float:
    """A first target: close roughly a third of the remaining gap, capped at 100.

    Deliberately modest — an unreachable target reads as discouraging, and
    section 19 measures plan adoption, not plan ambition.
    """
    return round(min(current + (100 - current) / 3, 100), 2)


async def calculate_dimension_scores(
    session: AsyncSession, assessment_id: uuid.UUID
) -> dict[ScoreDimension, float]:
    """Aggregate answers into a 0-100 value per dimension.

    Each answer is weighted by its question's weight; the result is the
    weighted mean of answer values within the dimension.
    """
    rows = await session.execute(
        select(AssessmentAnswer.value, AssessmentQuestion.dimension, AssessmentQuestion.weight)
        .join(AssessmentQuestion, AssessmentAnswer.question_id == AssessmentQuestion.id)
        .where(AssessmentAnswer.assessment_id == assessment_id)
    )

    totals: dict[ScoreDimension, float] = defaultdict(float)
    weights: dict[ScoreDimension, float] = defaultdict(float)
    for value, dimension, weight in rows:
        totals[dimension] += value * weight
        weights[dimension] += weight

    return {
        dimension: round(totals[dimension] / weights[dimension], 2)
        for dimension in totals
        if weights[dimension] > 0
    }


async def persist_scores(
    session: AsyncSession,
    user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    dimension_scores: dict[ScoreDimension, float],
) -> list[DevelopmentScore]:
    """Write scores, preserving the baseline from the user's first assessment."""
    existing = {
        score.dimension: score
        for score in (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == user_id)
            )
        ).scalars()
    }

    now = datetime.now(UTC)
    saved: list[DevelopmentScore] = []

    for dimension, value in dimension_scores.items():
        if record := existing.get(dimension):
            record.current = value
            record.assessment_id = assessment_id
            record.measured_at = now
            if record.target is None:
                record.target = default_target(value)
        else:
            record = DevelopmentScore(
                user_id=user_id,
                assessment_id=assessment_id,
                dimension=dimension,
                baseline=value,
                current=value,
                target=default_target(value),
                measured_at=now,
            )
            session.add(record)
        saved.append(record)

    return saved


async def is_baseline_assessment(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """True when the user has no completed assessment yet."""
    completed = await session.scalar(
        select(Assessment.id)
        .where(Assessment.user_id == user_id, Assessment.completed_at.is_not(None))
        .limit(1)
    )
    return completed is None
