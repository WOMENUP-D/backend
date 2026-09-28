"""Recommendation contracts: next steps and the suggestions behind them.

Every item carries a `reason` and the facts it quotes (`params`, the matched
skills) instead of a composed sentence. The portal is read in four locales, and
an explanation the browser renders from keys stays true in all of them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    EnrollmentStatus,
    NextStepKind,
    OpportunityType,
    ProficiencyLevel,
    ProgramCategory,
    RecommendationReason,
    ScoreDimension,
)
from app.schemas.skill import SkillRef

# The numbers a reason quotes: a score, a progress percentage, a count.
Params = dict[str, int | float | str]


class ProgramSuggestion(BaseModel):
    """A published programme, and why it is offered to her."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    category: ProgramCategory
    duration_weeks: int | None = None
    has_certificate: bool = False
    skills_taught: list[SkillRef] = []
    # What she would learn that she does not already hold.
    new_skills: list[SkillRef] = []
    enrollment_status: EnrollmentStatus | None = None
    progress_percent: int | None = None
    dimension: ScoreDimension | None = None
    reason: RecommendationReason
    params: Params = {}


class OpportunitySuggestion(BaseModel):
    """An open listing, and why it is offered to her."""

    id: uuid.UUID
    type: OpportunityType
    title_i18n: dict
    organisation: str | None = None
    region: str | None = None
    deadline: datetime | None = None
    # Share of the listed requirements her skills cover. None when the listing
    # names no skills: "100% match" on a grant that asks for nothing would be a
    # number without a meaning.
    match: float | None = Field(default=None, ge=0, le=1)
    matched_skills: list[SkillRef] = []
    missing_skills: list[SkillRef] = []
    dimension: ScoreDimension | None = None
    reason: RecommendationReason
    params: Params = {}


class PathSuggestion(BaseModel):
    """A learning path, and why it is offered to her.

    Deliberately lighter than what the path screens read: a recommendation
    names a route and says how far along it she is, and the route itself is one
    tap away. Progress is the same derived figure the path endpoints return —
    counted from her enrollments, never stored.
    """

    id: uuid.UUID
    slug: str
    title_i18n: dict
    dimension: ScoreDimension | None = None
    level: ProficiencyLevel | None = None
    program_count: int = 0
    completed_count: int = 0
    percent: int = 0
    # Whether she has said she is on it, as opposed to having wandered onto it
    # by enrolling in one of its courses.
    started: bool = False
    # What the route would teach her that she does not hold yet.
    new_skills: list[SkillRef] = []
    reason: RecommendationReason
    params: Params = {}


class TaskSuggestion(BaseModel):
    """A practical task, and why it is offered to her."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    level: ProficiencyLevel | None = None
    estimated_minutes: int | None = None
    # What doing it would practise, resolved to the taxonomy.
    skills: list[SkillRef] = []
    # Where she stands on it, when she has touched it at all.
    status: str | None = None
    reason: RecommendationReason
    params: Params = {}


class NextStep(BaseModel):
    """One thing worth doing next, with what it points at and why."""

    kind: NextStepKind
    reason: RecommendationReason
    dimension: ScoreDimension | None = None
    params: Params = {}
    program: ProgramSuggestion | None = None
    path: PathSuggestion | None = None
    task: TaskSuggestion | None = None
    plan_item_id: uuid.UUID | None = None
    # A plan step's own words, in the language the plan was written in.
    text: str | None = None
    due_date: date | None = None
    opportunity_types: list[OpportunityType] = []


class RecommendationsRead(BaseModel):
    """The cabinet's answer to "what should I do next, and what is open to me"."""

    assessed: bool
    next_steps: list[NextStep]
    programs: list[ProgramSuggestion]
    # Routes through several courses, ranked the same way: the ones she is on
    # first, then the ones that build the dimensions that need her most.
    paths: list[PathSuggestion] = []
    # Work to do and be assessed on, drawn from the skills she is missing.
    tasks: list[TaskSuggestion] = []
    opportunities: list[OpportunitySuggestion]
