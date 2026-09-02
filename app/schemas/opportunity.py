"""Opportunity, application and outcome schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    ApplicationStatus,
    OpportunitySource,
    OpportunityType,
)
from app.schemas.common import ORMModel


class OpportunityRead(ORMModel):
    id: uuid.UUID
    source: OpportunitySource
    type: OpportunityType
    title_i18n: dict
    description_i18n: dict = {}
    organisation: str | None = None
    region: str | None = None
    required_skills: list[str]
    reward: dict
    deadline: datetime | None = None
    external_url: str | None = None


class OpportunityMatch(OpportunityRead):
    """A recommendation carries its own explanation — section 06 requires
    every AI suggestion to be interpretable."""

    match_score: float = Field(ge=0, le=1)
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    explanation: str


class OpportunityStats(BaseModel):
    """The shape of the live catalogue, in numbers.

    The landing page has to say what is on offer *before* it asks for an
    account. Counts computed over the same active-only set the public listing
    returns, so the promise on the front page and the list behind it cannot
    drift apart.
    """

    total: int
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    regions: int = 0


class OpportunityFilter(BaseModel):
    source: OpportunitySource | None = None
    type: OpportunityType | None = None
    region: str | None = None
    search: str | None = None
    active_only: bool = True


class ApplicationCreate(BaseModel):
    opportunity_id: uuid.UUID
    payload: dict = {}


class ApplicationRead(ORMModel):
    id: uuid.UUID
    opportunity_id: uuid.UUID
    status: ApplicationStatus
    external_application_id: str | None = None
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class ApplicationStatusUpdate(BaseModel):
    """Inbound callback from a partner platform."""

    status: ApplicationStatus
    external_application_id: str | None = None
    note: str | None = None


class OutcomeCreate(BaseModel):
    source: OpportunitySource
    outcome_type: str = Field(
        pattern="^(employment|business_registered|funding|first_sale|milestone)$"
    )
    application_id: uuid.UUID | None = None
    details: dict = {}
    occurred_at: datetime | None = None


class OutcomeRead(ORMModel):
    id: uuid.UUID
    source: OpportunitySource
    outcome_type: str
    details: dict
    verified: bool
    occurred_at: datetime | None = None


class SkillGap(BaseModel):
    """Result of comparing a user's skills against a vacancy's requirements."""

    opportunity_id: uuid.UUID
    matched_skills: list[str]
    missing_skills: list[str]
    coverage: float = Field(ge=0, le=1)
    recommended_program_ids: list[uuid.UUID] = []
