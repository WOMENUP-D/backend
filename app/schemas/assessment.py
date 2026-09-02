"""Diagnostic and Development Score schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.constants import ScoreDimension
from app.schemas.common import ORMModel


class QuestionRead(ORMModel):
    id: uuid.UUID
    dimension: ScoreDimension
    order_index: int
    question_type: str
    text_i18n: dict
    options: list


class AnswerSubmit(BaseModel):
    question_id: uuid.UUID
    value: float = Field(ge=0, le=100)
    raw_answer: str | None = None


class AssessmentSubmit(BaseModel):
    answers: list[AnswerSubmit] = Field(min_length=1)


class ScoreRead(ORMModel):
    dimension: ScoreDimension
    baseline: float
    current: float
    target: float | None = None
    progress: float


class DevelopmentScoreRead(BaseModel):
    """The 0-100 composite plus its per-dimension breakdown."""

    composite: float = Field(ge=0, le=100)
    dimensions: list[ScoreRead]
    assessment_id: uuid.UUID | None = None
    measured_at: datetime | None = None
    weakest_dimensions: list[ScoreDimension] = []


class AssessmentRead(ORMModel):
    id: uuid.UUID
    version: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    is_baseline: bool


class LearningAnswers(BaseModel):
    """Whatever she has filled in so far — partial saves are allowed."""

    answers: dict[str, Any] = Field(default_factory=dict)


class LearningProfileRead(BaseModel):
    """Her questionnaire run and the summary drawn from it."""

    version: int
    answers: dict[str, Any]
    derived: dict[str, Any]
    missing: list[str]
    completed: bool
