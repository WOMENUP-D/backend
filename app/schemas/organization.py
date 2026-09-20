"""Organisation contracts: the public profile, the workspace, and what an
organisation may see about a woman.

`OrganizationPublic` is built field by field from a published organisation and
carries nothing about its people. `CandidateProfile` is exactly the fields
`services.employer.EMPLOYER_FIELDS` names — the ones the apply page lists before
she confirms — and `CandidateCard` is the anonymous reading an organisation has
before she accepts an invitation: a pseudonym, never an id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    ApplicationStatus,
    EventFormat,
    InvitationStatus,
    OpportunityType,
    OrganizationKind,
    OrgMemberRole,
    Region,
)
from app.schemas.opportunity import OrganizationBrief
from app.schemas.skill import SkillRef

# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


class OrgListingSummary(BaseModel):
    id: uuid.UUID
    type: OpportunityType
    title_i18n: dict
    region: str | None = None
    deadline: datetime | None = None


class OrgProgrammeSummary(BaseModel):
    id: uuid.UUID
    slug: str
    title_i18n: dict


class OrganizationPublic(BaseModel):
    """What anyone may read about a published organisation. Nothing else."""

    slug: str
    name: str
    kind: OrganizationKind
    is_verified: bool
    description_i18n: dict = {}
    industry: str | None = None
    region: Region | None = None
    city: str | None = None
    website: str | None = None
    logo_url: str | None = None
    listings: list[OrgListingSummary] = []
    programmes: list[OrgProgrammeSummary] = []


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------


class OrganizationRead(BaseModel):
    """An organisation as its own people and WomanUP's administrators see it."""

    id: uuid.UUID
    slug: str
    name: str
    kind: OrganizationKind
    description_i18n: dict = {}
    industry: str | None = None
    region: Region | None = None
    city: str | None = None
    website: str | None = None
    logo_url: str | None = None
    is_public: bool
    is_verified: bool
    verified_at: datetime | None = None
    is_active: bool
    #: The caller's own role in it, in the workspace.
    my_role: OrgMemberRole | None = None


class OrganizationProfileIn(BaseModel):
    description_i18n: dict | None = None
    industry: str | None = Field(default=None, max_length=120)
    region: Region | None = None
    city: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=300)
    logo_url: str | None = Field(default=None, max_length=500)
    is_public: bool | None = None


class MemberRead(BaseModel):
    user_id: uuid.UUID
    role: OrgMemberRole
    first_name: str | None = None
    #: Administrators only; an organisation's own list shows names.
    email: str | None = None


class ListingIn(BaseModel):
    type: OpportunityType
    title_i18n: dict
    description_i18n: dict = {}
    region: Region | None = None
    skills: list[str] = Field(default=[], max_length=12)
    #: {salary_from, salary_to, amount, stipend, text} — what she would receive.
    reward: dict = {}
    #: {age_min, age_max} — enforced by the platform's one eligibility rule.
    eligibility: dict = {}
    deadline: datetime | None = None
    #: For an event: when and where. A workshop, seminar, conference, forum or
    #: networking event must have `starts_at`.
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    format: EventFormat | None = None
    venue: str | None = Field(default=None, max_length=300)


class ListingUpdate(BaseModel):
    title_i18n: dict | None = None
    description_i18n: dict | None = None
    region: Region | None = None
    skills: list[str] | None = Field(default=None, max_length=12)
    reward: dict | None = None
    eligibility: dict | None = None
    deadline: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    format: EventFormat | None = None
    venue: str | None = Field(default=None, max_length=300)


class OrgListingRead(BaseModel):
    id: uuid.UUID
    type: OpportunityType
    title_i18n: dict
    description_i18n: dict = {}
    region: str | None = None
    skills: list[SkillRef] = []
    reward: dict = {}
    eligibility: dict = {}
    deadline: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    format: EventFormat | None = None
    venue: str | None = None
    is_open: bool
    applications: int = 0
    created_at: datetime | None = None


class CandidateSkill(BaseModel):
    skill: SkillRef
    #: learned / assessed / verified — or self_reported, her own word.
    status: str


class CandidateProfile(BaseModel):
    """What an organisation sees once she has shared with it. Exactly these."""

    first_name: str | None = None
    region: str | None = None
    language: str | None = None
    education_level: str | None = None
    employment_status: str | None = None
    profession: str | None = None
    years_of_experience: int | None = None
    skills: list[CandidateSkill] = []
    public_portfolio: str | None = None


class OrgApplicationRead(BaseModel):
    id: uuid.UUID
    status: ApplicationStatus
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    #: The organisation's own history, its internal notes included.
    status_history: list[dict] = []
    listing: OrgListingSummary
    profile: CandidateProfile | None = None
    #: `withdrawn` or `consent_withdrawn` when the profile is not shown.
    hidden: str | None = None
    #: Statuses it may move to next.
    next: list[ApplicationStatus] = []


class StatusIn(BaseModel):
    status: ApplicationStatus
    note: str | None = Field(default=None, max_length=500)


class CandidateCard(BaseModel):
    """A findable candidate before she accepts: a pseudonym, never an id."""

    ref: str
    region: str | None = None
    education_level: str | None = None
    years_of_experience: int | None = None
    skills: list[CandidateSkill] = []
    certificates: int = 0
    passed_tasks: int = 0
    invitation: InvitationStatus | None = None


class InviteIn(BaseModel):
    ref: str = Field(min_length=8, max_length=64)
    opportunity_id: uuid.UUID | None = None
    message: str | None = Field(default=None, max_length=500)


class OrgInvitationRead(BaseModel):
    id: uuid.UUID
    ref: str
    status: InvitationStatus
    opportunity_id: uuid.UUID | None = None
    message: str | None = None
    created_at: datetime
    responded_at: datetime | None = None
    profile: CandidateProfile | None = None


# ---------------------------------------------------------------------------
# Her side
# ---------------------------------------------------------------------------


class MyInvitationRead(BaseModel):
    id: uuid.UUID
    status: InvitationStatus
    message: str | None = None
    created_at: datetime
    organization: OrganizationBrief
    opportunity: OrgListingSummary | None = None
    #: What accepting would share, so she can read it before she decides.
    fields: list[str] = []


class InvitationAnswer(BaseModel):
    accept: bool


# ---------------------------------------------------------------------------
# Administrators
# ---------------------------------------------------------------------------


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    kind: OrganizationKind


class OrganizationStatusIn(BaseModel):
    is_verified: bool | None = None
    is_active: bool | None = None


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: OrgMemberRole = OrgMemberRole.MEMBER


class OrganizationAdminRead(OrganizationRead):
    members: list[MemberRead] = []
    listings: int = 0
