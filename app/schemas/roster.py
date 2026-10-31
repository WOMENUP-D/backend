"""The participant roster: one row per woman, how much she uses the portal and
where she stands.

Individual-level by design, so the rules are stricter than for the aggregate
dashboards. Identifiers (a name, a masked address) are present only when the
caller is an administrator; everyone else sees a pseudonymous tag that is the
same for the same account on these screens and meaningless anywhere else.
Opening a card and exporting the table are written to the audit log.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.core.constants import EnrollmentStatus, Language, Region, ScoreDimension, UserStatus


class RosterRow(BaseModel):
    id: uuid.UUID
    #: Six characters derived from the account id under the app secret.
    tag: str
    #: Administrators only; None for every other role and for deleted accounts.
    name: str | None
    #: Administrators only, masked: "d*****@example.com".
    email: str | None
    region: Region | None
    status: UserStatus
    language: Language
    joined_at: datetime
    last_active_at: datetime | None
    onboarded: bool

    # How much she uses the portal.
    views: int
    sessions: int
    active_days: int

    # Where she stands.
    score: float | None
    score_measured_at: datetime | None
    diagnostics: int
    last_diagnostic_at: datetime | None
    courses_enrolled: int
    courses_completed: int
    certificates: int
    plan_accepted: bool
    ai_requests: int
    applications: int
    cv_entries: int


class RosterScope(BaseModel):
    region: Region | None
    #: Whether rows carry a name and a masked address.
    identifiers: bool
    #: Signed-in page views are recorded since this day; usage figures cannot
    #: reach further back.
    views_since: date | None
    generated_at: datetime


class Roster(BaseModel):
    scope: RosterScope
    items: list[RosterRow]
    total: int
    page: int
    size: int


class DimensionScore(BaseModel):
    dimension: ScoreDimension
    baseline: float
    current: float
    target: float | None


class CourseLine(BaseModel):
    program_id: uuid.UUID
    slug: str
    title_i18n: dict
    status: EnrollmentStatus
    progress_percent: int
    started_at: datetime | None
    completed_at: datetime | None
    certificate: bool


class EventLine(BaseModel):
    name: str
    occurred_at: datetime
    title_i18n: dict | None


class RosterCard(BaseModel):
    row: RosterRow
    dimensions: list[DimensionScore]
    courses: list[CourseLine]
    events: list[EventLine]
