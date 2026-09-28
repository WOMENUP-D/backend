"""Career path contracts: the catalogue, one direction, and where she stands on it.

A career path is read in four stages — learn, practice, build, explore — after
"you are here", which is her skills against the ones the work asks for. Every
course, task and listing in these responses is a real record, and the ones a
stage names are the suggestions the recommendation engine already knows how to
build, so the career page and the cabinet describe the same course the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.constants import (
    CareerCategory,
    DimensionBand,
    JourneyStage,
    ProficiencyLevel,
    ScoreDimension,
    SkillStatus,
    StageStatus,
)
from app.schemas.recommendation import (
    NextStep,
    OpportunitySuggestion,
    PathSuggestion,
    ProgramSuggestion,
    TaskSuggestion,
)
from app.schemas.skill import SkillRef


class CareerCounts(BaseModel):
    """How much the live catalogue holds for a direction right now."""

    programs: int = 0
    tasks: int = 0
    opportunities: int = 0


class CareerFit(BaseModel):
    """Her standing on a direction, for the catalogue card. Absent for a visitor."""

    #: Skills the work asks for that she holds, with any status.
    have: int
    total: int
    chosen: bool = False


class CareerPathRead(BaseModel):
    """A direction as the catalogue lists it."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    summary_i18n: dict = {}
    category: CareerCategory
    level: ProficiencyLevel | None = None
    skills: list[SkillRef] = []
    #: Development Score dimensions the direction builds: its skills' own.
    dimensions: list[ScoreDimension] = []
    counts: CareerCounts = CareerCounts()
    fit: CareerFit | None = None
    #: The one direction her skills are already closest to. Never more than
    #: one, and never set when she holds none of any direction's skills.
    suggested: bool = False


class CareerSkill(BaseModel):
    """One skill the work asks for, whether she has it, and where to get it."""

    skill: SkillRef
    #: Her status on it. None when she does not hold it — or for a visitor.
    status: SkillStatus | None = None
    #: Published courses that teach it, tasks that practise it and open
    #: listings that ask for it. A skill with all three at zero is one WomanUP
    #: cannot help with yet, and the page says so.
    programs: int = 0
    tasks: int = 0
    opportunities: int = 0


class StageRead(BaseModel):
    """One stage of the journey, with her standing on it."""

    stage: JourneyStage
    #: None for a visitor: there is nobody to have a standing.
    status: StageStatus | None = None
    #: Why a stage is unavailable — `no_courses`, `no_tasks`, `no_listings`,
    #: `adults_only` — so the page explains rather than going blank.
    reason: str | None = None
    #: The stage's own measure of progress, e.g. skills learned of those the
    #: catalogue teaches, tasks passed of those that exist.
    done: int = 0
    total: int = 0


class DimensionNote(BaseModel):
    """A Development Score dimension this direction builds that needs her."""

    dimension: ScoreDimension
    score: int
    band: DimensionBand


class CareerEvidence(BaseModel):
    """What she can already show for this direction, from her portfolio."""

    certificates: list[dict] = []  # {title_i18n, serial_number}
    passed_tasks: list[dict] = []  # {slug, title_i18n}
    projects: list[dict] = []  # {id, title}


class CareerJourney(BaseModel):
    """Where she stands on a direction. Only ever built for the caller herself."""

    chosen: bool = False
    chosen_at: datetime | None = None
    have: int = 0
    total: int = 0
    #: The first stage that is neither done nor unavailable. None when every
    #: stage WomanUP can offer is done.
    current: JourneyStage | None = None
    #: What to do now, in the engine's own vocabulary.
    next_step: NextStep | None = None
    dimension_notes: list[DimensionNote] = []
    evidence: CareerEvidence = CareerEvidence()
    #: Applications she has sent to listings on this direction.
    applications: int = 0


class CareerPathDetail(CareerPathRead):
    """One direction, read in stages, with her journey when she is signed in."""

    description_i18n: dict = {}
    learning_path: PathSuggestion | None = None
    skill_details: list[CareerSkill] = []
    programs: list[ProgramSuggestion] = []
    tasks: list[TaskSuggestion] = []
    opportunities: list[OpportunitySuggestion] = []
    stages: list[StageRead] = []
    journey: CareerJourney | None = None


class CareerChoiceIn(BaseModel):
    slug: str
