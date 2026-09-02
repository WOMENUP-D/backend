"""AI Assistant request and response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import AgeBand, AssistantSection


class SourceRef(BaseModel):
    """A quoted passage from the approved knowledge base."""

    document_title: str
    excerpt: str
    chunk_id: str


class AssistantAsk(BaseModel):
    message: str = Field(min_length=2, max_length=1000)
    # "auto" lets the router choose; the explicit values are for the section
    # buttons, so a woman who knows what she wants can skip the routing call.
    route: str = Field(default="auto", max_length=16)
    # The UI knows which locale she is reading in; without it the answer follows
    # her stored profile language and a question typed in Russian comes back in
    # Uzbek.
    language: str | None = Field(default=None, max_length=8)


class DetailAsk(AssistantAsk):
    """Second pass: expand an answer already given into cards."""

    answer: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="general", max_length=32)


class ProfessionCard(BaseModel):
    """One suggested profession, with everything section 1 of the brief asks
    for: what it is, what it needs, how to get there and whether it is wanted."""

    title: str
    summary: str = ""
    skills: list[str] = []
    learn: list[str] = []
    path: list[str] = []
    demand: str = ""
    why_you: str = ""
    program_ids: list[str] = []


class RoadmapStep(BaseModel):
    title: str
    detail: str = ""
    program_ids: list[str] = []


class ProgramRef(BaseModel):
    """A real programme from the catalogue, so a suggestion is one click from
    enrolment rather than a name the user has to go and search for."""

    id: str
    slug: str
    title: str
    category: str


class AssistantReply(BaseModel):
    answer: str
    section: AssistantSection
    kind: str = "general"
    trace_id: str
    personalised: bool
    escalated: bool = False
    escalation_reason: str | None = None
    see_a_doctor: bool = False
    professions: list[ProfessionCard] = []
    roadmap: list[RoadmapStep] = []
    next_actions: list[str] = []
    programs: list[ProgramRef] = []
    sources: list[SourceRef] = []
    confidence: float | None = None
    is_uncertain: bool = False
    route: str = "education"
    guest_questions_left: int | None = None


class AssistantProfile(BaseModel):
    """What the assistant currently knows — shown in the UI so personalisation
    is legible rather than mysterious, and so she can see what to fill in."""

    personalised: bool
    age_band: AgeBand
    age: int | None = None
    name: str | None = None
    interests: list[str] = []
    goals: list[str] = []
    directions: list[str] = []
    in_progress: list[str] = []
    plan_progress: int | None = None
    missing: list[str] = []


class OnboardingIn(BaseModel):
    """Everything asked at sign-up, and no more.

    Name, surname, age and region identify her enough to be useful; interests,
    goal and direction are what the assistant personalises on. Only the first
    four are required — the rest can be filled in later without blocking her
    from getting into the portal.
    """

    name: str = Field(min_length=1, max_length=120)
    surname: str = Field(default="", max_length=120)
    age: int = Field(ge=10, le=100)
    region: str | None = Field(default=None, max_length=40)
    interests: list[str] = Field(default_factory=list, max_length=12)
    goal: str = Field(default="", max_length=255)
    direction: str = Field(default="", max_length=60)
    consent_ai_personalisation: bool = True
