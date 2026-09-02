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

from app.core.constants import SCORE_WEIGHTS, ScoreDimension
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    DevelopmentScore,
)


def composite_score(dimension_scores: dict[ScoreDimension, float]) -> float:
    """Weighted mean over the dimensions actually present.

    Weights are renormalised across supplied dimensions, so a partial
    assessment still yields a comparable 0-100 figure.
    """
    present = {d: v for d, v in dimension_scores.items() if v is not None}
    if not present:
        return 0.0
    total_weight = sum(SCORE_WEIGHTS[d] for d in present)
    if total_weight == 0:
        return 0.0
    weighted = sum(SCORE_WEIGHTS[d] * v for d, v in present.items())
    return round(weighted / total_weight, 2)


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
