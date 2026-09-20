"""Practical task contracts: the brief, the attempt, and the verdict.

A task crosses the API twice over. `PracticalTaskRead` is the brief — public,
identical for everyone, and carrying the criteria the work will be judged
against so she is never assessed on something she was not told. `TaskAttemptRead`
is hers alone: what she submitted, and what came back.

The two are kept apart because their audiences are. An evaluator reading the
review queue sees an attempt and the task it answers; she sees her own attempts
and nobody else's. Nothing in the brief is private, and nothing in an attempt
is public.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    EvaluatorKind,
    ProficiencyLevel,
    TaskStatus,
    TaskSubmissionKind,
)
from app.schemas.skill import SkillRef


class TaskCriterion(BaseModel):
    """One thing the work is judged against."""

    key: str
    text_i18n: dict = {}


class TaskField(BaseModel):
    """One named answer a `fields` task asks for."""

    key: str
    label_i18n: dict = {}
    min_chars: int = 0


class CriterionVerdict(BaseModel):
    """How one criterion was judged. `note` is the evaluator's own words."""

    key: str
    met: bool
    note: str = ""


class TaskEvaluationRead(BaseModel):
    """What came back. Absent entirely until somebody actually assessed it."""

    passed: bool
    score: float | None = None
    feedback: str = ""
    criteria_met: list[CriterionVerdict] = []
    evaluator_kind: EvaluatorKind | None = None
    evaluated_at: datetime | None = None
    #: What the pass wrote into her skill record — named so the connection
    #: between doing the work and holding the skill is visible rather than
    #: implied.
    skills_evidenced: list[SkillRef] = []


class TaskAttemptRead(BaseModel):
    """One run at a task. Hers, or an evaluator's view of one."""

    id: uuid.UUID
    attempt_no: int
    status: TaskStatus
    submission: dict = {}
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    evaluation: TaskEvaluationRead | None = None


class PracticalTaskRead(BaseModel):
    """A task as the catalogue lists it. Public — nothing personal here."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    summary_i18n: dict = {}
    kind: TaskSubmissionKind
    level: ProficiencyLevel | None = None
    estimated_minutes: int | None = None
    skills: list[SkillRef] = []
    # Of those, the ones she does not hold yet. Empty for a visitor.
    new_skills: list[SkillRef] = []
    program_id: uuid.UUID | None = None
    program_slug: str | None = None
    program_title_i18n: dict = {}
    ai_reviewed: bool = True

    #: Where she stands on it. `None` for a visitor, and for a signed-in woman
    #: who has never opened it — which is a different thing from "started".
    status: TaskStatus | None = None
    attempts: int = 0


class PracticalTaskDetail(PracticalTaskRead):
    """The task opened: the full brief, and her own history with it."""

    instructions_i18n: dict = {}
    outcome_i18n: dict = {}
    criteria: list[TaskCriterion] = []
    fields: list[TaskField] = []
    min_chars: int | None = None
    #: Newest first. A retry is a new attempt, so this is the story of the work.
    my_attempts: list[TaskAttemptRead] = []
    #: Whether she may start a fresh attempt right now, and nothing about
    #: whether a button should be grey — the server refuses either way.
    can_start: bool = False
    can_submit: bool = False


class TaskSubmitIn(BaseModel):
    """What she hands in. Shaped by the task's kind, checked on the server."""

    text: str | None = Field(default=None, max_length=20000)
    link: str | None = Field(default=None, max_length=500)
    fields: dict[str, str] = {}


class TaskEvaluateIn(BaseModel):
    """An authorized evaluator's verdict.

    `passed` is required: an evaluation that does not say whether the work
    passed is not an evaluation.
    """

    passed: bool
    score: float | None = Field(default=None, ge=0, le=100)
    feedback: str = Field(default="", max_length=4000)
    criteria_met: list[CriterionVerdict] = []


class ReviewItem(BaseModel):
    """One submission waiting for a person, as the review queue shows it.

    Carries the work and the brief it answers — and no more of the woman who
    wrote it than the evaluation needs. Her name is not here.
    """

    attempt: TaskAttemptRead
    task: PracticalTaskDetail


class PracticalTaskCreate(BaseModel):
    """Authoring a task. Trainers, moderators and admins only."""

    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=160)
    title_i18n: dict
    summary_i18n: dict = {}
    instructions_i18n: dict = {}
    outcome_i18n: dict = {}
    criteria: list[TaskCriterion] = Field(default=[], max_length=8)
    kind: TaskSubmissionKind = TaskSubmissionKind.TEXT
    fields: list[TaskField] = Field(default=[], max_length=8)
    min_chars: int | None = Field(default=None, ge=0, le=20000)
    level: ProficiencyLevel | None = None
    estimated_minutes: int | None = Field(default=None, gt=0)
    skills_practised: list[str] = []
    program_id: uuid.UUID | None = None
    ai_reviewed: bool = True
    is_published: bool = False


class PracticalTaskUpdate(BaseModel):
    title_i18n: dict | None = None
    summary_i18n: dict | None = None
    instructions_i18n: dict | None = None
    outcome_i18n: dict | None = None
    criteria: list[TaskCriterion] | None = None
    level: ProficiencyLevel | None = None
    estimated_minutes: int | None = None
    skills_practised: list[str] | None = None
    ai_reviewed: bool | None = None
    is_published: bool | None = None
