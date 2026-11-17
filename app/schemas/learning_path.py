"""Learning path contracts: the route, its steps, and where she stands on it.

Everything a screen needs to draw a path arrives in one object, and almost none
of it is stored. Target skills, hours and weeks are read off the courses in the
path; progress, status and what comes next are read off her enrollments. The
API composes them so no browser has to, and so two screens can never compute
the same path differently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from app.core.constants import ProficiencyLevel, ScoreDimension
from app.schemas.program import ProgramRead
from app.schemas.skill import SkillRef


class PathItemStatus(StrEnum):
    """Where one step of a path stands, for her.

    `locked` is a reading of the route, not a punishment: an earlier required
    course has not been finished. It never applies to a course she is already
    enrolled in — the catalogue is open, and a path may not lock a woman out of
    something she has already begun.
    """

    LOCKED = "locked"
    AVAILABLE = "available"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class PathStatus(StrEnum):
    """Where the path as a whole stands. Derived, never stored."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class LearningPathItemRead(BaseModel):
    """One step: an existing programme, at a position, with her state on it."""

    program: ProgramRead
    order_index: int
    is_required: bool
    # What this step teaches, resolved to the taxonomy so it reads in her
    # language. The programme's own column keeps the author's wording.
    skills: list[SkillRef] = []

    status: PathItemStatus = PathItemStatus.AVAILABLE
    # The server's number for her enrollment, or None before she enrols: there
    # is no progress on a course nobody has started.
    progress_percent: int | None = None
    enrollment_id: uuid.UUID | None = None


class LearningPathProgress(BaseModel):
    """How far along a path is, entirely derived from her enrollments."""

    status: PathStatus = PathStatus.NOT_STARTED
    # Share of the *required* steps finished. Optional steps widen a route;
    # counting them would make finishing everything required read as "80%".
    percent: int = 0
    completed_items: int = 0
    required_items: int = 0
    total_items: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # The step she is on and the one after it, so a screen never has to work
    # out either by scanning the list.
    current_program_id: uuid.UUID | None = None
    next_program_id: uuid.UUID | None = None


class LearningPathRead(BaseModel):
    """A path as the catalogue lists it."""

    id: uuid.UUID
    slug: str
    title_i18n: dict
    description_i18n: dict = {}
    dimension: ScoreDimension | None = None
    level: ProficiencyLevel | None = None

    program_count: int = 0
    required_count: int = 0
    # Summed from the courses in the path; None when none of them says.
    total_hours: float | None = None
    total_weeks: int | None = None
    # The union of what its courses teach, in path order, without repeats.
    skills: list[SkillRef] = []
    # Of those, the ones she does not hold yet. Empty for a visitor.
    new_skills: list[SkillRef] = []

    progress: LearningPathProgress = LearningPathProgress()


class LearningPathDetail(LearningPathRead):
    """The path opened: its steps, in order."""

    items: list[LearningPathItemRead] = []


# --- personal directions -----------------------------------------------------


class InterestRef(BaseModel):
    """One answer to the diagnostic's interests question, with its words."""

    id: str
    label_i18n: dict[str, str] = {}


class LearningDirectionRead(BaseModel):
    """A field she is interested in, with the catalogue courses that lead into
    it in order. A reading, not a path she is on: nothing is stored."""

    slug: str
    title_i18n: dict[str, str]
    summary_i18n: dict[str, str]
    #: Why it is shown: one of her interests, one of her lowest-scoring areas,
    #: or — when neither leads anywhere — one of the best-stocked directions.
    reason: Literal["interest", "weakest_dimension", "popular"]
    interest: InterestRef | None = None
    dimension: ScoreDimension | None = None
    #: The catalogue topic that opens every course of the field (`/programs?skill=`).
    topic: str | None = None
    #: How many published courses the direction holds; `courses` is the first few.
    course_total: int
    courses: list[ProgramRead]


class LearningDirectionsForMe(BaseModel):
    #: No completed diagnostic yet — nothing to read interests from.
    needs_diagnostic: bool
    #: Where the directions came from as a whole; None when none were found.
    basis: Literal["interests", "weakest_dimension", "popular"] | None = None
    interests: list[InterestRef] = []
    #: Interests she chose that the catalogue cannot serve yet.
    unmatched_interests: list[InterestRef] = []
    directions: list[LearningDirectionRead] = []
    assessment_id: uuid.UUID | None = None
