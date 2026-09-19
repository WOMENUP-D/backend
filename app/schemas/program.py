"""Programme catalogue, lesson, enrollment and certificate schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.constants import (
    EnrollmentStatus,
    Language,
    LessonKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
)
from app.schemas.common import ORMModel
from app.schemas.skill import SkillRef


class ProgramLessonRead(ORMModel):
    """A lesson as the contents list shows it — without its body.

    The body is the largest thing in a course and is read one lesson at a time;
    shipping every lesson's text to draw a table of contents is what makes a
    course page slow on a regional connection.
    """

    id: uuid.UUID
    order_index: int
    slug: str
    title_i18n: dict
    kind: LessonKind
    duration_minutes: int | None = None


class ProgramLessonDetail(ProgramLessonRead):
    """One lesson, opened."""

    module_id: uuid.UUID
    blocks: list = []
    resources: list = []
    media_url: str | None = None


class ProgramModuleRead(ORMModel):
    id: uuid.UUID
    order_index: int
    title_i18n: dict
    # What the module actually covers. Held back until now, which left the
    # course page listing module names it could not explain.
    content_i18n: dict = {}
    media_url: str | None = None
    duration_minutes: int | None = None
    lessons: list[ProgramLessonRead] = []


class ProgramBase(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=160)
    title_i18n: dict
    goal_i18n: dict = {}
    description_i18n: dict = {}
    category: ProgramCategory
    format: ProgramFormat = ProgramFormat.VIDEO
    language: Language = Language.UZ
    # How demanding the course is. Absent rather than guessed for a course
    # nobody has classified.
    level: ProficiencyLevel | None = None
    target_age_min: int | None = Field(default=None, ge=10, le=100)
    target_age_max: int | None = Field(default=None, ge=10, le=100)
    target_regions: list[str] = []
    target_segments: list[str] = []
    duration_hours: float | None = Field(default=None, gt=0)
    duration_weeks: int | None = Field(default=None, gt=0)
    prerequisites: list[str] = []
    learning_outcomes: list = Field(default=[], max_length=7)
    skills_taught: list[str] = []
    assessment_type: str | None = None
    has_certificate: bool = False
    next_step: str | None = None
    provider: str | None = None

    @field_validator("learning_outcomes")
    @classmethod
    def outcomes_within_standard(cls, value: list) -> list:
        """Section 04A fixes the card standard at 3-7 measurable outcomes."""
        if value and not 3 <= len(value) <= 7:
            raise ValueError("a programme card needs between 3 and 7 learning outcomes")
        return value


class ProgramCreate(ProgramBase):
    pass


class ProgramUpdate(BaseModel):
    title_i18n: dict | None = None
    description_i18n: dict | None = None
    category: ProgramCategory | None = None
    format: ProgramFormat | None = None
    level: ProficiencyLevel | None = None
    is_published: bool | None = None


class ProgramRead(ProgramBase, ORMModel):
    id: uuid.UUID
    cover_url: str | None = None
    is_published: bool
    published_at: datetime | None = None
    created_at: datetime


class ProgramDetail(ProgramRead):
    modules: list[ProgramModuleRead] = []
    # The same skills as `skills_taught`, resolved against the taxonomy so the
    # card can name them in the reader's language. The raw column stays as the
    # author wrote it.
    skills: list[SkillRef] = []


class ProgramFilter(BaseModel):
    category: ProgramCategory | None = None
    format: ProgramFormat | None = None
    language: Language | None = None
    region: str | None = None
    search: str | None = None
    has_certificate: bool | None = None


class EnrollmentRead(ORMModel):
    id: uuid.UUID
    program_id: uuid.UUID
    status: EnrollmentStatus
    progress_percent: int
    # What she has ticked. The learning section marks the contents from these,
    # rather than keeping a second copy of completion in the browser.
    completed_modules: list = []
    completed_lessons: list = []
    final_score: float | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_activity_at: datetime | None = None


class EnrollmentDetail(EnrollmentRead):
    """An enrollment with the course it belongs to.

    The learning dashboard lists what she is studying; without the programme
    beside it every row would cost another request to name the course.
    """

    program: ProgramRead | None = None


class ProgressUpdate(BaseModel):
    module_id: uuid.UUID
    completed: bool = True


class LessonProgressUpdate(BaseModel):
    lesson_id: uuid.UUID
    completed: bool = True


class CertificateRead(ORMModel):
    id: uuid.UUID
    serial_number: str
    issued_at: datetime
    file_url: str | None = None
    verification_code: str
    # Which course it certifies — a list of serial numbers names nothing.
    program_id: uuid.UUID | None = None
    program_title_i18n: dict = {}
