"""Admin dashboard, KPI and notification schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.constants import NotificationChannel, NotificationTrigger, Region
from app.schemas.common import ORMModel


class DashboardFilter(BaseModel):
    """Filters listed in section 09."""

    date_from: date | None = None
    date_to: date | None = None
    region: Region | None = None
    district: str | None = None
    age_group: str | None = None
    segment: str | None = None
    program_category: str | None = None
    integration_source: str | None = None
    outcome_status: str | None = None


class KpiValue(BaseModel):
    key: str
    label: str
    #: None when the denominator is 0 — "0 of 0" is not "0%".
    value: float | None
    unit: str = "percent"
    numerator: int | None = None
    denominator: int | None = None
    target: float | None = None


class KpiSnapshot(BaseModel):
    """The MVP KPI set: activation, assessment completion, plan adoption,
    programme completion, opportunity/employment/business conversion,
    commerce activation, D30 retention."""

    generated_at: datetime
    filters: DashboardFilter
    kpis: list[KpiValue]


class RegionCoverage(BaseModel):
    region: Region
    registered: int
    active: int
    #: Not computed per region; None rather than an invented 0.0.
    avg_development_score: float | None = None


class TrafficDay(BaseModel):
    day: date
    visitors: int
    views: int


class TrafficPath(BaseModel):
    path: str
    views: int
    visitors: int


class TrafficReport(BaseModel):
    """Who came to the site, as opposed to what registered users achieved."""

    visitors_today: int
    visitors_7d: int
    visitors_30d: int
    views_today: int
    views_30d: int
    signed_in_share: float
    returning_share: float
    by_day: list[TrafficDay]
    top_paths: list[TrafficPath]


class DashboardOverview(BaseModel):
    total_registered: int
    active_users: int
    onboarding_completed: int
    programs_completed: int
    employed_via_edu_job: int
    businesses_via_invest_hub: int
    sellers_via_commerce: int
    mentor_sessions: int
    at_risk_users: int
    avg_development_score: float
    coverage_by_region: list[RegionCoverage]


class NotificationRead(ORMModel):
    id: uuid.UUID
    channel: NotificationChannel
    trigger: NotificationTrigger | None = None
    title: str
    body: str
    action_url: str | None = None
    sent_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime


class NotificationPreferenceUpdate(BaseModel):
    in_app_enabled: bool | None = None
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    push_enabled: bool | None = None
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)


class ConsentUpdate(BaseModel):
    scope: str
    accepted: bool
    policy_version: str = "1.0"


class ConsentRead(ORMModel):
    scope: str
    accepted: bool
    policy_version: str
    accepted_at: datetime
