"""Opportunity, application and outcome schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    ApplicationStatus,
    EligibilityStatus,
    EventFormat,
    OpportunitySource,
    OpportunityType,
    SkillStatus,
)
from app.schemas.common import ORMModel
from app.schemas.skill import SkillRef


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
    #: Set only for an event (`services.events`): when and where it happens.
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    format: EventFormat | None = None
    venue: str | None = None


class OpportunityMatch(OpportunityRead):
    """A recommendation carries its own explanation — section 06 requires
    every AI suggestion to be interpretable.

    Skills travel as references rather than as the words the listing happened
    to use, so "buxgalteriya" reads as Бухгалтерия for a Russian reader.
    """

    match_score: float = Field(ge=0, le=1)
    matched_skills: list[SkillRef] = []
    missing_skills: list[SkillRef] = []
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


class OpportunitySummary(BaseModel):
    """What an application tracker needs to name the listing it points at."""

    id: uuid.UUID
    type: OpportunityType
    source: OpportunitySource
    title_i18n: dict
    organisation: str | None = None
    region: str | None = None
    deadline: datetime | None = None
    is_open: bool = True
    #: For an event, when it starts — so a tracker links it to the events page.
    starts_at: datetime | None = None


class ApplicationRead(ORMModel):
    id: uuid.UUID
    opportunity_id: uuid.UUID
    status: ApplicationStatus
    external_application_id: str | None = None
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    #: Every status it has been in, oldest first: `{status, at}`. The note a
    #: partner may attach is internal and is not passed on.
    status_history: list[dict] = []
    #: The listing, when the tracker asks for it.
    opportunity: OpportunitySummary | None = None
    #: Whether she may still withdraw it.
    can_withdraw: bool = False


# ---------------------------------------------------------------------------
# Discovery: the catalogue, one listing, and why it fits her
# ---------------------------------------------------------------------------


class EligibilityRead(BaseModel):
    """Whether she may apply, as the server can establish it. See
    `services.eligibility` — the page never decides this on its own."""

    status: EligibilityStatus
    reason: str | None = None
    age_min: int | None = None
    age_max: int | None = None
    may_apply: bool = False


class MatchReason(BaseModel):
    """One true thing that connects her to a listing.

    `skill`: a skill it asks for that she holds — with how she holds it and,
    when there is one, the course or task that gave it to her. `career`: the
    listing is on the career direction she chose. `region`: it is in her region.
    For business listings also: `own_business` (her profile says she runs one),
    `interest` (an interest she chose matches what it is about), `score` (her
    entrepreneurship score is an area worth developing).
    """

    kind: str
    skill: SkillRef | None = None
    status: SkillStatus | None = None
    source_i18n: dict = {}
    career_slug: str | None = None
    career_title_i18n: dict = {}
    dimension: str | None = None
    score: int | None = None


class MissingSkill(BaseModel):
    """A skill the listing asks for that she does not hold, and the courses on
    WomanUP that teach it — real, published and open to her. Often none."""

    skill: SkillRef
    programs: list[dict] = []  # {id, slug, title_i18n}


class OpportunityFit(BaseModel):
    """How she stands against what a listing asks for. Never a verdict: the
    organisation decides, and a listing that names no skills has no fit."""

    have: int = 0
    total: int = 0
    reasons: list[MatchReason] = []
    missing: list[MissingSkill] = []
    on_career: bool = False


class OrganizationBrief(BaseModel):
    """The organisation a WomanUP-published listing belongs to. Only public
    fields; `slug` only when its page is published."""

    id: uuid.UUID
    name: str
    kind: str
    is_verified: bool = False
    slug: str | None = None
    logo_url: str | None = None


class OpportunityCard(OpportunityRead):
    """A listing as the catalogue shows it."""

    organization: OrganizationBrief | None = None
    is_open: bool = True
    skills: list[SkillRef] = []
    #: Absent for a visitor.
    fit: OpportunityFit | None = None
    eligibility: EligibilityRead | None = None
    application_status: ApplicationStatus | None = None
    saved: bool = False


class SkillFacet(BaseModel):
    skill: SkillRef
    count: int


class OpportunityFacets(BaseModel):
    """What the filters can offer, counted over the listings that exist, so a
    filter never offers an option that returns nothing."""

    types: dict[str, int] = {}
    regions: dict[str, int] = {}
    skills: list[SkillFacet] = []


class OpportunityDiscover(BaseModel):
    items: list[OpportunityCard]
    total: int
    page: int
    size: int
    facets: OpportunityFacets = OpportunityFacets()
    signed_in: bool = False
    #: Her own counts, for the links to the tracker and the saved list.
    applications: int = 0
    saved: int = 0


class SharingRead(BaseModel):
    """What applying sends, and to whom — shown before she confirms.

    `partner` is the platform the listing came from; `fields` are exactly the
    keys `minimal_profile_payload` sends. None of them is her name, phone,
    email, marital status or children.
    """

    partner: OpportunitySource | None = None
    consent_scope: str | None = None
    consent_given: bool = False
    fields: list[str] = []
    #: For a listing an organisation published on WomanUP: who receives her
    #: profile. Nothing leaves the platform; the organisation reads it here.
    organization: OrganizationBrief | None = None


class OpportunityDetail(OpportunityCard):
    sharing: SharingRead = SharingRead()
    application: ApplicationRead | None = None
    #: Requirements the listing states that the platform cannot evaluate.
    other_requirements: list[str] = []


class ApplyIn(BaseModel):
    """Her confirmation. `consent` is her agreement to share the listed fields
    with the listing's partner platform, given on the confirm step."""

    consent: bool = False


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


class BusinessRead(BaseModel):
    """ "For your business": a few real listings, each with why it is here."""

    items: list[OpportunityCard] = []
    #: How many open business listings exist in all, for "see all".
    total_open: int = 0
    #: What the page could read about her, so it can say what would help.
    signals: dict[str, bool] = {}
