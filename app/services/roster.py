"""The participant roster: who uses the portal, how much, and with what result.

One row per participant account, assembled from the same records the
dashboards count — page views, enrollments, assessments, plans, the assistant's
trace, applications, CV entries — but kept per person. Everything here is
individual-level data, so:

* **Participants only.** Staff, trainers, mentors and organisations' people
  are not listed; they are not whom the portal serves.
* **Identifiers are the administrator's.** A name and a masked address are
  attached only when the caller asked for them and the API layer decided she
  may have them. Everyone else gets `tag`, six characters of an HMAC of the
  account id — stable on these screens, meaningless anywhere else.
* **A region narrows everything.** A coordinator's rows are her region's;
  the API layer resolves which.
* **Search is an identifier too.** Searching by name or address is honoured
  only together with identifiers; otherwise the term is ignored.
* **No answers, no messages, no health.** The card carries dimension scores,
  courses and the names of events — never what she wrote or chose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Select, String, and_, cast, exists, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ApplicationStatus,
    EnrollmentStatus,
    Region,
    Role,
    ScoreDimension,
    UserStatus,
)
from app.core.security import mask_email
from app.models.analytics import AnalyticsEvent, PageView
from app.models.assessment import Assessment, DevelopmentScore
from app.models.audit import AiInteraction
from app.models.career import EducationEntry, WorkExperience
from app.models.opportunity import Application
from app.models.plan import DevelopmentPlan
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.user import User, UserRole
from app.schemas.roster import (
    CourseLine,
    DimensionScore,
    EventLine,
    Roster,
    RosterCard,
    RosterRow,
    RosterScope,
)
from app.services.insights import TIMELINE_EVENTS, _actions
from app.services.results import OFFSET, TZ
from app.services.traffic import hash_visitor

PARTICIPANT_ROLES = (Role.USER, Role.MOTHER)
STAFF_ROLES = tuple(role for role in Role if role not in PARTICIPANT_ROLES)
_LIVE_APPLICATION = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.IN_REVIEW,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
)
SORTS = ("last_active", "joined", "score", "views", "active_days", "courses", "name")
CARD_EVENTS = 20


@dataclass(frozen=True, slots=True)
class Query:
    region: Region | None = None
    status: UserStatus | None = None
    search: str | None = None
    sort: str = "last_active"
    descending: bool = True
    page: int = 1
    size: int = 25
    identifiers: bool = False


def tag_of(user_id: uuid.UUID) -> str:
    """The pseudonym: six characters of the HMAC the traffic table uses too,
    so the same woman reads the same here and on the activity timeline."""
    return hash_visitor(str(user_id))[:6]


def _participants() -> Select:
    staff = select(UserRole.user_id).where(UserRole.role.in_(STAFF_ROLES))
    return select(User.id).where(User.id.not_in(staff))


def _day(column):
    return func.date_trunc("day", func.timezone(TZ, column))


def _aggregates():
    """Per-user figures as subqueries, each keyed by user_id."""
    scores = (
        select(
            DevelopmentScore.user_id.label("user_id"),
            func.avg(DevelopmentScore.current)
            .filter(DevelopmentScore.dimension.in_(list(ScoreDimension)))
            .label("score"),
            func.max(DevelopmentScore.measured_at).label("measured_at"),
        )
        .group_by(DevelopmentScore.user_id)
        .subquery("scores")
    )
    diagnostics = (
        select(
            Assessment.user_id.label("user_id"),
            func.count().label("n"),
            func.max(Assessment.completed_at).label("last_at"),
        )
        .where(Assessment.completed_at.is_not(None))
        .group_by(Assessment.user_id)
        .subquery("diagnostics")
    )
    views = (
        select(
            PageView.user_id.label("user_id"),
            func.count().label("views"),
            func.count(func.distinct(PageView.session_id)).label("sessions"),
        )
        .where(PageView.user_id.is_not(None))
        .group_by(PageView.user_id)
        .subquery("views")
    )
    acts = _actions()
    activity = union_all(
        select(acts.c.user_id.label("user_id"), acts.c.at.label("at")),
        select(PageView.user_id.label("user_id"), PageView.occurred_at.label("at")).where(
            PageView.user_id.is_not(None)
        ),
    ).subquery("activity")
    days = (
        select(
            activity.c.user_id.label("user_id"),
            func.count(func.distinct(_day(activity.c.at))).label("days"),
        )
        .group_by(activity.c.user_id)
        .subquery("days")
    )
    courses = (
        select(
            Enrollment.user_id.label("user_id"),
            func.count().label("enrolled"),
            func.count().filter(Enrollment.status == EnrollmentStatus.COMPLETED).label("done"),
        )
        .group_by(Enrollment.user_id)
        .subquery("courses")
    )
    certificates = (
        select(Certificate.user_id.label("user_id"), func.count().label("n"))
        .where(Certificate.revoked_at.is_(None))
        .group_by(Certificate.user_id)
        .subquery("certificates")
    )
    plans = (
        select(
            DevelopmentPlan.user_id.label("user_id"),
            func.bool_or(DevelopmentPlan.accepted_at.is_not(None)).label("accepted"),
        )
        .group_by(DevelopmentPlan.user_id)
        .subquery("plans")
    )
    ai = (
        select(AiInteraction.user_id.label("user_id"), func.count().label("n"))
        .where(AiInteraction.user_id.is_not(None), AiInteraction.feature != "news_ingest")
        .group_by(AiInteraction.user_id)
        .subquery("ai")
    )
    applications = (
        select(Application.user_id.label("user_id"), func.count().label("n"))
        .where(Application.status.in_(_LIVE_APPLICATION))
        .group_by(Application.user_id)
        .subquery("applications")
    )
    cv_rows = union_all(
        select(WorkExperience.user_id.label("user_id")),
        select(EducationEntry.user_id.label("user_id")),
    ).subquery("cv_rows")
    cv = (
        select(cv_rows.c.user_id.label("user_id"), func.count().label("n"))
        .group_by(cv_rows.c.user_id)
        .subquery("cv")
    )
    return scores, diagnostics, views, days, courses, certificates, plans, ai, applications, cv


def _base(q: Query):
    scores, diagnostics, views, days, courses, certificates, plans, ai, applications, cv = (
        _aggregates()
    )
    columns = {
        "score": scores.c.score,
        "score_measured_at": scores.c.measured_at,
        "diagnostics": func.coalesce(diagnostics.c.n, 0),
        "last_diagnostic_at": diagnostics.c.last_at,
        "views": func.coalesce(views.c.views, 0),
        "sessions": func.coalesce(views.c.sessions, 0),
        "active_days": func.coalesce(days.c.days, 0),
        "courses_enrolled": func.coalesce(courses.c.enrolled, 0),
        "courses_completed": func.coalesce(courses.c.done, 0),
        "certificates": func.coalesce(certificates.c.n, 0),
        "plan_accepted": func.coalesce(plans.c.accepted, False),
        "ai_requests": func.coalesce(ai.c.n, 0),
        "applications": func.coalesce(applications.c.n, 0),
        "cv_entries": func.coalesce(cv.c.n, 0),
    }
    stmt = (
        select(User, Profile.full_name, *[col.label(name) for name, col in columns.items()])
        .outerjoin(Profile, Profile.user_id == User.id)
        .outerjoin(scores, scores.c.user_id == User.id)
        .outerjoin(diagnostics, diagnostics.c.user_id == User.id)
        .outerjoin(views, views.c.user_id == User.id)
        .outerjoin(days, days.c.user_id == User.id)
        .outerjoin(courses, courses.c.user_id == User.id)
        .outerjoin(certificates, certificates.c.user_id == User.id)
        .outerjoin(plans, plans.c.user_id == User.id)
        .outerjoin(ai, ai.c.user_id == User.id)
        .outerjoin(applications, applications.c.user_id == User.id)
        .outerjoin(cv, cv.c.user_id == User.id)
        .where(User.id.in_(_participants()))
    )
    if q.region is not None:
        stmt = stmt.where(User.region == q.region)
    if q.status is not None:
        stmt = stmt.where(User.status == q.status)
    if q.search and q.search.strip() and q.identifiers:
        needle = f"%{q.search.strip()[:80]}%"
        stmt = stmt.where(or_(Profile.full_name.ilike(needle), User.email.ilike(needle)))
    return stmt, columns


def _row(user: User, full_name: str | None, values: dict, *, identifiers: bool) -> RosterRow:
    deleted = user.status == UserStatus.DELETED
    show = identifiers and not deleted
    return RosterRow(
        id=user.id,
        tag=tag_of(user.id),
        name=(full_name or None) if show else None,
        email=mask_email(user.email) if show and user.email else None,
        region=user.region,
        status=user.status,
        language=user.language,
        joined_at=user.created_at,
        last_active_at=user.last_active_at,
        onboarded=user.onboarding_completed_at is not None,
        views=int(values["views"]),
        sessions=int(values["sessions"]),
        active_days=int(values["active_days"]),
        score=round(float(values["score"]), 1) if values["score"] is not None else None,
        score_measured_at=values["score_measured_at"],
        diagnostics=int(values["diagnostics"]),
        last_diagnostic_at=values["last_diagnostic_at"],
        courses_enrolled=int(values["courses_enrolled"]),
        courses_completed=int(values["courses_completed"]),
        certificates=int(values["certificates"]),
        plan_accepted=bool(values["plan_accepted"]),
        ai_requests=int(values["ai_requests"]),
        applications=int(values["applications"]),
        cv_entries=int(values["cv_entries"]),
    )


def _rows(result, *, identifiers: bool) -> list[RosterRow]:
    keys = list(result.keys())[2:]
    return [
        _row(user, full_name, dict(zip(keys, rest, strict=True)), identifiers=identifiers)
        for user, full_name, *rest in result.all()
    ]


async def _views_since(session: AsyncSession) -> date | None:
    first = await session.scalar(
        select(func.min(PageView.occurred_at)).where(PageView.user_id.is_not(None))
    )
    return (first + OFFSET).date() if first else None


async def roster(session: AsyncSession, q: Query) -> Roster:
    stmt, columns = _base(q)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    keys = {
        "last_active": User.last_active_at,
        "joined": User.created_at,
        "score": columns["score"],
        "views": columns["views"],
        "active_days": columns["active_days"],
        "courses": columns["courses_completed"],
        "name": Profile.full_name if q.identifiers else User.created_at,
    }
    key = keys[q.sort if q.sort in SORTS else "last_active"]
    order = key.desc().nullslast() if q.descending else key.asc().nullsfirst()
    result = await session.execute(
        stmt.order_by(order, User.created_at.desc()).limit(q.size).offset((q.page - 1) * q.size)
    )
    return Roster(
        scope=RosterScope(
            region=q.region,
            identifiers=q.identifiers,
            views_since=await _views_since(session),
            generated_at=datetime.now(UTC),
        ),
        items=_rows(result, identifiers=q.identifiers),
        total=total,
        page=q.page,
        size=q.size,
    )


async def all_rows(session: AsyncSession, q: Query) -> list[RosterRow]:
    """Every row the filters allow, for an export. Sorted like the table."""
    stmt, _ = _base(q)
    result = await session.execute(stmt.order_by(User.created_at.desc()))
    return _rows(result, identifiers=q.identifiers)


async def card(
    session: AsyncSession, user_id: uuid.UUID, *, region: Region | None, identifiers: bool
) -> RosterCard | None:
    """One woman's card, or None when she is not a participant in scope."""
    stmt, _ = _base(Query(region=region, identifiers=identifiers))
    result = await session.execute(stmt.where(User.id == user_id))
    rows = _rows(result, identifiers=identifiers)
    if not rows:
        return None

    dims = await session.execute(
        select(DevelopmentScore)
        .where(
            DevelopmentScore.user_id == user_id,
            DevelopmentScore.dimension.in_(list(ScoreDimension)),
        )
        .order_by(DevelopmentScore.dimension)
    )
    has_certificate = exists().where(Certificate.enrollment_id == Enrollment.id)
    courses = await session.execute(
        select(Enrollment, Program, has_certificate.label("certificate"))
        .join(Program, Program.id == Enrollment.program_id)
        .where(Enrollment.user_id == user_id)
        .order_by(func.coalesce(Enrollment.last_activity_at, Enrollment.created_at).desc())
    )
    events = await session.execute(
        select(AnalyticsEvent, Program.title_i18n)
        .outerjoin(
            Program,
            and_(
                AnalyticsEvent.entity_type == "program",
                cast(Program.id, String) == AnalyticsEvent.entity_id,
            ),
        )
        .where(AnalyticsEvent.user_id == user_id, AnalyticsEvent.name.in_(TIMELINE_EVENTS))
        .order_by(AnalyticsEvent.occurred_at.desc())
        .limit(CARD_EVENTS)
    )
    return RosterCard(
        row=rows[0],
        dimensions=[
            DimensionScore(
                dimension=score.dimension,
                baseline=round(score.baseline, 1),
                current=round(score.current, 1),
                target=round(score.target, 1) if score.target is not None else None,
            )
            for score in dims.scalars()
        ],
        courses=[
            CourseLine(
                program_id=program.id,
                slug=program.slug,
                title_i18n=program.title_i18n,
                status=enrollment.status,
                progress_percent=enrollment.progress_percent,
                started_at=enrollment.started_at,
                completed_at=enrollment.completed_at,
                certificate=bool(certificate),
            )
            for enrollment, program, certificate in courses
        ],
        events=[
            EventLine(name=event.name, occurred_at=event.occurred_at, title_i18n=title)
            for event, title in events
        ],
    )


EXPORT_COLUMNS = (
    "tag",
    "name",
    "email",
    "region",
    "status",
    "language",
    "joined_at",
    "last_active_at",
    "onboarded",
    "views",
    "sessions",
    "active_days",
    "score",
    "score_measured_at",
    "diagnostics",
    "last_diagnostic_at",
    "courses_enrolled",
    "courses_completed",
    "certificates",
    "plan_accepted",
    "ai_requests",
    "applications",
    "cv_entries",
)


def export_rows(rows: list[RosterRow]) -> list[list]:
    """The table as CSV rows, header first. Values as the API sends them."""
    out: list[list] = [list(EXPORT_COLUMNS)]
    for row in rows:
        data = row.model_dump()
        out.append(
            [
                getattr(value, "value", value.isoformat() if isinstance(value, datetime) else value)
                for value in (data[column] for column in EXPORT_COLUMNS)
            ]
        )
    return out
