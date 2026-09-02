"""Programme catalogue, enrollment and certificate schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.constants import (
    EnrollmentStatus,
    Language,
    ProgramCategory,
    ProgramFormat,
)
from app.schemas.common import ORMModel


class ProgramModuleRead(ORMModel):
    id: uuid.UUID
    order_index: int
    title_i18n: dict
    # What the module actually covers. Held back until now, which left the
    # course page listing module names it could not explain.
    content_i18n: dict = {}
    media_url: str | None = None
    duration_minutes: int | None = None


class ProgramBase(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=160)
    title_i18n: dict
    goal_i18n: dict = {}
    description_i18n: dict = {}
    category: ProgramCategory
    format: ProgramFormat = ProgramFormat.VIDEO
    language: Language = Language.UZ
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
    is_published: bool | None = None


class ProgramRead(ProgramBase, ORMModel):
    id: uuid.UUID
    cover_url: str | None = None
    is_published: bool
    published_at: datetime | None = None
    created_at: datetime


class ProgramDetail(ProgramRead):
    modules: list[ProgramModuleRead] = []


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
    final_score: float | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ProgressUpdate(BaseModel):
    module_id: uuid.UUID
    completed: bool = True


class CertificateRead(ORMModel):
    id: uuid.UUID
    serial_number: str
    issued_at: datetime
    file_url: str | None = None
    verification_code: str
