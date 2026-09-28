"""Portfolio contracts: what she can show, and what a stranger may see of it.

Two readers, two shapes. `PortfolioRead` is hers — everything, including what
is private, the status of work still waiting to be assessed, and whether she is
allowed to publish. `PublicPortfolioRead` is what anyone with the link sees,
and it is built field by field rather than by filtering hers: a public shape
that starts from the private one and removes things is one forgotten field away
from a leak.

The public shape never carries a surname, an age, a region, contact details,
a Development Score, an evaluator's identity or words, or anything she wrote in
a practical task. Those are not hidden by a flag; they are not in the schema.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    AchievementType,
    EvaluatorKind,
    ProficiencyLevel,
    SkillStatus,
    TaskStatus,
)
from app.schemas.skill import EvidenceRead, SkillRef


class AchievementRead(BaseModel):
    """Something that actually happened, and the record it came from."""

    #: `{type}:{source_id}`. Stable, and unique by construction — the source
    #: row cannot exist twice, so neither can the achievement.
    key: str
    type: AchievementType
    title_i18n: dict = {}
    #: A machine value the browser words in her language where the type needs
    #: a second line: the evidence kind that verified a skill, the outcome a
    #: partner confirmed, who assessed a task, a certificate's serial.
    detail: str | None = None
    #: `None` when the source never recorded a date. Shown as undated rather
    #: than stamped with a guess.
    earned_at: datetime | None = None
    #: True for a record she wrote herself (a project). Nobody on the platform
    #: has checked it, and it is labelled that way wherever it appears.
    self_declared: bool = False
    #: Where to look at it, when it has a page.
    href: str | None = None


class PortfolioSkill(BaseModel):
    """A skill she holds, how well it is known, and where that came from."""

    skill: SkillRef
    status: SkillStatus
    level: ProficiencyLevel | None = None
    #: The evidence behind it, named by the course, task or project that
    #: produced it. Never re-derived here — read from `services.skills`.
    evidence: list[EvidenceRead] = []


class PortfolioSkills(BaseModel):
    """Grouped, because the groups are the point. They never merge."""

    verified: list[PortfolioSkill] = []
    assessed: list[PortfolioSkill] = []
    learned: list[PortfolioSkill] = []


class PortfolioCertificate(BaseModel):
    id: uuid.UUID
    serial_number: str
    issued_at: datetime
    verification_code: str
    program_slug: str | None = None
    program_title_i18n: dict = {}
    #: What the course taught, as skills. A certificate is *learned* evidence;
    #: saying which skills it covers never makes them verified.
    skills: list[SkillRef] = []


class PortfolioPractice(BaseModel):
    """A practical task she handed in, and where it stands.

    Never carries what she wrote or what the evaluator said: those stay on the
    task page, where only she can read them.
    """

    task_slug: str
    title_i18n: dict = {}
    status: TaskStatus
    evaluated_at: datetime | None = None
    evaluator_kind: EvaluatorKind | None = None
    skills: list[SkillRef] = []


class ProjectRead(BaseModel):
    id: uuid.UUID
    title: str
    summary: str | None = None
    description: str | None = None
    skills: list[SkillRef] = []
    project_url: str | None = None
    repo_url: str | None = None
    demo_url: str | None = None
    completed_on: date | None = None
    is_public: bool = False
    created_at: datetime | None = None


class PortfolioOverview(BaseModel):
    """Counts, every one of them a count of real rows."""

    achievements: int = 0
    certificates: int = 0
    learned: int = 0
    assessed: int = 0
    verified: int = 0
    practice_passed: int = 0
    practice_submitted: int = 0
    projects: int = 0


class PortfolioSettings(BaseModel):
    is_public: bool = False
    slug: str | None = None
    published_at: datetime | None = None
    #: Which sections a public reader sees.
    sections: dict[str, bool] = {}
    #: Whether publishing is open to her at all, and why not when it is not.
    can_publish: bool = False
    #: `minor` — under 18, or an age the platform does not know.
    publish_blocked: str | None = None


class PortfolioPerson(BaseModel):
    """What the profile already says, reused rather than retyped."""

    first_name: str | None = None
    profession: str | None = None
    bio: str | None = None


class PortfolioRead(BaseModel):
    """Her whole portfolio, private parts included."""

    person: PortfolioPerson = PortfolioPerson()
    settings: PortfolioSettings = PortfolioSettings()
    overview: PortfolioOverview = PortfolioOverview()
    skills: PortfolioSkills = PortfolioSkills()
    certificates: list[PortfolioCertificate] = []
    achievements: list[AchievementRead] = []
    practice: list[PortfolioPractice] = []
    projects: list[ProjectRead] = []


class PublicPortfolioRead(BaseModel):
    """What a stranger with the link may read. Built, not filtered."""

    first_name: str | None = None
    profession: str | None = None
    bio: str | None = None
    skills: PortfolioSkills | None = None
    certificates: list[PortfolioCertificate] | None = None
    achievements: list[AchievementRead] | None = None
    practice: list[PortfolioPractice] | None = None
    projects: list[ProjectRead] = []


class PortfolioSettingsIn(BaseModel):
    is_public: bool | None = None
    sections: dict[str, bool] | None = None


class ProjectIn(BaseModel):
    """A project as she writes it. Skills are checked against the taxonomy."""

    title: str = Field(min_length=2, max_length=160)
    summary: str | None = Field(default=None, max_length=280)
    description: str | None = Field(default=None, max_length=5000)
    #: Slugs or spellings — each must resolve to an existing skill.
    skills: list[str] = Field(default=[], max_length=12)
    project_url: str | None = Field(default=None, max_length=500)
    repo_url: str | None = Field(default=None, max_length=500)
    demo_url: str | None = Field(default=None, max_length=500)
    completed_on: date | None = None
    is_public: bool = False
    #: Generated once per form in the browser; a retry with the same value is
    #: the same project.
    client_ref: uuid.UUID | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=160)
    summary: str | None = Field(default=None, max_length=280)
    description: str | None = Field(default=None, max_length=5000)
    skills: list[str] | None = Field(default=None, max_length=12)
    project_url: str | None = Field(default=None, max_length=500)
    repo_url: str | None = Field(default=None, max_length=500)
    demo_url: str | None = Field(default=None, max_length=500)
    completed_on: date | None = None
    is_public: bool | None = None
