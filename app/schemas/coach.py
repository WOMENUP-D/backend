"""AI Coach contracts: where she stands, and what the Coach is allowed to name.

Two shapes, and the split between them is the architecture.

`CoachContextRead` is computed without a model call — the Development Score,
the canonical skills, the real enrollments, the real paths, and the ranked next
steps the Step 2 engine already produced. The cabinet renders it directly, so
"your current situation" and "your next step" are always true and always fast.

`CoachReply` is what comes back from a conversation. Its `references` are
resolved against the same real records *after* the model has spoken, so a
reference that reaches the browser is one the database holds.

Suggested questions travel as a key plus the facts they quote, like every other
explanation in this codebase — the portal is read in four locales and a
question composed on the server would be stuck in one of them.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.constants import ProficiencyLevel, ScoreDimension, SkillStatus
from app.schemas.event import EventCard
from app.schemas.recommendation import NextStep, OpportunitySuggestion
from app.schemas.skill import SkillGapRead, SkillRef


class CoachSkill(BaseModel):
    """A skill she holds, and how well the platform knows it."""

    skill: SkillRef
    status: SkillStatus | None = None
    level: ProficiencyLevel | None = None
    evidence_count: int = 0


class CoachSkills(BaseModel):
    """Her skills, grouped by what backs them.

    Grouped rather than flat because the groups are the point: a course makes a
    skill *learned*, and only a person or a placement makes it *verified*.
    """

    verified: list[CoachSkill] = []
    assessed: list[CoachSkill] = []
    learned: list[CoachSkill] = []
    self_reported: list[CoachSkill] = []
    gaps: list[SkillGapRead] = []


class CoachDimension(BaseModel):
    dimension: ScoreDimension
    current: float
    band: str


class CoachScore(BaseModel):
    """Her Development Score as the Coach reads it."""

    assessed: bool = False
    composite: float | None = None
    dimensions: list[CoachDimension] = []
    strengths: list[ScoreDimension] = []
    focus: list[ScoreDimension] = []


class CoachCourse(BaseModel):
    """A course she is actually enrolled in, with the server's own progress."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    progress_percent: int
    status: str
    # The lesson she would open next, when the course has lessons written.
    next_lesson_slug: str | None = None
    next_lesson_title_i18n: dict = {}


class CoachLearning(BaseModel):
    in_progress: list[CoachCourse] = []
    completed: list[CoachCourse] = []
    certificates: int = 0


class CoachPath(BaseModel):
    """A route she is on, with the derived progress the path layer computes."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    status: str
    percent: int
    completed_items: int
    required_items: int
    next_program_slug: str | None = None
    next_program_title_i18n: dict = {}


class CoachPractice(BaseModel):
    """Her practice, as the Coach reads it.

    Counts and the few tasks worth naming — not the whole history. The Coach
    needs to know she has something to fix, not every attempt she ever made.
    """

    open_tasks: int = 0
    awaiting_review: int = 0
    needs_improvement: int = 0
    passed: int = 0
    #: The task she should look at next, and why it is that one: something to
    #: fix comes before something to start.
    next_task_slug: str | None = None
    next_task_title_i18n: dict = {}
    #: The evaluator's own words on her most recent needs-improvement attempt.
    #: Quoted, never paraphrased, and absent when nobody wrote any.
    last_feedback: str = ""
    #: Published tasks practising a skill she is missing. Real records only.
    available_slugs: list[str] = []


class CoachPortfolio(BaseModel):
    """What she can show, as the Coach reads it.

    Titles of records that exist — the certificates she holds, the projects she
    wrote up, the achievements the platform can back. The Coach may name these
    and nothing else; a certificate that is not in this list does not exist.
    """

    certificates: list[dict] = []
    projects: list[str] = []
    achievements: int = 0
    is_public: bool = False


class CoachCareerOption(BaseModel):
    """One direction the catalogue offers, and how many of its skills she holds."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    have: int = 0
    total: int = 0


class CoachCareer(BaseModel):
    """Her chosen direction, as the career page reads it. Real records only."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    have: int = 0
    total: int = 0
    #: Skills the direction needs that she does not hold yet.
    missing: list[SkillRef] = []
    #: The stage she is on (`JourneyStage`), or None when every stage WomanUP
    #: can offer is done.
    current: str | None = None
    next_kind: str | None = None
    #: Open listings on this direction. Zero for a woman under 18, who is not
    #: shown them.
    opportunities: int = 0
    listings_withheld: bool = False
    #: Development Score dimensions the direction builds that need her.
    weak_dimensions: list[ScoreDimension] = []


class CoachApplication(BaseModel):
    """An application she sent, as the tracker shows it."""

    opportunity_id: uuid.UUID
    title_i18n: dict
    status: str
    submitted_at: str | None = None


class CoachSuggestion(BaseModel):
    """A question worth asking, chosen from her actual state.

    `key` names the phrasing in the message catalogue; `params` carries the
    facts it quotes — a course title, a skill name — already resolved into the
    language the context was requested in.
    """

    key: str
    params: dict[str, str] = {}


class CoachContextRead(BaseModel):
    """Everything the Coach knows, as the browser renders it. No model call."""

    personalised: bool = False
    name: str | None = None
    language: str = "uz"
    score: CoachScore = CoachScore()
    skills: CoachSkills = CoachSkills()
    learning: CoachLearning = CoachLearning()
    practice: CoachPractice = CoachPractice()
    portfolio: CoachPortfolio = CoachPortfolio()
    paths: list[CoachPath] = []
    #: The directions the catalogue offers, and the one she chose.
    careers: list[CoachCareerOption] = []
    career: CoachCareer | None = None
    applications: list[CoachApplication] = []
    # The deterministic engine's own ranking, reused verbatim.
    next_steps: list[NextStep] = []
    opportunities: list[OpportunitySuggestion] = []
    #: Events worth her time, each with its reasons, and the ones ahead she
    #: saved, registered for or set a reminder on (`services.events`).
    events: list[EventCard] = []
    my_events: list[EventCard] = []
    suggestions: list[CoachSuggestion] = []


class CoachReference(BaseModel):
    """A real WomanUP record the answer points at.

    Only ever built from a record that was in the offer list, so a reference
    that reaches the browser is one the database holds.
    """

    kind: str  # program | learning_path | practical_task | opportunity | career_path | event
    id: uuid.UUID
    slug: str | None = None
    title_i18n: dict = {}


class CoachReply(BaseModel):
    """One coaching answer."""

    message: str
    trace_id: str
    personalised: bool
    # True when the context could not answer — said out loud rather than filled
    # in with something plausible.
    unsupported: bool = False
    escalated: bool = False
    escalation_reason: str | None = None
    # Whether a model wrote this, or the deterministic fallback did.
    generated: bool = True
    next_step: CoachReference | None = None
    references: list[CoachReference] = []


class CoachAsk(BaseModel):
    """A question for the Coach, optionally about one listing.

    `opportunity_id` is the listing page she asked from. It is looked up — a
    real, visible record or nothing — and its fit against her is computed on
    the server before the model sees a word of it.
    """

    message: str = Field(min_length=2, max_length=1000)
    language: str | None = Field(default=None, max_length=8)
    opportunity_id: uuid.UUID | None = None
