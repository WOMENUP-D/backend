"""Product analytics: the administrator's questions, answered from records.

The read side. `traffic.py` and `telemetry.py` write; this module counts. It
sits beside `services.results` (what participants achieved) and answers the
other half of the brief: did anyone come, who registered, who is still here,
where do they drop out, which courses do they actually use, is anything
broken.

Rules, in addition to those `services.results` sets for periods, scopes and
rates:

* **Authoritative first.** Registrations are `users` rows, completions are
  `enrollments` rows, diagnostics are `assessments` rows. The event stream is
  read only for the steps that leave no other trace — a sign-up form opened,
  a course page read, a diagnostic begun — and for the activity timeline.
* **Say since when.** Sessions, referrers, devices and every event exist
  since revision 0029. A figure that depends on them carries `since`, the
  first day there is data, and is `None` (never `0`) when there is none.
* **Active means did something.** "Engaged" counts a participant with a
  recorded action — an enrollment touched, a diagnostic completed, a plan
  accepted, a CV entry added, an assistant question asked, an application
  sent, a task submitted, a project added, onboarding finished. "Visited"
  counts a signed-in page view. Both are shown; neither is mixed into the
  other.
* **Staff are not traffic.** A staff account's browsing and actions, and the
  management screens themselves, are left out unless the caller asks.
* **Nothing identifies anyone.** Groups are regions, devices, routes. The
  timeline shows a pseudonymous tag, a region and a course title.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, String, and_, case, cast, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus, EnrollmentStatus, Role, UserStatus
from app.models.analytics import AnalyticsEvent, PageView, VisitorPresence
from app.models.assessment import Assessment, DevelopmentScore
from app.models.audit import AiInteraction
from app.models.career import EducationEntry, WorkExperience
from app.models.learning_path import UserLearningPath
from app.models.opportunity import Application
from app.models.plan import DevelopmentPlan
from app.models.portfolio import PortfolioProject
from app.models.practice import TaskAttempt
from app.models.program import Enrollment, Program
from app.models.user import User, UserRole
from app.schemas.insights import (
    Accounts,
    Activity,
    ActivityItem,
    Cohort,
    CohortCell,
    Engagement,
    Errors,
    Features,
    Figure,
    Funnel,
    Live,
    Overview,
    PathStat,
    Programs,
    ProgramStat,
    Ratio,
    Scope,
    Stage,
    Traffic,
)
from app.schemas.results import Group, Period, Point, Rate, Series
from app.services.results import OFFSET, TZ, Filters, _bucket_starts, _within, people
from app.services.telemetry import SERVER_EVENTS
from app.services.traffic import STAFF_PATHS, hash_visitor

#: Minutes of silence after which a visitor is no longer "online".
PRESENCE_MINUTES = 5
#: A cached answer may be this old.
CACHE_TTL = 60
#: Enrollments untouched for this long, and unfinished, count as stalled.
STALLED_AFTER = timedelta(days=14)
#: Weeks of registration cohorts the retention grid shows.
COHORT_WEEKS = 8
TOP = 12
#: The longest period one request may ask for.
MAX_DAYS = 366

PARTICIPANT_ROLES = (Role.USER, Role.MOTHER)
STAFF_ROLES = tuple(role for role in Role if role not in PARTICIPANT_ROLES)
_LIVE_APPLICATION = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.IN_REVIEW,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
)
#: What the activity timeline shows. Logins and errors are not a story.
TIMELINE_EVENTS = tuple(sorted(SERVER_EVENTS - {"login_completed", "api_error"}))
#: Property keys the timeline is allowed to show.
TIMELINE_PROPERTIES = frozenset({"section", "version", "results", "resumed", "unit"})

#: What each figure counts, in words — shown under it on the dashboard.
DEFINITIONS: dict[str, str] = {
    "online_now": (
        f"Visitors with a page open in the last {PRESENCE_MINUTES} minutes (visitor_presence, "
        "refreshed by views and a once-a-minute heartbeat from visible tabs). Live."
    ),
    "online_signed_in": "Of those online now, the ones signed in to an account.",
    "views": "Screens opened in the period (page_views). Staff and management screens excluded.",
    "visitors": "Distinct visitor cookies with a view in the period (page_views.visitor_hash).",
    "sessions": (
        "Distinct browser sessions in the period (page_views.session_id). A session ends after "
        "30 minutes of silence. Recorded since sessions were added."
    ),
    "new_visitors": "Visitors whose first ever recorded view falls in the period.",
    "returning_visitors": "Visitors in the period who had been seen before it began.",
    "signed_in_visitors": "Visitors in the period who were signed in for at least one view.",
    "views_per_session": "Views divided by sessions, over the period.",
    "registrations": (
        "Participant accounts opened in the period (users.created_at; roles user or mother)."
    ),
    "accounts_total": (
        "Participant accounts that exist now and are not deleted. Staff accounts are counted "
        "separately. Duplicates cannot exist: an address or a phone number opens one account."
    ),
    "accounts_deleted": "Accounts a woman deleted (status deleted; identifiers anonymised).",
    "staff_accounts": "Accounts with a staff, trainer, mentor or organisation role, now.",
    "verified": "Participant accounts with a verified e-mail address or phone number, now.",
    "onboarded_total": "Participant accounts that have finished onboarding, now.",
    "onboarded_new": "Participants who finished onboarding in the period.",
    "onboarding_rate": "Of accounts opened in the period, the share that has finished onboarding.",
    "verified_rate": "Of participant accounts now, the share with a verified address or number.",
    "engaged_1d": (
        "Participants with a recorded action in the last 24 hours: an enrollment touched, a "
        "diagnostic completed, a plan generated or accepted, a CV entry added, an assistant "
        "question, an application, a task, a project, onboarding finished. Daily active users."
    ),
    "engaged_7d": "The same, over the last 7 days. Weekly active users.",
    "engaged_30d": "The same, over the last 30 days. Monthly active users.",
    "visited_1d": "Participants with a signed-in page view in the last 24 hours.",
    "visited_7d": "The same, over the last 7 days.",
    "visited_30d": "The same, over the last 30 days.",
    "engaged_users": "Participants with a recorded action in the period (see engaged_1d).",
    "engaged": "Participants with a recorded action in the period (see engaged_1d).",
    "visited_users": "Participants with a signed-in page view in the period.",
    "returning_users": "Participants active in the period who registered before it began.",
    "visitors_today": "Distinct visitors so far today (Tashkent).",
    "views_today": "Screens opened so far today.",
    "sessions_today": "Distinct browser sessions so far today.",
    "registrations_today": "Participant accounts opened so far today.",
    "engaged_today": "Participants with a recorded action so far today.",
    "registration_started": (
        "Visitors who began filling in the sign-up form (a browser event). Recorded since "
        "events were added."
    ),
    "registered": "Participant accounts opened in the period.",
    "onboarded": "Of those, accounts that have finished onboarding so far.",
    "test_started": (
        "Of those, accounts that began a Development Score diagnostic (a browser event). "
        "Recorded since events were added."
    ),
    "test_completed": "Of those, accounts with at least one completed diagnostic (assessments).",
    "score_received": "Of those, accounts holding a Development Score (development_scores).",
    "plan_accepted": "Of those, accounts that accepted a development plan.",
    "program_enrolled": "Of those, accounts enrolled in at least one course.",
    "program_started": "Of those, accounts that completed at least one lesson or module.",
    "program_completed": "Of those, accounts that completed at least one course.",
    "ai_requests": "Questions put to the AI assistant, navigator or coach (ai_interactions).",
    "ai_users": "Distinct signed-in participants who used the AI in the period.",
    "plans_generated": "Development plans drafted in the period (development_plans.created_at).",
    "plans_accepted": "Development plans accepted in the period (development_plans.accepted_at).",
    "cv_entries": "Work and education entries added to CVs in the period.",
    "cv_users": "Distinct participants who added a CV entry in the period.",
    "portfolio_projects": "Portfolio projects added in the period.",
    "applications": "Applications to jobs and other listings submitted in the period.",
    "tasks_submitted": "Practical-task attempts submitted in the period.",
    "paths_started": "Learning paths started in the period.",
    "searches": "Catalogue searches typed in the period (a browser event; the text is not kept).",
    "api_errors": "Requests the API answered with a server error (status 500 and above).",
    "client_errors": "Uncaught errors browsers reported from portal pages.",
}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    filters: Filters
    include_staff: bool = False

    @property
    def start(self) -> datetime | None:
        return self.filters.start

    @property
    def end(self) -> datetime | None:
        return self.filters.end


class PeriodTooLong(ValueError):
    pass


def filters_for(date_from: date | None, date_to: date | None) -> Filters:
    """The period a request asked for, defaulting to the last 30 Tashkent days
    when none was given — traffic over all time is not a dashboard figure."""
    today = (datetime.now(UTC) + OFFSET).date()
    if date_from is None and date_to is None:
        date_from, date_to = today - timedelta(days=29), today
    elif date_from is None:
        date_from = date_to - timedelta(days=29)
    elif date_to is None:
        date_to = min(today, date_from + timedelta(days=29))
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise PeriodTooLong
    return Filters(date_from=date_from, date_to=date_to)


_cache: dict[tuple, tuple[float, Any]] = {}


def _cached(key: tuple) -> Any | None:
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key: tuple, value: Any) -> None:
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic() + CACHE_TTL, value)


def _scope(q: Query, *, cached: bool = False) -> Scope:
    return Scope(
        period=Period(
            date_from=q.filters.date_from, date_to=q.filters.date_to, bucket=q.filters.bucket
        ),
        timezone=TZ,
        includes_staff=q.include_staff,
        generated_at=datetime.now(UTC),
        cached=cached,
        cache_ttl_seconds=CACHE_TTL if cached else 0,
    )


def _as_cached(model: Any) -> Any:
    scope = model.scope.model_copy(update={"cached": True, "cache_ttl_seconds": CACHE_TTL})
    return model.model_copy(update={"scope": scope})


async def _count(session: AsyncSession, stmt: Select) -> int:
    return int(await session.scalar(stmt) or 0)


def _rate(key: str, numerator: int, denominator: int) -> Rate:
    value = round(numerator * 100 / denominator, 1) if denominator else None
    return Rate(key=key, numerator=numerator, denominator=denominator, value=value)


def _share(part: int | None, whole: int | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part * 100 / whole, 1)


def _day(moment: datetime | None) -> date | None:
    return (moment + OFFSET).date() if moment else None


def _stages(
    rows: list[tuple[str, int | None, date | None]], period_start: date | None = None
) -> list[Stage]:
    """Counts chained into a funnel: each as a share of the one before and of
    the first.

    A stage that is None (never recorded) breaks neither chain: the next stage
    compares with the last known one. So does a stage recorded only from a day
    inside the period — comparing a full month of completions with a day of
    starts would say 1,400%. Its own count is still shown, with its "since".
    """
    out: list[Stage] = []

    def comparable(value: int | None, since: date | None) -> bool:
        if value is None:
            return False
        return since is None or (period_start is not None and since <= period_start)

    first = rows[0][1] if rows and comparable(rows[0][1], rows[0][2]) else None
    previous: int | None = None
    for key, value, since in rows:
        out.append(
            Stage(
                key=key,
                value=value,
                since=since,
                of_previous=_share(value, previous) if out else None,
                of_first=_share(value, first) if out else None,
            )
        )
        if comparable(value, since):
            previous = value
    return out


def _groups(rows, keys: list[str] | None = None) -> list[Group]:
    counts = {
        (key.value if hasattr(key, "value") else (key or "unknown")): int(n) for key, n in rows
    }
    order = keys if keys is not None else sorted(counts, key=lambda k: -counts[k])
    return [Group(key=key, value=counts.get(key, 0)) for key in order]


# ---------------------------------------------------------------------------
# Views: the one filter every traffic figure shares
# ---------------------------------------------------------------------------


def _views(q: Query, *, period: bool = True) -> Select:
    """Page views in scope: the period, no bots, and — unless asked — no staff
    browsing and no management screens."""
    stmt = select(PageView).where(or_(PageView.device.is_(None), PageView.device != "bot"))
    if period:
        stmt = stmt.where(*_within(PageView.occurred_at, q.filters))
    if not q.include_staff:
        stmt = stmt.where(PageView.is_staff.is_(False), PageView.path.not_in(STAFF_PATHS))
    return stmt


def _events(q: Query, *names: str, period: bool = True) -> Select:
    stmt = select(AnalyticsEvent).where(AnalyticsEvent.name.in_(names))
    if period:
        stmt = stmt.where(*_within(AnalyticsEvent.occurred_at, q.filters))
    if not q.include_staff:
        stmt = stmt.where(AnalyticsEvent.is_staff.is_(False))
    return stmt


async def _first_day(session: AsyncSession, stmt: Select) -> date | None:
    """The first day a source has data for, or None if it never has."""
    return _day(await session.scalar(stmt))


def _bucket(column, f: Filters):
    return func.date_trunc(f.bucket, func.timezone(TZ, column)).label("bucket")


async def _series(session: AsyncSession, key: str, stmt: Select, column, f: Filters, agg) -> Series:
    """One aggregate per day, week or month, empty buckets filled with 0."""
    bucket = _bucket(column, f)
    rows = (
        await session.execute(stmt.with_only_columns(bucket, agg).group_by(bucket).order_by(bucket))
    ).all()
    found = {row[0].date(): int(row[1] or 0) for row in rows}
    today = (datetime.now(UTC) + OFFSET).date()
    first = f.date_from or (min(found) if found else None)
    last = min(f.date_to, today) if f.date_to else today
    if first is None or first > last:
        return Series(key=key, points=[])
    return Series(
        key=key,
        points=[
            Point(bucket=start, value=found.get(start, 0))
            for start in _bucket_starts(first, last, f.bucket)
        ],
    )


def _first_seen() -> Select:
    """Every visitor's first view ever — the line between new and returning."""
    return (
        select(
            PageView.visitor_hash.label("visitor_hash"),
            func.min(PageView.occurred_at).label("first_at"),
        )
        .where(or_(PageView.device.is_(None), PageView.device != "bot"))
        .group_by(PageView.visitor_hash)
    )


# ---------------------------------------------------------------------------
# Engaged participants: a recorded action, from the records themselves
# ---------------------------------------------------------------------------


def _actions():
    """(user_id, at) for every recorded action a participant can take.

    One UNION of the authoritative tables, so "active" is defined once and
    reaches back as far as the records do — not only since events existed.
    """

    def part(user_col, at_col):
        return select(user_col.label("user_id"), at_col.label("at")).where(at_col.is_not(None))

    return union_all(
        part(Enrollment.user_id, Enrollment.last_activity_at),
        part(Enrollment.user_id, Enrollment.completed_at),
        part(Assessment.user_id, Assessment.completed_at),
        part(DevelopmentPlan.user_id, DevelopmentPlan.created_at),
        part(DevelopmentPlan.user_id, DevelopmentPlan.accepted_at),
        part(WorkExperience.user_id, WorkExperience.created_at),
        part(EducationEntry.user_id, EducationEntry.created_at),
        part(AiInteraction.user_id, AiInteraction.created_at).where(
            AiInteraction.user_id.is_not(None)
        ),
        part(Application.user_id, Application.submitted_at),
        part(PortfolioProject.user_id, PortfolioProject.created_at),
        part(TaskAttempt.user_id, func.coalesce(TaskAttempt.submitted_at, TaskAttempt.created_at)),
        part(UserLearningPath.user_id, UserLearningPath.started_at),
        part(User.id, User.onboarding_completed_at),
    ).subquery("actions")


async def _distinct_users_between(
    session: AsyncSession, source, user_col, at_col, who: Select, start: datetime, end: datetime
) -> int:
    return await _count(
        session,
        select(func.count(func.distinct(user_col)))
        .select_from(source)
        .where(user_col.in_(who), at_col >= start, at_col < end),
    )


async def _active_figures(session: AsyncSession, q: Query) -> list[Figure]:
    """DAU / WAU / MAU, engaged and visited, as rolling windows ending now."""
    now = datetime.now(UTC)
    who = people(q.filters)
    acts = _actions()
    visits = _views(q, period=False).where(PageView.user_id.is_not(None)).subquery("visits")
    out: list[Figure] = []
    for days in (1, 7, 30):
        since = now - timedelta(days=days)
        engaged = await _distinct_users_between(
            session, acts, acts.c.user_id, acts.c.at, who, since, now
        )
        visited = await _distinct_users_between(
            session, visits, visits.c.user_id, visits.c.occurred_at, who, since, now
        )
        out.append(Figure(key=f"engaged_{days}d", value=engaged, kind="live"))
        out.append(Figure(key=f"visited_{days}d", value=visited, kind="live"))
    return out


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


async def live(session: AsyncSession, q: Query) -> Live:
    """Who is here now. Never cached: it is the one figure that is live."""
    now = datetime.now(UTC)
    since = now - timedelta(minutes=PRESENCE_MINUTES)
    stmt = select(func.count(VisitorPresence.visitor_hash)).where(
        VisitorPresence.last_seen_at >= since
    )
    if not q.include_staff:
        stmt = stmt.where(VisitorPresence.is_staff.is_(False))
    online = await _count(session, stmt)
    signed_in = await _count(session, stmt.where(VisitorPresence.user_id.is_not(None)))
    return Live(
        online_now=online,
        online_signed_in=signed_in,
        window_minutes=PRESENCE_MINUTES,
        as_of=now,
    )


async def overview(session: AsyncSession, q: Query) -> Overview:
    key = ("overview", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit).model_copy(update={"live": await live(session, q)})

    today_local = (datetime.now(UTC) + OFFSET).date()
    today = Query(Filters(date_from=today_local, date_to=today_local), q.include_staff)
    who = people(q.filters)
    acts = _actions()

    async def traffic_figures(scope: Query, suffix: str) -> list[Figure]:
        views = _views(scope)
        sessions_since = await _first_day(
            session, select(func.min(PageView.occurred_at)).where(PageView.session_id.is_not(None))
        )
        return [
            Figure(
                key=f"visitors{suffix}",
                value=await _count(
                    session,
                    views.with_only_columns(func.count(func.distinct(PageView.visitor_hash))),
                ),
            ),
            Figure(
                key=f"views{suffix}",
                value=await _count(session, views.with_only_columns(func.count(PageView.id))),
            ),
            Figure(
                key=f"sessions{suffix}",
                value=(
                    await _count(
                        session,
                        views.where(PageView.session_id.is_not(None)).with_only_columns(
                            func.count(func.distinct(PageView.session_id))
                        ),
                    )
                    if sessions_since
                    else None
                ),
                since=sessions_since,
            ),
            Figure(
                key=f"registrations{suffix}",
                value=await _count(
                    session,
                    select(func.count(User.id)).where(
                        User.id.in_(who), *_within(User.created_at, scope.filters)
                    ),
                ),
            ),
            Figure(
                key=f"engaged{suffix}",
                value=await _count(
                    session,
                    select(func.count(func.distinct(acts.c.user_id)))
                    .select_from(acts)
                    .where(acts.c.user_id.in_(who), *_within(acts.c.at, scope.filters)),
                ),
            ),
        ]

    today_figures = await traffic_figures(today, "_today")
    period_figures = await traffic_figures(q, "")
    accounts_total = await _count(
        session,
        select(func.count(User.id)).where(User.id.in_(who), User.status != UserStatus.DELETED),
    )
    period_figures.append(Figure(key="accounts_total", value=accounts_total, kind="now"))

    visitors_series = await _series(
        session,
        "visitors",
        _views(q),
        PageView.occurred_at,
        q.filters,
        func.count(func.distinct(PageView.visitor_hash)),
    )
    engaged_series = await _series(
        session,
        "engaged_users",
        select(acts.c.user_id).where(acts.c.user_id.in_(who), *_within(acts.c.at, q.filters)),
        acts.c.at,
        q.filters,
        func.count(func.distinct(acts.c.user_id)),
    )

    result = Overview(
        scope=_scope(q),
        live=await live(session, q),
        today=today_figures,
        period=period_figures,
        active=await _active_figures(session, q),
        series=[visitors_series, engaged_series],
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Traffic
# ---------------------------------------------------------------------------


async def traffic(session: AsyncSession, q: Query) -> Traffic:
    key = ("traffic", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    views = _views(q)
    f = q.filters

    async def first_with(column) -> date | None:
        return await _first_day(
            session, select(func.min(PageView.occurred_at)).where(column.is_not(None))
        )

    collected = {
        "sessions": await first_with(PageView.session_id),
        "referrers": await first_with(PageView.referrer_host),
        "devices": await first_with(PageView.device),
    }

    n_views = await _count(session, views.with_only_columns(func.count(PageView.id)))
    n_visitors = await _count(
        session, views.with_only_columns(func.count(func.distinct(PageView.visitor_hash)))
    )
    n_sessions = (
        await _count(
            session,
            views.where(PageView.session_id.is_not(None)).with_only_columns(
                func.count(func.distinct(PageView.session_id))
            ),
        )
        if collected["sessions"]
        else None
    )
    signed_in = await _count(
        session,
        views.where(PageView.is_authenticated.is_(True)).with_only_columns(
            func.count(func.distinct(PageView.visitor_hash))
        ),
    )
    first_seen = _first_seen().subquery("first_seen")
    present = views.with_only_columns(PageView.visitor_hash).distinct().subquery("present")
    new_visitors = await _count(
        session,
        select(func.count())
        .select_from(first_seen)
        .where(
            first_seen.c.visitor_hash.in_(select(present.c.visitor_hash)),
            *_within(first_seen.c.first_at, f),
        ),
    )

    figures = [
        Figure(key="views", value=n_views),
        Figure(key="visitors", value=n_visitors),
        Figure(key="sessions", value=n_sessions, since=collected["sessions"]),
        Figure(key="new_visitors", value=new_visitors),
        Figure(key="returning_visitors", value=max(0, n_visitors - new_visitors)),
        Figure(key="signed_in_visitors", value=signed_in),
    ]
    ratios = [
        Ratio(
            key="views_per_session",
            value=round(n_views / n_sessions, 1) if n_sessions else None,
            numerator=n_views,
            denominator=n_sessions or 0,
        )
    ]

    series = [
        await _series(session, "views", views, PageView.occurred_at, f, func.count(PageView.id)),
        await _series(
            session,
            "visitors",
            views,
            PageView.occurred_at,
            f,
            func.count(func.distinct(PageView.visitor_hash)),
        ),
        await _series(
            session,
            "sessions",
            views.where(PageView.session_id.is_not(None)),
            PageView.occurred_at,
            f,
            func.count(func.distinct(PageView.session_id)),
        ),
        await _series(
            session,
            "new_visitors",
            select(first_seen.c.first_at).where(
                first_seen.c.visitor_hash.in_(select(present.c.visitor_hash)),
                *_within(first_seen.c.first_at, f),
            ),
            first_seen.c.first_at,
            f,
            func.count(),
        ),
    ]

    async def paths(stmt: Select) -> list[PathStat]:
        rows = await session.execute(
            stmt.with_only_columns(
                PageView.path,
                func.count(PageView.id),
                func.count(func.distinct(PageView.visitor_hash)),
            )
            .group_by(PageView.path)
            .order_by(func.count(PageView.id).desc(), PageView.path)
            .limit(TOP)
        )
        return [PathStat(path=row[0], views=int(row[1]), visitors=int(row[2])) for row in rows]

    pages = await paths(views)
    landing = await paths(views.where(PageView.is_entry.is_(True)))

    # The last view of each session is where it ended.
    last_of_session = (
        views.where(PageView.session_id.is_not(None))
        .with_only_columns(PageView.session_id, PageView.path, PageView.visitor_hash)
        .distinct(PageView.session_id)
        .order_by(PageView.session_id, PageView.occurred_at.desc())
        .subquery("last_of_session")
    )
    exit_rows = await session.execute(
        select(
            last_of_session.c.path,
            func.count(),
            func.count(func.distinct(last_of_session.c.visitor_hash)),
        )
        .group_by(last_of_session.c.path)
        .order_by(func.count().desc(), last_of_session.c.path)
        .limit(TOP)
    )
    exits = [PathStat(path=row[0], views=int(row[1]), visitors=int(row[2])) for row in exit_rows]

    async def grouped(column, *, entry_only: bool = False) -> list[Group]:
        stmt = views.where(column.is_not(None))
        if entry_only:
            stmt = stmt.where(PageView.is_entry.is_(True))
        rows = await session.execute(
            stmt.with_only_columns(column, func.count(func.distinct(PageView.visitor_hash)))
            .group_by(column)
            .order_by(func.count(func.distinct(PageView.visitor_hash)).desc(), column)
            .limit(TOP)
        )
        return [Group(key=str(row[0]), value=int(row[1])) for row in rows]

    result = Traffic(
        scope=_scope(q),
        figures=figures,
        ratios=ratios,
        series=series,
        pages=pages,
        landing=landing,
        exits=exits,
        referrers=await grouped(PageView.referrer_host, entry_only=True),
        devices=await grouped(PageView.device),
        browsers=await grouped(PageView.browser),
        systems=await grouped(PageView.os),
        locales=await grouped(PageView.locale),
        collected=collected,
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def accounts(session: AsyncSession, q: Query) -> Accounts:
    key = ("accounts", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    f = q.filters
    who = people(f)
    participants = select(func.count(User.id)).where(User.id.in_(who))
    now_rows = participants.where(User.status != UserStatus.DELETED)
    created_in_period = participants.where(*_within(User.created_at, f))

    total = await _count(session, now_rows)
    new = await _count(session, created_in_period)
    onboarded_of_new = await _count(
        session, created_in_period.where(User.onboarding_completed_at.is_not(None))
    )
    verified = await _count(
        session,
        now_rows.where(or_(User.email_verified.is_(True), User.phone_verified.is_(True))),
    )
    staff = await _count(
        session,
        select(func.count(func.distinct(UserRole.user_id))).where(UserRole.role.in_(STAFF_ROLES)),
    )
    figures = [
        Figure(key="accounts_total", value=total, kind="now"),
        Figure(key="registrations", value=new),
        Figure(
            key="onboarded_new",
            value=await _count(
                session, participants.where(*_within(User.onboarding_completed_at, f))
            ),
        ),
        Figure(
            key="onboarded_total",
            value=await _count(session, now_rows.where(User.onboarding_completed_at.is_not(None))),
            kind="now",
        ),
        Figure(key="verified", value=verified, kind="now"),
        Figure(
            key="accounts_deleted",
            value=await _count(session, participants.where(User.status == UserStatus.DELETED)),
            kind="now",
        ),
        Figure(key="staff_accounts", value=staff, kind="now"),
    ]
    rates = [
        _rate("onboarding_rate", onboarded_of_new, new),
        _rate("verified_rate", verified, total),
    ]

    base = select(User).where(User.id.in_(who), User.status != UserStatus.DELETED)
    by_status = _groups(
        await session.execute(
            select(User.status, func.count()).where(User.id.in_(who)).group_by(User.status)
        ),
        [s.value for s in UserStatus],
    )
    by_provider = _groups(
        await session.execute(
            base.with_only_columns(User.auth_provider, func.count()).group_by(User.auth_provider)
        )
    )
    by_language = _groups(
        await session.execute(
            base.with_only_columns(User.language, func.count()).group_by(User.language)
        )
    )

    started_events = _events(q, "registration_started")
    started_since = await _first_day(
        session,
        select(func.min(AnalyticsEvent.occurred_at)).where(
            AnalyticsEvent.name == "registration_started"
        ),
    )
    started = (
        await _count(
            session,
            started_events.with_only_columns(
                func.count(func.distinct(AnalyticsEvent.visitor_hash))
            ),
        )
        if started_since
        else None
    )

    result = Accounts(
        scope=_scope(q),
        figures=figures,
        rates=rates,
        series=[
            await _series(
                session,
                "registrations",
                select(User.id).where(User.id.in_(who)),
                User.created_at,
                f,
                func.count(),
            )
        ],
        by_status=by_status,
        by_provider=by_provider,
        by_language=by_language,
        registration=_stages(
            [("registration_started", started, started_since), ("registered", new, None)],
            f.date_from,
        ),
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Engagement: active users, returning users, retention
# ---------------------------------------------------------------------------


async def engagement(session: AsyncSession, q: Query) -> Engagement:
    key = ("engagement", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    f = q.filters
    who = people(f)
    acts = _actions()
    visits = _views(q, period=False).where(PageView.user_id.is_not(None)).subquery("visits")

    engaged_in_period = await _count(
        session,
        select(func.count(func.distinct(acts.c.user_id)))
        .select_from(acts)
        .where(acts.c.user_id.in_(who), *_within(acts.c.at, f)),
    )
    visited_in_period = await _count(
        session,
        select(func.count(func.distinct(visits.c.user_id)))
        .select_from(visits)
        .where(visits.c.user_id.in_(who), *_within(visits.c.occurred_at, f)),
    )
    before = (
        select(User.id).where(User.id.in_(who), User.created_at < f.start)
        if f.start
        else select(User.id).where(User.id.in_(who))
    )
    returning = await _count(
        session,
        select(func.count(func.distinct(acts.c.user_id)))
        .select_from(acts)
        .where(acts.c.user_id.in_(before), *_within(acts.c.at, f)),
    )

    series = [
        await _series(
            session,
            "engaged_users",
            select(acts.c.user_id).where(acts.c.user_id.in_(who), *_within(acts.c.at, f)),
            acts.c.at,
            f,
            func.count(func.distinct(acts.c.user_id)),
        ),
        await _series(
            session,
            "visited_users",
            select(visits.c.user_id).where(
                visits.c.user_id.in_(who), *_within(visits.c.occurred_at, f)
            ),
            visits.c.occurred_at,
            f,
            func.count(func.distinct(visits.c.user_id)),
        ),
    ]

    # Retention: registration-week cohorts, active (engaged or visited) in the
    # weeks after. Weeks are Tashkent Mondays.
    today = (datetime.now(UTC) + OFFSET).date()
    this_monday = today - timedelta(days=today.weekday())
    first_monday = this_monday - timedelta(weeks=COHORT_WEEKS - 1)
    first_utc = datetime.combine(first_monday, datetime.min.time(), tzinfo=UTC) - OFFSET
    cohort_rows = await session.execute(
        select(User.id, User.created_at).where(User.id.in_(who), User.created_at >= first_utc)
    )
    cohort_of: dict[uuid.UUID, date] = {}
    for user_id, created in cohort_rows:
        local = (created + OFFSET).date()
        cohort_of[user_id] = local - timedelta(days=local.weekday())

    cells: dict[date, dict[int, set[uuid.UUID]]] = defaultdict(lambda: defaultdict(set))
    if cohort_of:
        members = list(cohort_of)
        activity = union_all(
            select(acts.c.user_id.label("user_id"), acts.c.at.label("at")).where(
                acts.c.user_id.in_(members), acts.c.at >= first_utc
            ),
            select(visits.c.user_id.label("user_id"), visits.c.occurred_at.label("at")).where(
                visits.c.user_id.in_(members), visits.c.occurred_at >= first_utc
            ),
        ).subquery("activity")
        week = func.date_trunc("week", func.timezone(TZ, activity.c.at))
        rows = await session.execute(
            select(activity.c.user_id, week).group_by(activity.c.user_id, week)
        )
        for user_id, moment in rows:
            start = cohort_of.get(user_id)
            if start is None:
                continue
            offset = (moment.date() - start).days // 7
            if offset >= 0:
                cells[start][offset].add(user_id)

    cohorts: list[Cohort] = []
    sizes: dict[date, int] = defaultdict(int)
    for start in cohort_of.values():
        sizes[start] += 1
    for start in sorted(sizes):
        size = sizes[start]
        span = (this_monday - start).days // 7
        cohorts.append(
            Cohort(
                starts=start,
                size=size,
                cells=[
                    CohortCell(
                        week=offset,
                        value=len(cells[start][offset]),
                        share=_share(len(cells[start][offset]), size),
                        complete=start + timedelta(weeks=offset + 1) <= today,
                    )
                    for offset in range(span + 1)
                ],
            )
        )

    visits_since = await _first_day(
        session, select(func.min(PageView.occurred_at)).where(PageView.user_id.is_not(None))
    )
    result = Engagement(
        scope=_scope(q),
        active=[
            *await _active_figures(session, q),
            Figure(key="engaged_users", value=engaged_in_period),
            Figure(key="visited_users", value=visited_in_period, since=visits_since),
        ],
        series=series,
        cohorts=cohorts,
        returning=Figure(key="returning_users", value=returning),
        visits_since=visits_since,
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------


async def funnel(session: AsyncSession, q: Query) -> Funnel:
    key = ("funnel", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    f = q.filters
    who = people(f)

    async def event_since(name: str) -> date | None:
        return await _first_day(
            session,
            select(func.min(AnalyticsEvent.occurred_at)).where(AnalyticsEvent.name == name),
        )

    # Acquisition: counts within the period, each of its own population. Views
    # have been recorded since revision 0004; the day they began is carried so
    # a period reaching further back never treats them as a full denominator.
    visitors = await _count(
        session, _views(q).with_only_columns(func.count(func.distinct(PageView.visitor_hash)))
    )
    visitors_since = await _first_day(session, select(func.min(PageView.occurred_at)))
    started_since = await event_since("registration_started")
    started = (
        await _count(
            session,
            _events(q, "registration_started").with_only_columns(
                func.count(func.distinct(AnalyticsEvent.visitor_hash))
            ),
        )
        if started_since
        else None
    )
    registered = await _count(
        session,
        select(func.count(User.id)).where(User.id.in_(who), *_within(User.created_at, f)),
    )
    acquisition = _stages(
        [
            ("visitors", visitors, visitors_since),
            ("registration_started", started, started_since),
            ("registered", registered, None),
        ],
        f.date_from,
    )

    # Journey: the accounts opened in the period, followed as far as they got.
    cohort = select(User.id).where(User.id.in_(who), *_within(User.created_at, f))

    async def of_cohort(stmt: Select) -> int:
        return await _count(session, stmt)

    def users_with(model, *conditions) -> Select:
        return select(func.count(func.distinct(model.user_id))).where(
            model.user_id.in_(cohort), *conditions
        )

    test_started_since = await event_since("development_test_started")
    test_started = (
        await of_cohort(
            select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                AnalyticsEvent.user_id.in_(cohort),
                AnalyticsEvent.name == "development_test_started",
            )
        )
        if test_started_since
        else None
    )
    program_started = EnrollmentStatus.IN_PROGRESS, EnrollmentStatus.COMPLETED
    journey = _stages(
        [
            ("registered", registered, None),
            (
                "onboarded",
                await of_cohort(
                    select(func.count(User.id)).where(
                        User.id.in_(cohort), User.onboarding_completed_at.is_not(None)
                    )
                ),
                None,
            ),
            ("test_started", test_started, test_started_since),
            (
                "test_completed",
                await of_cohort(users_with(Assessment, Assessment.completed_at.is_not(None))),
                None,
            ),
            ("score_received", await of_cohort(users_with(DevelopmentScore)), None),
            (
                "plan_accepted",
                await of_cohort(
                    users_with(DevelopmentPlan, DevelopmentPlan.accepted_at.is_not(None))
                ),
                None,
            ),
            ("program_enrolled", await of_cohort(users_with(Enrollment)), None),
            (
                "program_started",
                await of_cohort(
                    users_with(
                        Enrollment,
                        or_(
                            Enrollment.progress_percent > 0, Enrollment.status.in_(program_started)
                        ),
                    )
                ),
                None,
            ),
            (
                "program_completed",
                await of_cohort(
                    users_with(Enrollment, Enrollment.status == EnrollmentStatus.COMPLETED)
                ),
                None,
            ),
        ],
        f.date_from,
    )

    # Diagnostic within the period: who began, who finished.
    diag_started = (
        await _count(
            session,
            _events(q, "development_test_started").with_only_columns(
                func.count(func.distinct(AnalyticsEvent.user_id))
            ),
        )
        if test_started_since
        else None
    )
    diag_completed = await _count(
        session,
        select(func.count(func.distinct(Assessment.user_id))).where(
            Assessment.user_id.in_(who), *_within(Assessment.completed_at, f)
        ),
    )
    diagnostic = _stages(
        [
            ("test_started", diag_started, test_started_since),
            ("test_completed", diag_completed, None),
        ],
        f.date_from,
    )

    result = Funnel(
        scope=_scope(q), acquisition=acquisition, journey=journey, diagnostic=diagnostic
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Programmes
# ---------------------------------------------------------------------------

PROGRAM_SORTS = ("enrolled", "started", "completed", "completion", "views", "stalled", "title")
LANGS = ("uz", "ru", "en")


async def programs(
    session: AsyncSession,
    q: Query,
    *,
    sort: str = "enrolled",
    descending: bool = True,
    page: int = 1,
    size: int = 20,
    search: str | None = None,
    lang: str = "uz",
) -> Programs:
    f = q.filters
    who = people(f)
    mine = Enrollment.user_id.in_(who)
    started_col = func.coalesce(Enrollment.started_at, Enrollment.created_at)
    in_period = and_(*_within(started_col, f))
    done_in_period = and_(*_within(Enrollment.completed_at, f))
    stale = datetime.now(UTC) - STALLED_AFTER
    begun = or_(
        Enrollment.progress_percent > 0,
        Enrollment.status.in_((EnrollmentStatus.IN_PROGRESS, EnrollmentStatus.COMPLETED)),
    )

    agg = (
        select(
            Enrollment.program_id.label("program_id"),
            func.count().filter(in_period).label("enrolled"),
            func.count().filter(in_period, begun).label("started"),
            func.count()
            .filter(
                Enrollment.status.in_((EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS))
            )
            .label("in_progress"),
            func.count()
            .filter(Enrollment.status == EnrollmentStatus.COMPLETED, done_in_period)
            .label("completed"),
            func.count()
            .filter(Enrollment.status == EnrollmentStatus.COMPLETED, in_period)
            .label("cohort_done"),
            func.count()
            .filter(
                in_period,
                Enrollment.status != EnrollmentStatus.COMPLETED,
                func.coalesce(Enrollment.last_activity_at, started_col) < stale,
            )
            .label("stalled"),
        )
        .where(mine)
        .group_by(Enrollment.program_id)
        .subquery("agg")
    )
    viewed = (
        _events(q, "program_viewed")
        .where(AnalyticsEvent.entity_type == "program")
        .with_only_columns(
            AnalyticsEvent.entity_id.label("program_id"),
            func.count().label("views"),
            func.count(
                func.distinct(
                    func.coalesce(AnalyticsEvent.visitor_hash, cast(AnalyticsEvent.user_id, String))
                )
            ).label("viewers"),
        )
        .group_by(AnalyticsEvent.entity_id)
        .subquery("viewed")
    )
    views_since = await _first_day(
        session,
        select(func.min(AnalyticsEvent.occurred_at)).where(AnalyticsEvent.name == "program_viewed"),
    )

    enrolled = func.coalesce(agg.c.enrolled, 0)
    cohort_done = func.coalesce(agg.c.cohort_done, 0)
    rate = case((enrolled > 0, cohort_done * 100.0 / enrolled), else_=None)
    title = func.coalesce(
        *[
            Program.title_i18n[code].astext
            for code in dict.fromkeys((lang if lang in LANGS else "uz", *LANGS))
        ]
    )
    base = (
        select(
            Program,
            enrolled.label("enrolled"),
            func.coalesce(agg.c.started, 0).label("started"),
            func.coalesce(agg.c.in_progress, 0).label("in_progress"),
            func.coalesce(agg.c.completed, 0).label("completed"),
            cohort_done.label("cohort_done"),
            func.coalesce(agg.c.stalled, 0).label("stalled"),
            viewed.c.views,
            viewed.c.viewers,
        )
        .outerjoin(agg, agg.c.program_id == Program.id)
        .outerjoin(viewed, viewed.c.program_id == cast(Program.id, String))
        .where(or_(Program.is_published.is_(True), agg.c.program_id.is_not(None)))
    )
    if search and search.strip():
        needle = f"%{search.strip()[:80]}%"
        base = base.where(or_(*[Program.title_i18n[code].astext.ilike(needle) for code in LANGS]))
    total = await _count(session, select(func.count()).select_from(base.subquery()))
    key = {
        "enrolled": enrolled,
        "started": func.coalesce(agg.c.started, 0),
        "completed": func.coalesce(agg.c.completed, 0),
        "completion": rate,
        "views": func.coalesce(viewed.c.views, 0),
        "stalled": func.coalesce(agg.c.stalled, 0),
        "title": title,
    }[sort if sort in PROGRAM_SORTS else "enrolled"]
    order = key.desc().nullslast() if descending else key.asc().nullsfirst()
    rows = await session.execute(
        base.order_by(order, title.asc()).limit(size).offset((page - 1) * size)
    )
    items = [
        ProgramStat(
            id=program.id,
            slug=program.slug,
            title_i18n=program.title_i18n,
            is_published=program.is_published,
            views=int(n_views or 0) if views_since else None,
            viewers=int(n_viewers or 0) if views_since else None,
            enrolled=int(n_enrolled),
            started=int(n_started),
            in_progress_now=int(n_progress),
            completed=int(n_completed),
            stalled=int(n_stalled),
            completion=_rate("completion", int(n_cohort), int(n_enrolled)),
        )
        for (
            program,
            n_enrolled,
            n_started,
            n_progress,
            n_completed,
            n_cohort,
            n_stalled,
            n_views,
            n_viewers,
        ) in rows
    ]
    return Programs(
        scope=_scope(q), items=items, total=total, page=page, size=size, views_since=views_since
    )


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


async def features(session: AsyncSession, q: Query) -> Features:
    key = ("features", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    f = q.filters
    who = people(f)
    ai = select(AiInteraction).where(
        AiInteraction.feature != "news_ingest",
        or_(AiInteraction.user_id.is_(None), AiInteraction.user_id.in_(who)),
        *_within(AiInteraction.created_at, f),
    )
    cv = union_all(
        select(WorkExperience.user_id.label("user_id"), WorkExperience.created_at.label("at")),
        select(EducationEntry.user_id.label("user_id"), EducationEntry.created_at.label("at")),
    ).subquery("cv")
    cv_rows = select(cv.c.user_id).where(cv.c.user_id.in_(who), *_within(cv.c.at, f))
    searches_since = await _first_day(
        session,
        select(func.min(AnalyticsEvent.occurred_at)).where(
            AnalyticsEvent.name == "catalogue_search"
        ),
    )

    figures = [
        Figure(key="ai_requests", value=await _count(session, ai.with_only_columns(func.count()))),
        Figure(
            key="ai_users",
            value=await _count(
                session,
                ai.where(AiInteraction.user_id.is_not(None)).with_only_columns(
                    func.count(func.distinct(AiInteraction.user_id))
                ),
            ),
        ),
        Figure(
            key="plans_generated",
            value=await _count(
                session,
                select(func.count(DevelopmentPlan.id)).where(
                    DevelopmentPlan.user_id.in_(who), *_within(DevelopmentPlan.created_at, f)
                ),
            ),
        ),
        Figure(
            key="plans_accepted",
            value=await _count(
                session,
                select(func.count(DevelopmentPlan.id)).where(
                    DevelopmentPlan.user_id.in_(who), *_within(DevelopmentPlan.accepted_at, f)
                ),
            ),
        ),
        Figure(
            key="cv_entries",
            value=await _count(session, cv_rows.with_only_columns(func.count())),
        ),
        Figure(
            key="cv_users",
            value=await _count(
                session, cv_rows.with_only_columns(func.count(func.distinct(cv.c.user_id)))
            ),
        ),
        Figure(
            key="portfolio_projects",
            value=await _count(
                session,
                select(func.count(PortfolioProject.id)).where(
                    PortfolioProject.user_id.in_(who), *_within(PortfolioProject.created_at, f)
                ),
            ),
        ),
        Figure(
            key="applications",
            value=await _count(
                session,
                select(func.count(Application.id)).where(
                    Application.user_id.in_(who),
                    Application.status.in_(_LIVE_APPLICATION),
                    *_within(Application.submitted_at, f),
                ),
            ),
        ),
        Figure(
            key="tasks_submitted",
            value=await _count(
                session,
                select(func.count(TaskAttempt.id)).where(
                    TaskAttempt.user_id.in_(who), *_within(TaskAttempt.submitted_at, f)
                ),
            ),
        ),
        Figure(
            key="paths_started",
            value=await _count(
                session,
                select(func.count(UserLearningPath.id)).where(
                    UserLearningPath.user_id.in_(who), *_within(UserLearningPath.started_at, f)
                ),
            ),
        ),
        Figure(
            key="searches",
            value=(
                await _count(
                    session, _events(q, "catalogue_search").with_only_columns(func.count())
                )
                if searches_since
                else None
            ),
            since=searches_since,
        ),
    ]
    by_feature = _groups(
        await session.execute(
            ai.with_only_columns(AiInteraction.feature, func.count()).group_by(
                AiInteraction.feature
            )
        )
    )
    result = Features(
        scope=_scope(q),
        figures=figures,
        ai_by_feature=by_feature,
        series=[
            await _series(
                session,
                "ai_requests",
                ai.with_only_columns(AiInteraction.id),
                AiInteraction.created_at,
                f,
                func.count(),
            )
        ],
    )
    _store(key, result)
    return result


# ---------------------------------------------------------------------------
# Activity timeline
# ---------------------------------------------------------------------------


async def activity(
    session: AsyncSession,
    q: Query,
    *,
    page: int = 1,
    size: int = 30,
    names: list[str] | None = None,
) -> Activity:
    wanted = [n for n in (names or TIMELINE_EVENTS) if n in TIMELINE_EVENTS] or list(
        TIMELINE_EVENTS
    )
    events = _events(q, *wanted)
    total = await _count(session, events.with_only_columns(func.count()))
    rows = await session.execute(
        events.with_only_columns(AnalyticsEvent, User.region, Program.title_i18n)
        .outerjoin(User, User.id == AnalyticsEvent.user_id)
        .outerjoin(
            Program,
            and_(
                AnalyticsEvent.entity_type == "program",
                cast(Program.id, String) == AnalyticsEvent.entity_id,
            ),
        )
        .order_by(AnalyticsEvent.occurred_at.desc())
        .limit(size)
        .offset((page - 1) * size)
    )
    items = [
        ActivityItem(
            id=event.id,
            name=event.name,
            occurred_at=event.occurred_at,
            actor=hash_visitor(str(event.user_id))[:6] if event.user_id else None,
            region=region.value if region else None,
            entity_type=event.entity_type,
            title_i18n=title,
            properties={
                k: v for k, v in (event.properties or {}).items() if k in TIMELINE_PROPERTIES
            },
        )
        for event, region, title in rows
    ]
    since = await _first_day(session, select(func.min(AnalyticsEvent.occurred_at)))
    return Activity(scope=_scope(q), items=items, total=total, page=page, size=size, since=since)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def errors(session: AsyncSession, q: Query) -> Errors:
    key = ("errors", q)
    hit = _cached(key)
    if hit is not None:
        return _as_cached(hit)

    f = q.filters
    api = select(AnalyticsEvent).where(
        AnalyticsEvent.name == "api_error", *_within(AnalyticsEvent.occurred_at, f)
    )
    client = _events(q, "client_error")
    since = await _first_day(
        session,
        select(func.min(AnalyticsEvent.occurred_at)).where(
            AnalyticsEvent.name.in_(("api_error", "client_error"))
        ),
    )
    route = AnalyticsEvent.properties["route"].astext
    status = AnalyticsEvent.properties["status"].astext
    by_route = await session.execute(
        api.with_only_columns(route, func.count())
        .where(route.is_not(None))
        .group_by(route)
        .order_by(func.count().desc())
        .limit(TOP)
    )
    by_status = await session.execute(
        api.with_only_columns(status, func.count())
        .where(status.is_not(None))
        .group_by(status)
        .order_by(func.count().desc())
    )
    result = Errors(
        scope=_scope(q),
        figures=[
            Figure(
                key="api_errors",
                value=await _count(session, api.with_only_columns(func.count())) if since else None,
                since=since,
            ),
            Figure(
                key="client_errors",
                value=await _count(session, client.with_only_columns(func.count()))
                if since
                else None,
                since=since,
            ),
        ],
        series=[
            await _series(
                session,
                "api_errors",
                api.with_only_columns(AnalyticsEvent.id),
                AnalyticsEvent.occurred_at,
                f,
                func.count(),
            ),
            await _series(
                session,
                "client_errors",
                client.with_only_columns(AnalyticsEvent.id),
                AnalyticsEvent.occurred_at,
                f,
                func.count(),
            ),
        ],
        by_route=[Group(key=str(row[0]), value=int(row[1])) for row in by_route],
        by_status=[Group(key=str(row[0]), value=int(row[1])) for row in by_status],
        since=since,
    )
    _store(key, result)
    return result


def clear_cache() -> None:
    """For tests, and for anything that must see a write straight away."""
    _cache.clear()
