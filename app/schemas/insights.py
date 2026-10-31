"""Product analytics: what the admin Analytics dashboard reads.

Same rules as `schemas.results`: a count is an `int`; a figure the platform
has never recorded is `None`, never `0`; a rate carries its numerator and
denominator and is `None` over 0. One more rule here, because several columns
and every event only exist since revision 0029: a figure whose source started
recording on a date says so in `since`, and the dashboard writes "collected
since <date>" under it instead of pretending the history exists.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.results import Group, Period, Rate, Series


class Scope(BaseModel):
    period: Period
    #: The calendar every day-based figure is cut on.
    timezone: str = "Asia/Tashkent"
    #: Staff accounts' own browsing and actions are left out unless asked for.
    includes_staff: bool = False
    generated_at: datetime
    #: True when the answer came from the short in-process cache.
    cached: bool = False
    #: How old a cached answer may be, in seconds. 0 means computed just now.
    cache_ttl_seconds: int = 0


class Figure(BaseModel):
    key: str
    #: None when the platform has never recorded this.
    value: int | None
    #: The first day the source of this figure has data for. None when it has
    #: existed as long as the platform, or has never recorded anything.
    since: date | None = None
    #: "period" counts the selected days; "now" is the state of things today;
    #: "live" is the last few minutes.
    kind: str = "period"


class Ratio(BaseModel):
    """An average, as opposed to a share: views per session, say."""

    key: str
    value: float | None
    numerator: int
    denominator: int


class Live(BaseModel):
    online_now: int
    online_signed_in: int
    #: How many minutes of silence end a presence.
    window_minutes: int
    as_of: datetime


class Overview(BaseModel):
    scope: Scope
    live: Live
    today: list[Figure]
    period: list[Figure]
    active: list[Figure]
    series: list[Series]


class PathStat(BaseModel):
    path: str
    views: int
    visitors: int


class Traffic(BaseModel):
    scope: Scope
    figures: list[Figure]
    ratios: list[Ratio]
    series: list[Series]
    pages: list[PathStat]
    landing: list[PathStat]
    exits: list[PathStat]
    referrers: list[Group]
    devices: list[Group]
    browsers: list[Group]
    systems: list[Group]
    locales: list[Group]
    #: The first day each newer column has data for; None if it never has.
    collected: dict[str, date | None]


class Stage(BaseModel):
    key: str
    value: int | None
    since: date | None = None
    #: Share of the stage before it, as a percentage. None over 0 or unknown.
    of_previous: float | None = None
    #: Share of the first stage, as a percentage.
    of_first: float | None = None


class Accounts(BaseModel):
    scope: Scope
    figures: list[Figure]
    rates: list[Rate]
    series: list[Series]
    by_status: list[Group]
    by_provider: list[Group]
    by_language: list[Group]
    registration: list[Stage]


class CohortCell(BaseModel):
    week: int
    value: int
    share: float | None
    #: False while the week it describes has not ended yet.
    complete: bool


class Cohort(BaseModel):
    #: Monday of the registration week, Tashkent.
    starts: date
    size: int
    cells: list[CohortCell]


class Engagement(BaseModel):
    scope: Scope
    active: list[Figure]
    series: list[Series]
    cohorts: list[Cohort]
    #: Participants active in the period who had registered before it began.
    returning: Figure
    #: The first signed-in visit on record: "active" by visits cannot reach
    #: further back than this; "engaged" reads the records themselves.
    visits_since: date | None


class Funnel(BaseModel):
    scope: Scope
    #: Visitors to registrations, as counts within the period. Not a cohort:
    #: the women who registered need not be the visitors who were counted.
    acquisition: list[Stage]
    #: Accounts opened in the period, followed as far as they have got so far.
    journey: list[Stage]
    #: Diagnostic starts to completions within the period.
    diagnostic: list[Stage]


class ProgramStat(BaseModel):
    id: uuid.UUID
    slug: str
    title_i18n: dict
    is_published: bool
    #: Course page openings and distinct readers. None before views were tracked.
    views: int | None
    viewers: int | None
    enrolled: int
    started: int
    in_progress_now: int
    completed: int
    #: Enrollments from the period with no activity for two weeks and not done.
    stalled: int
    completion: Rate


class Programs(BaseModel):
    scope: Scope
    items: list[ProgramStat]
    total: int
    page: int
    size: int
    views_since: date | None


class Features(BaseModel):
    scope: Scope
    figures: list[Figure]
    ai_by_feature: list[Group]
    series: list[Series]


class ActivityItem(BaseModel):
    id: uuid.UUID
    name: str
    occurred_at: datetime
    #: A short pseudonymous tag for the account, the same for the same woman
    #: on this screen and meaningless anywhere else.
    actor: str | None
    region: str | None
    entity_type: str | None
    title_i18n: dict | None
    properties: dict


class Activity(BaseModel):
    scope: Scope
    items: list[ActivityItem]
    total: int
    page: int
    size: int
    since: date | None


class Errors(BaseModel):
    scope: Scope
    figures: list[Figure]
    series: list[Series]
    by_route: list[Group]
    by_status: list[Group]
    since: date | None
