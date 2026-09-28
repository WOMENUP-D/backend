"""Results & impact: what the admin dashboard reads.

Three rules shape every model here:

* A count is an `int`. A metric the platform does not record is `None` —
  never `0`, which would claim that nothing happened.
* A rate carries its numerator and denominator, and its `value` is `None`
  when the denominator is 0: "0 of 0" is not "0%".
* A demographic group smaller than `MIN_GROUP` people has `value=None` and
  `suppressed=True`, so a small village's two women cannot be singled out —
  and when that would leave one hidden group recoverable from the total, the
  next smallest group is hidden with it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.core.constants import Region
from app.schemas.skill import SkillRef


class Period(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    #: The step of every series in the response: day, week or month.
    bucket: str = "month"


class Scope(BaseModel):
    period: Period
    #: Set when the figures are limited to one region — by the filter, or
    #: because a regional coordinator only ever sees her own.
    region: Region | None = None
    generated_at: datetime


class Metric(BaseModel):
    key: str
    #: None when the platform does not record it.
    value: int | None


class Rate(BaseModel):
    key: str
    numerator: int
    denominator: int
    #: A percentage, or None when the denominator is 0.
    value: float | None


class Point(BaseModel):
    bucket: date
    value: int


class Series(BaseModel):
    key: str
    points: list[Point] = []


class Group(BaseModel):
    key: str
    value: int | None
    suppressed: bool = False


class Named(BaseModel):
    """A record counted by name — a programme, a skill, an event."""

    id: uuid.UUID | None = None
    title_i18n: dict = {}
    value: int


class ResultsOverview(BaseModel):
    scope: Scope
    #: Participant accounts that exist at the end of the period.
    participants: int
    metrics: list[Metric]
    #: The first signed-in visit the platform recorded: "active in the
    #: period" cannot reach further back than this.
    activity_since: date | None = None
    registrations: Series
    regions: list[Group]
    ages: list[Group]


class ResultsLearning(BaseModel):
    scope: Scope
    metrics: list[Metric]
    rates: list[Rate]
    series: list[Series]
    #: Evaluated practical-task attempts by who evaluated them.
    evaluations: list[Group]


class ProgrammeRow(BaseModel):
    id: uuid.UUID
    slug: str
    title_i18n: dict
    is_published: bool
    enrolled: int
    in_progress: int
    completed: int
    #: Of enrollments started in the period, the share completed so far.
    completion: Rate
    certificates: int
    tasks_submitted: int


class ProgrammeTable(BaseModel):
    scope: Scope
    items: list[ProgrammeRow]
    total: int
    page: int
    size: int


class SkillCount(BaseModel):
    skill: SkillRef
    value: int


class ResultsSkills(BaseModel):
    scope: Scope
    #: Evidence recorded in the period, by the status it supports.
    evidence: list[Group]
    #: The same evidence by where it came from (course, certificate, mentor…).
    evidence_kinds: list[Group]
    #: For each status, the skills most people gained evidence for.
    top: dict[str, list[SkillCount]]
    #: Every skill she holds today, by its status and by its level.
    statuses: list[Group]
    levels: list[Group]


class DimensionReading(BaseModel):
    dimension: str
    people: int
    #: None when fewer than MIN_GROUP people have a score in it.
    average: float | None
    #: People below 40, 40-69 and 70+. Suppressed like any group (see
    #: `services.results._suppressed`): "one woman in this region is strong
    #: in X" singles her out.
    focus: int | None
    developing: int | None
    strong: int | None


class ResultsScore(BaseModel):
    scope: Scope
    #: People with a Development Score — the sample every figure below is of.
    people: int
    #: True below SMALL_SAMPLE: the figures describe these people, not women
    #: in general.
    small_sample: bool
    average: float | None
    distribution: list[Group]
    dimensions: list[DimensionReading]


class ResultsOpportunities(BaseModel):
    scope: Scope
    metrics: list[Metric]
    applications_by_type: list[Group]
    applications_by_status: list[Group]
    applications_by_source: list[Group]
    saved_by_type: list[Group]
    open_by_type: list[Group]
    #: Only what a partner or an organisation recorded. An application is not
    #: a job, and a course is not one either.
    outcomes: list[Group]
    rates: list[Rate]


class ResultsEvents(BaseModel):
    scope: Scope
    metrics: list[Metric]
    by_type: list[Group]
    by_format: list[Group]
    top: list[Named]
    series: list[Series]
