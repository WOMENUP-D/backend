"""Results & impact: what is actually happening on WomanUP, counted from records.

Every figure on the admin Results dashboard comes from one query here, and each
has a written definition in `DEFINITIONS` — the same words the dashboard shows
and the CSV export carries. The rules:

* **Participants.** The women and girls the platform serves: accounts whose
  only roles are `user` or `mother`. Staff, mentors, trainers, coordinators and
  organisations' people are not counted, so the platform's own team never
  inflates its reach. A deleted account's history still counts toward what
  happened (it was anonymised, not erased); it is not counted as a current
  participant.
* **Period.** `[date_from 00:00, date_to + 1 day 00:00)` in Tashkent time.
  Each metric filters on its own event's time — a completion on `completed_at`,
  a certificate on `issued_at` — and its definition names which. Metrics that
  describe the state of things now (courses in progress, open listings) say
  "now" and ignore the period.
* **Scope.** A region filter narrows the participants; a regional
  coordinator's scope is her region whatever she asks for (`api.admin`).
* **Nothing inferred.** An application is not a job, a course is not a job, a
  verified skill is not a job. Outcomes are only `outcome_records` — results a
  partner platform or an organisation recorded. Event attendance is not
  recorded anywhere, so it is reported as not recorded, not as registrations.
* **Rates** carry their numerator and denominator and are `None` over 0.
* **Small groups.** A demographic group of 1-4 people is suppressed
  (`MIN_GROUP`), and when exactly one group in a list is, the next smallest is
  hidden with it so the total cannot give it away. A Development Score average
  needs `MIN_GROUP` people, and a sample under `SMALL_SAMPLE` is flagged as
  describing those people only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, and_, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    SCORE_WEIGHTS,
    ApplicationStatus,
    EnrollmentStatus,
    EvaluatorKind,
    EvidenceKind,
    InvitationStatus,
    NotificationTrigger,
    OpportunitySource,
    OpportunityType,
    Region,
    Role,
    ScoreDimension,
    SkillStatus,
    TaskStatus,
    UserStatus,
)
from app.models.analytics import PageView
from app.models.assessment import Assessment, DevelopmentScore
from app.models.learning_path import UserLearningPath
from app.models.notification import Notification
from app.models.opportunity import Application, Opportunity, OutcomeRecord, SavedOpportunity
from app.models.organization import OrganizationInvitation
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User, UserRole
from app.schemas.results import (
    DimensionReading,
    Group,
    Metric,
    Named,
    Period,
    Point,
    ProgrammeRow,
    ProgrammeTable,
    Rate,
    ResultsEvents,
    ResultsLearning,
    ResultsOpportunities,
    ResultsOverview,
    ResultsScore,
    ResultsSkills,
    Scope,
    Series,
    SkillCount,
)
from app.schemas.skill import SkillRef
from app.services.score_insights import FOCUS_BELOW, STRONG_FROM
from app.services.skills import EVIDENCE_STATUS

#: A demographic group smaller than this is not shown as a number.
MIN_GROUP = 5
#: Below this many people, a score summary describes them and nobody else.
SMALL_SAMPLE = 30
TZ = "Asia/Tashkent"
OFFSET = timedelta(hours=5)
TOP = 8

PARTICIPANT_ROLES = (Role.USER, Role.MOTHER)
STAFF_ROLES = tuple(role for role in Role if role not in PARTICIPANT_ROLES)
_LIVE_APPLICATION = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.IN_REVIEW,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.WITHDRAWN,
)
AGE_BRACKETS = (("10-17", 10, 17), ("18-24", 18, 24), ("25-34", 25, 34), ("35-44", 35, 44),
                ("45-54", 45, 54), ("55+", 55, 200))  # fmt: skip

#: What each figure counts, in words — shown under it and written into the export.
DEFINITIONS: dict[str, str] = {
    "participants": (
        "Participant accounts (roles user or mother only; not deleted) that existed at the "
        "end of the period."
    ),
    "new_registrations": "Participant accounts created in the period (users.created_at).",
    "active_in_period": (
        "Participants with at least one signed-in page view in the period (page_views). "
        "Recorded since activity_since."
    ),
    "onboarded": (
        "Participants who finished onboarding in the period (users.onboarding_completed_at)."
    ),
    "diagnosed": (
        "Participants who completed at least one Development Score diagnostic in the period "
        "(assessments.completed_at)."
    ),
    "learners_started": (
        "Participants who started at least one course in the period (enrollments.started_at)."
    ),
    "enrollments_started": "Course enrollments started in the period (enrollments.started_at).",
    "in_progress_now": (
        "Course enrollments in progress now (status enrolled or in_progress). Ignores the period."
    ),
    "course_completions": "Course enrollments completed in the period (enrollments.completed_at).",
    "certificates": "Certificates issued in the period and not revoked (certificates.issued_at).",
    "paths_started": "Learning paths started in the period (user_learning_paths.started_at).",
    "paths_completed": "Learning paths completed in the period (user_learning_paths.completed_at).",
    "tasks_started": "Practical-task attempts started in the period.",
    "tasks_submitted": (
        "Practical-task attempts submitted for evaluation in the period "
        "(task_attempts.submitted_at)."
    ),
    "tasks_evaluated": (
        "Practical-task attempts evaluated in the period (task_attempts.evaluated_at)."
    ),
    "tasks_passed": "Evaluated attempts that passed, evaluated in the period.",
    "portfolio_projects": "Portfolio projects added in the period.",
    "applications": (
        "Applications to jobs, internships and other listings submitted in the period (not "
        "events; not drafts)."
    ),
    "event_registrations": (
        "Registrations for events submitted in the period (applications to events)."
    ),
    "outcomes_recorded": (
        "Results a partner platform or an organisation recorded in the period "
        "(outcome_records). Never inferred from applications."
    ),
    "saved_listings": "Listings (not events) saved in the period.",
    "invitations_sent": "Invitations organisations sent to candidates in the period.",
    "invitations_accepted": "Invitations accepted in the period.",
    "events_added": (
        "Events added to the platform in the period (opportunities with a start time, created_at)."
    ),
    "events_held": "Events that started in the period.",
    "events_upcoming": "Active events that have not ended yet. Ignores the period.",
    "event_reminders": "Event reminders participants set in the period.",
    "events_saved": "Events saved in the period.",
    "attendance": (
        "Not recorded: WomanUP has no attendance record. Registrations are not attendance."
    ),
    "course_completion_rate": "Of enrollments started in the period, the share completed so far.",
    "path_completion_rate": "Of learning paths started in the period, the share completed so far.",
    "task_pass_rate": "Of attempts evaluated in the period, the share that passed.",
    "application_accepted_rate": (
        "Of applications submitted in the period, the share an organisation or partner has "
        "accepted so far."
    ),
    "score_people": (
        "Participants with a Development Score (development_scores). Current state; ignores "
        "the period."
    ),
}


@dataclass(frozen=True, slots=True)
class Filters:
    date_from: date | None = None
    date_to: date | None = None
    region: Region | None = None

    @property
    def start(self) -> datetime | None:
        if self.date_from is None:
            return None
        return datetime.combine(self.date_from, datetime.min.time(), tzinfo=UTC) - OFFSET

    @property
    def end(self) -> datetime | None:
        if self.date_to is None:
            return None
        return (
            datetime.combine(self.date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            - OFFSET
        )

    @property
    def bucket(self) -> str:
        if self.date_from is None or self.date_to is None:
            return "month"
        days = (self.date_to - self.date_from).days + 1
        return "day" if days <= 31 else "week" if days <= 180 else "month"


def _scope(f: Filters) -> Scope:
    return Scope(
        period=Period(date_from=f.date_from, date_to=f.date_to, bucket=f.bucket),
        region=f.region,
        generated_at=datetime.now(UTC),
    )


def _within(column, f: Filters) -> list:
    out = []
    if f.start is not None:
        out.append(column >= f.start)
    if f.end is not None:
        out.append(column < f.end)
    return out


def people(f: Filters) -> Select:
    """The participants in scope, as a subquery of user ids."""
    staff = select(UserRole.user_id).where(UserRole.role.in_(STAFF_ROLES))
    stmt = select(User.id).where(User.id.not_in(staff))
    if f.region is not None:
        stmt = stmt.where(User.region == f.region)
    return stmt


async def _count(session: AsyncSession, stmt: Select) -> int:
    return int(await session.scalar(stmt) or 0)


def _rate(key: str, numerator: int, denominator: int) -> Rate:
    value = round(numerator * 100 / denominator, 1) if denominator else None
    return Rate(key=key, numerator=numerator, denominator=denominator, value=value)


def _groups(counts: dict[str, int], keys: list[str] | None = None) -> list[Group]:
    keys = keys if keys is not None else sorted(counts, key=lambda k: -counts[k])
    return [Group(key=key, value=counts.get(key, 0)) for key in keys]


def _suppressed(counts: dict[str, int], keys: list[str]) -> list[Group]:
    """Groups of 1-4 people are reported as suppressed, not as a number.

    The groups' total is always on screen next to them (participants, or the
    score's sample), so one hidden group alone could be read back as the total
    minus the rest. When exactly one is hidden, the next smallest group is
    hidden with it — a non-empty one if there is one — and only their sum can
    be worked out.
    """
    values = {key: int(counts.get(key, 0)) for key in keys}
    hidden = {key for key, value in values.items() if 0 < value < MIN_GROUP}
    if len(hidden) == 1:
        rest = [(value == 0, value, key) for key, value in values.items() if key not in hidden]
        if rest:
            hidden.add(min(rest)[2])
    return [
        Group(key=key, value=None, suppressed=True)
        if key in hidden
        else Group(key=key, value=value)
        for key, value in values.items()
    ]


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def _bucket_starts(first: date, last: date, bucket: str) -> list[date]:
    if bucket == "week":
        first = first - timedelta(days=first.weekday())
    if bucket == "month":
        first = first.replace(day=1)
    out: list[date] = []
    cursor = first
    while cursor <= last and len(out) < 400:
        out.append(cursor)
        if bucket == "day":
            cursor += timedelta(days=1)
        elif bucket == "week":
            cursor += timedelta(days=7)
        else:
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


async def _series(session: AsyncSession, key: str, column, stmt: Select, f: Filters) -> Series:
    """Counts per day, week or month of `column`, with empty buckets as 0 —
    but only between the first and the last day the period covers."""
    bucket = func.date_trunc(f.bucket, func.timezone(TZ, column)).label("bucket")
    rows = (
        await session.execute(
            stmt.with_only_columns(bucket, func.count())
            .where(*_within(column, f), column.is_not(None))
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    found = {row[0].date(): int(row[1]) for row in rows}
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


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


async def overview(session: AsyncSession, f: Filters) -> ResultsOverview:
    who = people(f)
    count_users = select(func.count(User.id)).where(User.id.in_(who))

    participants = await _count(
        session,
        count_users.where(
            User.status != UserStatus.DELETED, *([User.created_at < f.end] if f.end else [])
        ),
    )
    metrics = {
        "new_registrations": await _count(session, count_users.where(*_within(User.created_at, f))),
        "active_in_period": await _count(
            session,
            select(func.count(func.distinct(PageView.user_id))).where(
                PageView.user_id.in_(who), *_within(PageView.occurred_at, f)
            ),
        ),
        "onboarded": await _count(
            session, count_users.where(*_within(User.onboarding_completed_at, f))
        ),
        "diagnosed": await _count(
            session,
            select(func.count(func.distinct(Assessment.user_id))).where(
                Assessment.user_id.in_(who), *_within(Assessment.completed_at, f)
            ),
        ),
        "learners_started": await _count(
            session,
            select(func.count(func.distinct(Enrollment.user_id))).where(
                Enrollment.user_id.in_(who), *_within(_started(Enrollment), f)
            ),
        ),
        "course_completions": await _completions(session, who, f),
        "certificates": await _certificates(session, who, f),
        "paths_completed": await _count(
            session,
            select(func.count(UserLearningPath.id)).where(
                UserLearningPath.user_id.in_(who), *_within(UserLearningPath.completed_at, f)
            ),
        ),
        "tasks_submitted": await _count(
            session,
            select(func.count(TaskAttempt.id)).where(
                TaskAttempt.user_id.in_(who), *_within(TaskAttempt.submitted_at, f)
            ),
        ),
        "tasks_passed": await _count(
            session,
            select(func.count(TaskAttempt.id)).where(
                TaskAttempt.user_id.in_(who),
                TaskAttempt.status == TaskStatus.PASSED,
                *_within(TaskAttempt.evaluated_at, f),
            ),
        ),
        "portfolio_projects": await _count(
            session,
            select(func.count(PortfolioProject.id)).where(
                PortfolioProject.user_id.in_(who), *_within(PortfolioProject.created_at, f)
            ),
        ),
        "applications": await _applications(session, who, f, events=False),
        "event_registrations": await _applications(session, who, f, events=True),
        "outcomes_recorded": await _count(
            session,
            select(func.count(OutcomeRecord.id)).where(
                OutcomeRecord.user_id.in_(who), *_within(_when(OutcomeRecord), f)
            ),
        ),
    }
    first_visit = await session.scalar(
        select(func.min(PageView.occurred_at)).where(PageView.user_id.is_not(None))
    )

    now_people = select(User).where(User.id.in_(who), User.status != UserStatus.DELETED)
    if f.end is not None:
        now_people = now_people.where(User.created_at < f.end)
    region_rows = await session.execute(
        now_people.with_only_columns(User.region, func.count()).group_by(User.region)
    )
    regions = {(region.value if region else "unknown"): int(n) for region, n in region_rows}
    region_keys = [region.value for region in Region] + ["unknown"]

    return ResultsOverview(
        scope=_scope(f),
        participants=participants,
        metrics=[Metric(key=key, value=value) for key, value in metrics.items()],
        activity_since=(first_visit + OFFSET).date() if first_visit else None,
        registrations=await _series(
            session, "registrations", User.created_at, select(User.id).where(User.id.in_(who)), f
        ),
        regions=_suppressed(regions, region_keys),
        ages=_suppressed(
            await _ages(session, now_people), [b[0] for b in AGE_BRACKETS] + ["unknown"]
        ),
    )


async def _ages(session: AsyncSession, now_people: Select) -> dict[str, int]:
    """Age brackets from a recorded date of birth only. A profile with no date
    of birth is "unknown" — a stated bracket's lower bound would be a guess."""
    years = func.date_part("year", func.age(func.current_date(), Profile.birth_date))
    bracket = case(
        *[(years.between(low, high), key) for key, low, high in AGE_BRACKETS],
        else_="unknown",
    )
    rows = await session.execute(
        now_people.outerjoin(Profile, Profile.user_id == User.id)
        .with_only_columns(bracket.label("bracket"), func.count())
        .group_by("bracket")
    )
    return {key: int(n) for key, n in rows}


def _started(model):
    return func.coalesce(model.started_at, model.created_at)


def _when(model):
    return func.coalesce(model.occurred_at, model.created_at)


async def _completions(session: AsyncSession, who: Select, f: Filters) -> int:
    return await _count(
        session,
        select(func.count(Enrollment.id)).where(
            Enrollment.user_id.in_(who),
            Enrollment.status == EnrollmentStatus.COMPLETED,
            *_within(Enrollment.completed_at, f),
        ),
    )


async def _certificates(session: AsyncSession, who: Select, f: Filters) -> int:
    return await _count(
        session,
        select(func.count(Certificate.id)).where(
            Certificate.user_id.in_(who),
            Certificate.revoked_at.is_(None),
            *_within(Certificate.issued_at, f),
        ),
    )


def _applications_stmt(who: Select, f: Filters, *, events: bool) -> Select:
    kind = Opportunity.starts_at.is_not(None) if events else Opportunity.starts_at.is_(None)
    return (
        select(func.count(Application.id))
        .join(Opportunity, Opportunity.id == Application.opportunity_id)
        .where(
            Application.user_id.in_(who),
            Application.status.in_(_LIVE_APPLICATION),
            kind,
            *_within(Application.submitted_at, f),
        )
    )


async def _applications(session: AsyncSession, who: Select, f: Filters, *, events: bool) -> int:
    return await _count(session, _applications_stmt(who, f, events=events))


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


async def learning(session: AsyncSession, f: Filters) -> ResultsLearning:
    who = people(f)
    enrollments = select(func.count(Enrollment.id)).where(Enrollment.user_id.in_(who))
    started = await _count(session, enrollments.where(*_within(_started(Enrollment), f)))
    cohort_done = await _count(
        session,
        enrollments.where(
            *_within(_started(Enrollment), f), Enrollment.status == EnrollmentStatus.COMPLETED
        ),
    )
    paths = select(func.count(UserLearningPath.id)).where(UserLearningPath.user_id.in_(who))
    paths_started = await _count(session, paths.where(*_within(UserLearningPath.started_at, f)))
    paths_cohort_done = await _count(
        session,
        paths.where(
            *_within(UserLearningPath.started_at, f), UserLearningPath.completed_at.is_not(None)
        ),
    )
    attempts = select(func.count(TaskAttempt.id)).where(TaskAttempt.user_id.in_(who))
    evaluated = await _count(session, attempts.where(*_within(TaskAttempt.evaluated_at, f)))
    passed = await _count(
        session,
        attempts.where(
            TaskAttempt.status == TaskStatus.PASSED, *_within(TaskAttempt.evaluated_at, f)
        ),
    )
    by_evaluator = await session.execute(
        select(TaskAttempt.evaluator_kind, func.count())
        .where(
            TaskAttempt.user_id.in_(who),
            TaskAttempt.evaluator_kind.is_not(None),
            *_within(TaskAttempt.evaluated_at, f),
        )
        .group_by(TaskAttempt.evaluator_kind)
    )
    evaluators = {kind.value: int(n) for kind, n in by_evaluator}

    metrics = {
        "enrollments_started": started,
        "learners_started": await _count(
            session,
            select(func.count(func.distinct(Enrollment.user_id))).where(
                Enrollment.user_id.in_(who), *_within(_started(Enrollment), f)
            ),
        ),
        "in_progress_now": await _count(
            session,
            enrollments.where(
                Enrollment.status.in_((EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS))
            ),
        ),
        "course_completions": await _completions(session, who, f),
        "certificates": await _certificates(session, who, f),
        "paths_started": paths_started,
        "paths_completed": await _count(
            session, paths.where(*_within(UserLearningPath.completed_at, f))
        ),
        "tasks_started": await _count(session, attempts.where(*_within(_started(TaskAttempt), f))),
        "tasks_submitted": await _count(
            session, attempts.where(*_within(TaskAttempt.submitted_at, f))
        ),
        "tasks_evaluated": evaluated,
        "tasks_passed": passed,
    }
    return ResultsLearning(
        scope=_scope(f),
        metrics=[Metric(key=key, value=value) for key, value in metrics.items()],
        rates=[
            _rate("course_completion_rate", cohort_done, started),
            _rate("path_completion_rate", paths_cohort_done, paths_started),
            _rate("task_pass_rate", passed, evaluated),
        ],
        series=[
            await _series(
                session,
                "enrollments_started",
                _started(Enrollment),
                select(Enrollment.id).where(Enrollment.user_id.in_(who)),
                f,
            ),
            await _series(
                session,
                "course_completions",
                Enrollment.completed_at,
                select(Enrollment.id).where(
                    Enrollment.user_id.in_(who), Enrollment.status == EnrollmentStatus.COMPLETED
                ),
                f,
            ),
        ],
        evaluations=_groups(evaluators, [kind.value for kind in EvaluatorKind]),
    )


SORTS = ("enrolled", "completed", "completion", "certificates", "title")
LANGS = ("uz", "ru", "en")


async def programmes(
    session: AsyncSession,
    f: Filters,
    *,
    search: str | None = None,
    sort: str = "enrolled",
    descending: bool = True,
    page: int = 1,
    size: int = 20,
    lang: str = "uz",
) -> ProgrammeTable:
    """Every programme with the period's figures, sorted and paged in SQL.
    Sorting by name follows the reader's language (`lang`), falling back to
    the next written one — a list in English is in English alphabetical order."""
    who = people(f)
    mine = Enrollment.user_id.in_(who)
    in_period = and_(*_within(_started(Enrollment), f)) if f.start or f.end else true()
    done_in_period = and_(*_within(Enrollment.completed_at, f)) if f.start or f.end else true()

    agg = (
        select(
            Enrollment.program_id.label("program_id"),
            func.count().filter(in_period).label("enrolled"),
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
        )
        .where(mine)
        .group_by(Enrollment.program_id)
        .subquery()
    )
    certs = (
        select(Enrollment.program_id.label("program_id"), func.count(Certificate.id).label("n"))
        .join(Certificate, Certificate.enrollment_id == Enrollment.id)
        .where(mine, Certificate.revoked_at.is_(None), *_within(Certificate.issued_at, f))
        .group_by(Enrollment.program_id)
        .subquery()
    )
    tasks = (
        select(PracticalTask.program_id.label("program_id"), func.count(TaskAttempt.id).label("n"))
        .join(TaskAttempt, TaskAttempt.task_id == PracticalTask.id)
        .where(TaskAttempt.user_id.in_(who), *_within(TaskAttempt.submitted_at, f))
        .group_by(PracticalTask.program_id)
        .subquery()
    )
    enrolled = func.coalesce(agg.c.enrolled, 0)
    completed = func.coalesce(agg.c.completed, 0)
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
            func.coalesce(agg.c.in_progress, 0).label("in_progress"),
            completed.label("completed"),
            cohort_done.label("cohort_done"),
            func.coalesce(certs.c.n, 0).label("certificates"),
            func.coalesce(tasks.c.n, 0).label("tasks"),
        )
        .outerjoin(agg, agg.c.program_id == Program.id)
        .outerjoin(certs, certs.c.program_id == Program.id)
        .outerjoin(tasks, tasks.c.program_id == Program.id)
        # A draft nobody has touched is not a programme anybody used.
        .where(or_(Program.is_published.is_(True), agg.c.program_id.is_not(None)))
    )
    if search and search.strip():
        needle = f"%{search.strip()[:80]}%"
        base = base.where(
            or_(
                Program.title_i18n["uz"].astext.ilike(needle),
                Program.title_i18n["ru"].astext.ilike(needle),
                Program.title_i18n["en"].astext.ilike(needle),
            )
        )
    total = await _count(session, select(func.count()).select_from(base.subquery()))
    key = {
        "enrolled": enrolled,
        "completed": completed,
        "completion": rate,
        "certificates": func.coalesce(certs.c.n, 0),
        "title": title,
    }[sort if sort in SORTS else "enrolled"]
    order = key.desc().nullslast() if descending else key.asc().nullsfirst()
    rows = await session.execute(
        base.order_by(order, title.asc()).limit(size).offset((page - 1) * size)
    )
    items = [
        ProgrammeRow(
            id=program.id,
            slug=program.slug,
            title_i18n=program.title_i18n,
            is_published=program.is_published,
            enrolled=int(n_enrolled),
            in_progress=int(n_progress),
            completed=int(n_completed),
            completion=_rate("course_completion_rate", int(n_cohort), int(n_enrolled)),
            certificates=int(n_certs),
            tasks_submitted=int(n_tasks),
        )
        for program, n_enrolled, n_progress, n_completed, n_cohort, n_certs, n_tasks in rows
    ]
    return ProgrammeTable(scope=_scope(f), items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _tier_case():
    """The status each kind of evidence supports — the skill layer's own map."""
    by_status: dict[SkillStatus, list[EvidenceKind]] = {}
    for kind, status in EVIDENCE_STATUS.items():
        by_status.setdefault(status, []).append(kind)
    return case(
        *[(SkillEvidence.kind.in_(kinds), status.value) for status, kinds in by_status.items()],
        else_="other",
    )


async def skills(session: AsyncSession, f: Filters) -> ResultsSkills:
    who = people(f)
    theirs = (
        select(SkillEvidence)
        .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
        .where(
            UserSkill.user_id.in_(who),
            SkillEvidence.revoked_at.is_(None),
            *_within(SkillEvidence.created_at, f),
        )
    )
    tier = _tier_case().label("tier")
    by_tier = {
        key: int(n)
        for key, n in await session.execute(
            theirs.with_only_columns(tier, func.count()).group_by("tier")
        )
    }
    by_kind = {
        kind.value: int(n)
        for kind, n in await session.execute(
            theirs.with_only_columns(SkillEvidence.kind, func.count()).group_by(SkillEvidence.kind)
        )
    }

    top: dict[str, list[SkillCount]] = {}
    for status in (SkillStatus.LEARNED, SkillStatus.ASSESSED, SkillStatus.VERIFIED):
        kinds = [kind for kind, supports in EVIDENCE_STATUS.items() if supports == status]
        rows = await session.execute(
            theirs.where(SkillEvidence.kind.in_(kinds))
            .join(Skill, Skill.id == UserSkill.skill_id)
            .with_only_columns(Skill, func.count(func.distinct(UserSkill.user_id)).label("n"))
            .group_by(Skill.id)
            .order_by(func.count(func.distinct(UserSkill.user_id)).desc(), Skill.slug)
            .limit(TOP)
        )
        top[status.value] = [
            SkillCount(
                skill=SkillRef(
                    slug=skill.slug,
                    name_i18n=skill.name_i18n,
                    label=skill.slug,
                    category=skill.category,
                    dimensions=list(skill.dimensions or []),
                ),
                value=int(n),
            )
            for skill, n in rows
        ]

    held = select(UserSkill).where(UserSkill.user_id.in_(who))
    statuses = {
        (status.value if status else "none"): int(n)
        for status, n in await session.execute(
            held.with_only_columns(UserSkill.status, func.count()).group_by(UserSkill.status)
        )
    }
    levels = {
        (level.value if level else "unknown"): int(n)
        for level, n in await session.execute(
            held.where(UserSkill.status.is_not(None))
            .with_only_columns(UserSkill.level, func.count())
            .group_by(UserSkill.level)
        )
    }
    return ResultsSkills(
        scope=_scope(f),
        evidence=_groups(by_tier, [s.value for s in SkillStatus]),
        evidence_kinds=_groups(by_kind, [kind.value for kind in EvidenceKind]),
        top=top,
        statuses=_groups(statuses, [s.value for s in SkillStatus]),
        levels=_groups(levels),
    )


# ---------------------------------------------------------------------------
# Development Score
# ---------------------------------------------------------------------------


#: A dimension's people by band: below FOCUS_BELOW, between, from STRONG_FROM.
SPLIT = ["focus", "developing", "strong"]


async def score(session: AsyncSession, f: Filters) -> ResultsScore:
    """Her Development Score as it stands now. The period does not apply: a
    score is a reading of today, not an event."""
    who = people(f)
    mine = DevelopmentScore.user_id.in_(who)
    n_people = await _count(
        session, select(func.count(func.distinct(DevelopmentScore.user_id))).where(mine)
    )

    weight = case(
        *[(DevelopmentScore.dimension == dim, SCORE_WEIGHTS[dim]) for dim in ScoreDimension],
        else_=0.0,
    )
    per_person = (
        select(
            DevelopmentScore.user_id,
            (func.sum(weight * DevelopmentScore.current) / func.nullif(func.sum(weight), 0)).label(
                "composite"
            ),
        )
        .where(mine)
        .group_by(DevelopmentScore.user_id)
        .subquery()
    )
    bands = (
        ("0-19", 0, 20),
        ("20-39", 20, 40),
        ("40-59", 40, 60),
        ("60-79", 60, 80),
        ("80-100", 80, 101),
    )
    bucket = case(
        *[
            (and_(per_person.c.composite >= low, per_person.c.composite < high), key)
            for key, low, high in bands
        ],
        else_="0-19",
    )
    distribution = {
        key: int(n)
        for key, n in await session.execute(
            select(bucket.label("band"), func.count()).select_from(per_person).group_by("band")
        )
    }
    average = await session.scalar(select(func.avg(per_person.c.composite)))

    rows = await session.execute(
        select(
            DevelopmentScore.dimension,
            func.count(func.distinct(DevelopmentScore.user_id)),
            func.avg(DevelopmentScore.current),
            func.count().filter(DevelopmentScore.current < FOCUS_BELOW),
            func.count().filter(
                DevelopmentScore.current >= FOCUS_BELOW, DevelopmentScore.current < STRONG_FROM
            ),
            func.count().filter(DevelopmentScore.current >= STRONG_FROM),
        )
        .where(mine)
        .group_by(DevelopmentScore.dimension)
    )

    readings = {}
    for dimension, n, mean, *counts in rows:
        split = _suppressed(dict(zip(SPLIT, counts, strict=True)), SPLIT)
        readings[dimension.value] = DimensionReading(
            dimension=dimension.value,
            people=int(n),
            average=round(float(mean), 1) if n >= MIN_GROUP and mean is not None else None,
            **{group.key: group.value for group in split},
        )
    enough = n_people >= MIN_GROUP
    return ResultsScore(
        scope=_scope(f),
        people=n_people,
        small_sample=n_people < SMALL_SAMPLE,
        average=round(float(average), 1) if enough and average is not None else None,
        distribution=_suppressed(distribution, [band[0] for band in bands]) if enough else [],
        dimensions=sorted(
            (readings[dim.value] for dim in ScoreDimension if dim.value in readings),
            key=lambda reading: -(reading.focus or 0),
        )
        if enough
        else [],
    )


# ---------------------------------------------------------------------------
# Opportunities: work, business, outcomes
# ---------------------------------------------------------------------------


async def opportunities(session: AsyncSession, f: Filters) -> ResultsOpportunities:
    who = people(f)
    applied = (
        select(Application)
        .join(Opportunity, Opportunity.id == Application.opportunity_id)
        .where(
            Application.user_id.in_(who),
            Application.status.in_(_LIVE_APPLICATION),
            Opportunity.starts_at.is_(None),
            *_within(Application.submitted_at, f),
        )
    )

    async def grouped(stmt: Select, column) -> dict[str, int]:
        rows = await session.execute(stmt.with_only_columns(column, func.count()).group_by(column))
        return {getattr(key, "value", key) or "unknown": int(n) for key, n in rows}

    by_type = await grouped(applied, Opportunity.type)
    by_status = await grouped(applied, Application.status)
    by_source = await grouped(applied, Opportunity.source)
    total = sum(by_type.values())
    accepted = by_status.get(ApplicationStatus.ACCEPTED.value, 0)

    saved = await grouped(
        select(SavedOpportunity)
        .join(Opportunity, Opportunity.id == SavedOpportunity.opportunity_id)
        .where(
            SavedOpportunity.user_id.in_(who),
            Opportunity.starts_at.is_(None),
            *_within(SavedOpportunity.created_at, f),
        ),
        Opportunity.type,
    )
    now = datetime.now(UTC)
    open_now = await grouped(
        select(Opportunity).where(
            Opportunity.is_active.is_(True),
            Opportunity.starts_at.is_(None),
            or_(Opportunity.deadline.is_(None), Opportunity.deadline > now),
        ),
        Opportunity.type,
    )
    outcomes = await grouped(
        select(OutcomeRecord).where(
            OutcomeRecord.user_id.in_(who), *_within(_when(OutcomeRecord), f)
        ),
        OutcomeRecord.outcome_type,
    )
    invitations = select(func.count(OrganizationInvitation.id)).where(
        OrganizationInvitation.user_id.in_(who)
    )
    metrics = {
        "applications": total,
        "saved_listings": sum(saved.values()),
        "invitations_sent": await _count(
            session, invitations.where(*_within(OrganizationInvitation.created_at, f))
        ),
        "invitations_accepted": await _count(
            session,
            invitations.where(
                OrganizationInvitation.status == InvitationStatus.ACCEPTED,
                *_within(OrganizationInvitation.responded_at, f),
            ),
        ),
        "outcomes_recorded": sum(outcomes.values()),
    }
    types = [t.value for t in OpportunityType]
    return ResultsOpportunities(
        scope=_scope(f),
        metrics=[Metric(key=key, value=value) for key, value in metrics.items()],
        applications_by_type=[g for g in _groups(by_type, types) if g.value],
        applications_by_status=_groups(by_status, [s.value for s in _LIVE_APPLICATION]),
        applications_by_source=[
            g for g in _groups(by_source, [s.value for s in OpportunitySource]) if g.value
        ],
        saved_by_type=[g for g in _groups(saved, types) if g.value],
        open_by_type=[g for g in _groups(open_now, types) if g.value],
        outcomes=_groups(outcomes),
        rates=[_rate("application_accepted_rate", accepted, total)],
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def events(session: AsyncSession, f: Filters) -> ResultsEvents:
    who = people(f)
    now = datetime.now(UTC)
    event = Opportunity.starts_at.is_not(None)
    registrations = (
        select(Application)
        .join(Opportunity, Opportunity.id == Application.opportunity_id)
        .where(
            Application.user_id.in_(who),
            Application.status.in_(_LIVE_APPLICATION),
            event,
            *_within(Application.submitted_at, f),
        )
    )
    rows = await session.execute(
        registrations.with_only_columns(Opportunity.type, func.count()).group_by(Opportunity.type)
    )
    by_type = {kind.value: int(n) for kind, n in rows}
    rows = await session.execute(
        registrations.with_only_columns(Opportunity.format, func.count()).group_by(
            Opportunity.format
        )
    )
    by_format = {(fmt.value if fmt else "unknown"): int(n) for fmt, n in rows}
    top_rows = await session.execute(
        registrations.with_only_columns(
            Opportunity.id, Opportunity.title_i18n, func.count(Application.id).label("n")
        )
        .group_by(Opportunity.id, Opportunity.title_i18n)
        .order_by(func.count(Application.id).desc())
        .limit(TOP)
    )
    reminders = select(func.count(Notification.id)).where(
        Notification.user_id.in_(who),
        Notification.trigger == NotificationTrigger.EVENT_REMINDER,
        *_within(Notification.created_at, f),
    )
    metrics: dict[str, int | None] = {
        "events_added": await _count(
            session,
            select(func.count(Opportunity.id)).where(event, *_within(Opportunity.created_at, f)),
        ),
        "events_held": await _count(
            session,
            select(func.count(Opportunity.id)).where(event, *_within(Opportunity.starts_at, f)),
        ),
        "events_upcoming": await _count(
            session,
            select(func.count(Opportunity.id)).where(
                event,
                Opportunity.is_active.is_(True),
                func.coalesce(Opportunity.ends_at, Opportunity.starts_at) >= now,
            ),
        ),
        "event_registrations": sum(by_type.values()),
        "event_reminders": await _count(session, reminders),
        "events_saved": await _count(
            session,
            select(func.count(SavedOpportunity.id))
            .join(Opportunity, Opportunity.id == SavedOpportunity.opportunity_id)
            .where(
                SavedOpportunity.user_id.in_(who), event, *_within(SavedOpportunity.created_at, f)
            ),
        ),
        # Nothing records who came. Registrations are not attendance.
        "attendance": None,
    }
    return ResultsEvents(
        scope=_scope(f),
        metrics=[Metric(key=key, value=value) for key, value in metrics.items()],
        by_type=[g for g in _groups(by_type) if g.value],
        by_format=[g for g in _groups(by_format) if g.value],
        top=[Named(id=event_id, title_i18n=title, value=int(n)) for event_id, title, n in top_rows],
        series=[
            await _series(
                session,
                "event_registrations",
                Application.submitted_at,
                registrations.with_only_columns(Application.id),
                f,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def export_rows(session: AsyncSession, f: Filters) -> list[list]:
    """The headline figures with their definitions, for a report."""
    head = await overview(session, f)
    learn = await learning(session, f)
    opp = await opportunities(session, f)
    ev = await events(session, f)
    rows: list[list] = [["participants", head.participants, "", "", DEFINITIONS["participants"]]]
    seen = {"participants"}
    for metric in [*head.metrics, *learn.metrics, *opp.metrics, *ev.metrics]:
        if metric.key in seen:
            continue
        seen.add(metric.key)
        rows.append(
            [
                metric.key,
                "not recorded" if metric.value is None else metric.value,
                "",
                "",
                DEFINITIONS.get(metric.key, ""),
            ]
        )
    for rate in [*learn.rates, *opp.rates]:
        rows.append(
            [
                rate.key,
                "no data" if rate.value is None else rate.value,
                rate.numerator,
                rate.denominator,
                DEFINITIONS.get(rate.key, ""),
            ]
        )
    return rows
