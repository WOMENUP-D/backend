"""User, profile and goal schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.constants import (
    GoalHorizon,
    Language,
    NewsTopic,
    Priority,
    Region,
    Role,
    ScoreDimension,
    UserStatus,
)
from app.schemas.common import ORMModel, validate_birth_date


class UserRead(ORMModel):
    id: uuid.UUID
    phone: str | None = None
    email: EmailStr | None = None
    status: UserStatus
    language: Language
    region: Region | None = None
    phone_verified: bool
    email_verified: bool
    onboarding_completed_at: datetime | None = None
    created_at: datetime


class UserUpdate(BaseModel):
    language: Language | None = None
    region: Region | None = None


class UserRoleAssign(BaseModel):
    role: Role
    scope_region: Region | None = None


class ProfileBase(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    birth_date: date | None = None
    age_group: str | None = None
    district: str | None = Field(default=None, max_length=120)
    education_level: str | None = None
    education_field: str | None = None
    employment_status: str | None = None
    profession: str | None = None
    years_of_experience: int | None = Field(default=None, ge=0, le=70)
    skills: list[str] = []
    interests: list[str] = []
    languages: list[str] = []
    bio: str | None = None


class ProfileSensitive(BaseModel):
    """Requires the `profile:sensitive` scope — never returned by default."""

    marital_status: str | None = None
    children_count: int | None = Field(default=None, ge=0, le=30)
    has_disability: bool | None = None
    is_in_women_register: bool | None = None


class ProfileUpdate(ProfileBase, ProfileSensitive):
    # Typed on the way in, so a subject outside the vocabulary is a 422 rather
    # than a row the news ranker will silently ignore. Not optional: omitting
    # the field leaves the stored choice alone (`exclude_unset`), and sending
    # an explicit null — which would violate the column — is rejected.
    news_interests: list[NewsTopic] = []

    # The same rule the onboarding applies, so this endpoint cannot be used to
    # store a date of birth the sign-up form would have refused.
    #
    # It belongs on the *input* model only. `ProfileRead` and `ProfileReadFull`
    # inherit from `ProfileBase` and are FastAPI response models, so a validator
    # placed there would run on the way out as well — and because the rule is
    # wall-clock dependent, a stored date that quietly aged past the ceiling
    # would turn every read of that profile into a 500 rather than the write
    # into a 422.
    _check_birth_date = field_validator("birth_date")(validate_birth_date)


class ProfileRead(ProfileBase, ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    avatar_url: str | None = None
    completeness_percent: int
    updated_at: datetime
    # Plain strings on the way out. Retiring a subject from `NewsTopic` should
    # cost a woman a tick on a preferences screen, not a 500 on her profile.
    news_interests: list[str] = []


class ProfileReadFull(ProfileRead, ProfileSensitive):
    """Full record including the sensitive block."""


class GoalCreate(BaseModel):
    title: str = Field(max_length=255)
    description: str | None = None
    horizon: GoalHorizon
    dimension: ScoreDimension | None = None
    priority: Priority = Priority.MEDIUM
    target_date: date | None = None


class GoalUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: Priority | None = None
    target_date: date | None = None
    achieved: bool | None = None


class GoalRead(ORMModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    horizon: GoalHorizon
    dimension: ScoreDimension | None = None
    priority: Priority
    target_date: date | None = None
    achieved: bool
    created_at: datetime


class ActivityDay(BaseModel):
    """One square in the calendar."""

    date: date
    count: int
    level: int = Field(ge=0, le=4)


class ActivitySummary(BaseModel):
    """The activity calendar and the figures shown above it."""

    days: list[ActivityDay]
    total_actions: int
    active_days: int
    points: int
    current_streak: int
    best_streak: int
    from_date: date
    to_date: date
