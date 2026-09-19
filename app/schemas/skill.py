"""Skill contracts: the vocabulary, what she holds, and what she is missing.

Every skill crosses the API as a `SkillRef` — a stable slug plus names in every
language — rather than as the word an author happened to type. `label` carries
that original word, so a skill the taxonomy has never seen is still shown as
written instead of disappearing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    EvidenceKind,
    ProficiencyLevel,
    ScoreDimension,
    SkillCategory,
    SkillStatus,
)
from app.schemas.common import ORMModel


class SkillRef(BaseModel):
    """A skill as everything else refers to it."""

    # None for a label with no taxonomy entry yet: the browser falls back to
    # `label`, which is what the author or the partner platform wrote.
    slug: str | None = None
    name_i18n: dict = {}
    label: str
    category: SkillCategory | None = None
    dimensions: list[ScoreDimension] = []


class SkillRead(ORMModel):
    """A catalogue entry, for the skills list and for editors."""

    id: uuid.UUID
    slug: str
    name_i18n: dict
    category: SkillCategory
    dimensions: list[str] = []
    aliases: list[str] = []
    is_curated: bool
    is_active: bool


class SkillCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    name_i18n: dict
    category: SkillCategory = SkillCategory.PROFESSIONAL
    dimensions: list[ScoreDimension] = []
    aliases: list[str] = []
    is_active: bool = True


class SkillUpdate(BaseModel):
    name_i18n: dict | None = None
    category: SkillCategory | None = None
    dimensions: list[ScoreDimension] | None = None
    aliases: list[str] | None = None
    is_active: bool | None = None


class EvidenceRead(BaseModel):
    """One reason the platform believes she has a skill."""

    kind: EvidenceKind
    # The status this single piece of evidence can support on its own.
    supports: SkillStatus
    level: ProficiencyLevel | None = None
    score: float | None = None
    # What produced it — a course title, a certificate serial — so the profile
    # can name it rather than showing a bare identifier.
    title_i18n: dict = {}
    reference: str | None = None
    occurred_at: datetime | None = None


class UserSkillRead(BaseModel):
    """A skill she holds, with how strongly it is backed."""

    skill: SkillRef
    status: SkillStatus | None = None
    level: ProficiencyLevel | None = None
    evidence_count: int = 0
    evidence: list[EvidenceRead] = []
    # Progress of a course she is taking that teaches this skill, when there is
    # one. Read from the enrollment, never stored twice.
    progress_percent: int | None = None


class SkillGapRead(BaseModel):
    """A skill she does not hold that the platform can actually teach her."""

    skill: SkillRef
    dimension: ScoreDimension | None = None
    # How much of the live offer needs it: courses that teach it, listings that
    # ask for it. A gap nothing can close is not shown.
    programs: int = 0
    opportunities: int = 0
    program_id: uuid.UUID | None = None
    program_title_i18n: dict = {}


class SkillProfileRead(BaseModel):
    """The Skills section of her cabinet: what she has, and what to build next."""

    skills: list[UserSkillRead] = []
    improve: list[SkillGapRead] = []
