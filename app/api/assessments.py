"""Diagnostics and the Development Score."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession
from app.core.constants import ScoreDimension
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    DevelopmentScore,
)
from app.schemas.assessment import (
    AssessmentRead,
    AssessmentSubmit,
    DevelopmentScoreRead,
    LearningAnswers,
    LearningProfileRead,
    QuestionRead,
    ScoreRead,
)
from app.services import learning_profile, questionnaire
from app.services.scoring import (
    calculate_dimension_scores,
    composite_score,
    is_baseline_assessment,
    persist_scores,
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


@router.post("/submit", response_model=DevelopmentScoreRead)
async def submit_assessment(
    payload: AssessmentSubmit, user: CurrentUserDep, session: DbSession
) -> DevelopmentScoreRead:
    """Score a completed diagnostic and store the result."""
    user_id = uuid.UUID(user.id)
    baseline = await is_baseline_assessment(session, user_id)

    question_ids = [answer.question_id for answer in payload.answers]
    known = set(
        (
            await session.execute(
                select(AssessmentQuestion.id).where(AssessmentQuestion.id.in_(question_ids))
            )
        ).scalars()
    )
    unknown = set(question_ids) - known
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown question ids: {sorted(str(i) for i in unknown)}",
        )

    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now, is_baseline=baseline)
    session.add(assessment)
    await session.flush()

    for answer in payload.answers:
        session.add(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=answer.question_id,
                value=answer.value,
                raw_answer=answer.raw_answer,
            )
        )
    await session.flush()

    dimension_scores = await calculate_dimension_scores(session, assessment.id)
    scores = await persist_scores(session, user_id, assessment.id, dimension_scores)
    await session.flush()

    return DevelopmentScoreRead(
        composite=composite_score(dimension_scores),
        dimensions=[
            ScoreRead(
                dimension=score.dimension,
                baseline=score.baseline,
                current=score.current,
                target=score.target,
                progress=score.progress,
            )
            for score in scores
        ],
        assessment_id=assessment.id,
        measured_at=now,
        weakest_dimensions=weakest_dimensions(dimension_scores),
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
