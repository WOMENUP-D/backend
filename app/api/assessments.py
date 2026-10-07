"""Diagnostics and the Development Score."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession
from app.core.constants import ScoreDimension
from app.models.assessment import (
    Assessment,
    AssessmentQuestion,
    DevelopmentScore,
)
from app.schemas.assessment import (
    AssessmentRead,
    DevelopmentScoreRead,
    LearningAnswers,
    LearningProfileRead,
    QuestionRead,
    ScoreInsightsRead,
    ScoreRead,
)
from app.services import learning_profile, questionnaire
from app.services.recommendation import dimension_insights
from app.services.scoring import (
    composite_score,
    weakest_dimensions,
)

router = APIRouter(prefix="/assessments", tags=["assessments"])


@router.get("/questions", response_model=list[QuestionRead])
async def list_questions(
    session: DbSession,
    user: CurrentUserDep,
    version: int | None = None,
) -> list[AssessmentQuestion]:
    """Active question set, ordered as it should be shown."""
    stmt = select(AssessmentQuestion).where(AssessmentQuestion.is_active.is_(True))
    if version is not None:
        stmt = stmt.where(AssessmentQuestion.version == version)
    stmt = stmt.order_by(AssessmentQuestion.dimension, AssessmentQuestion.order_index)
    return list((await session.execute(stmt)).scalars())


@router.post("/submit", status_code=status.HTTP_410_GONE, deprecated=True)
async def submit_assessment(user: CurrentUserDep) -> None:
    """Retired: this endpoint took the score of each answer from the browser.

    The diagnostic is now `POST /diagnostic/attempt`, which receives only the
    chosen options and scores them on the server.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Use POST /api/v1/diagnostic/attempt",
    )


@router.get("/score", response_model=DevelopmentScoreRead)
async def read_score(user: CurrentUserDep, session: DbSession) -> DevelopmentScoreRead:
    """The user's current Development Score."""
    scores = list(
        (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == uuid.UUID(user.id))
            )
        ).scalars()
    )
    if not scores:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assessment completed yet",
        )

    dimension_map: dict[ScoreDimension, float] = {s.dimension: s.current for s in scores}
    return DevelopmentScoreRead(
        composite=composite_score(dimension_map),
        dimensions=[
            ScoreRead(
                dimension=s.dimension,
                baseline=s.baseline,
                current=s.current,
                target=s.target,
                progress=s.progress,
            )
            for s in scores
        ],
        measured_at=max((s.measured_at for s in scores if s.measured_at), default=None),
        weakest_dimensions=weakest_dimensions(dimension_map),
    )


@router.get("/score/insights", response_model=ScoreInsightsRead)
async def read_score_insights(user: CurrentUserDep, session: DbSession) -> ScoreInsightsRead:
    """The score, dimension by dimension: how each reads, why, and what would move it.

    Built from her own answers, the published catalogue and open listings, with
    no model call — so it is as fast and as repeatable as the score itself.
    """
    insights = await dimension_insights(session, uuid.UUID(user.id))
    if insights is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No assessment completed yet",
        )
    return insights


@router.get("/history", response_model=list[AssessmentRead])
async def assessment_history(user: CurrentUserDep, session: DbSession) -> list[Assessment]:
    rows = await session.execute(
        select(Assessment)
        .where(Assessment.user_id == uuid.UUID(user.id))
        .order_by(Assessment.completed_at.desc())
    )
    return list(rows.scalars())


# --------------------------------------------------------------------------
# Learning profile: what she wants to learn, and how ready she is.
#
# Deliberately a different instrument from the Development Score above. The
# score measures eight dimensions of life and drives her plan and the national
# KPIs; this drives what the assistant recommends and how hard the level test
# starts.
# --------------------------------------------------------------------------


@router.get("/questionnaire")
async def learning_questionnaire() -> dict:
    """The question set itself — data, so the wording can change freely."""
    return questionnaire.payload()


@router.get("/learning-profile", response_model=LearningProfileRead)
async def read_learning_profile(user: CurrentUserDep, session: DbSession) -> LearningProfileRead:
    record = await learning_profile.get(session, user.id)
    answers = record.answers if record else {}
    return LearningProfileRead(
        version=record.version if record else questionnaire.VERSION,
        answers=answers,
        derived=record.derived if record else {},
        missing=learning_profile.missing_required(answers),
        completed=bool(record and record.completed_at),
    )


@router.put("/learning-profile", response_model=LearningProfileRead)
async def save_learning_profile(
    payload: LearningAnswers, user: CurrentUserDep, session: DbSession
) -> LearningProfileRead:
    """Save the run. Partial answers are kept so she can come back to it."""
    record = await learning_profile.save(session, user.id, payload.answers)
    await session.commit()
    return LearningProfileRead(
        version=record.version,
        answers=record.answers,
        derived=record.derived,
        missing=learning_profile.missing_required(record.answers),
        completed=bool(record.completed_at),
    )
