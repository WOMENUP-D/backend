"""Results & impact: every figure counted from records, in its own period,
within the caller's scope, and never more than the records say.

The fixtures below insert the records a real journey leaves behind — an
account, an enrollment, an evaluated attempt, an application — at chosen
times, so each test can say exactly what the answer must be. They live only in
the per-test schema the conftest drops afterwards.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.constants import (
    ApplicationStatus,
    EnrollmentStatus,
    EvaluatorKind,
    EventFormat,
    EvidenceKind,
    Language,
    OpportunitySource,
    OpportunityType,
    ProgramCategory,
    ProgramFormat,
    Region,
    Role,
    ScoreDimension,
    SkillCategory,
    SkillStatus,
    TaskStatus,
    UserStatus,
)
from app.core.security import create_token
from app.models.analytics import PageView
from app.models.assessment import DevelopmentScore
from app.models.audit import AuditLog
from app.models.learning_path import LearningPath, UserLearningPath
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User, UserRole
from app.services.results import DEFINITIONS, MIN_GROUP

API = "/api/v1/admin/results"
ENDPOINTS = ("overview", "learning", "programmes", "skills", "score", "opportunities", "events")
MARCH = {"date_from": "2026-03-01", "date_to": "2026-03-31"}


def at(day: str, hour: int = 12, minute: int = 0) -> datetime:
    """A moment on a Tashkent calendar day, as the UTC time the database holds."""
    local = datetime.fromisoformat(day).replace(hour=hour, minute=minute)
    return (local - timedelta(hours=5)).replace(tzinfo=UTC)


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _token(user_id: uuid.UUID, *roles: Role) -> dict[str, str]:
    roles = roles or (Role.USER,)
    token = create_token(user_id, roles[0], "access", {"roles": [r.value for r in roles]})
    return {"Authorization": f"Bearer {token}"}


async def _person(
    session,
    *,
    region: Region | None = Region.SAMARKAND,
    born: date | None = None,
    profile: bool = True,
    created: datetime | None = None,
    roles: tuple[Role, ...] = (Role.USER,),
    status: UserStatus = UserStatus.ACTIVE,
    scope: Region | None = None,
    email: str | None = None,
    name: str | None = None,
) -> uuid.UUID:
    user = User(
        phone=_phone(),
        email=email,
        language=Language.UZ,
        region=region,
        status=status,
        created_at=created or at("2026-01-15"),
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(
            UserRole(
                user_id=user.id,
                role=role,
                scope_region=scope if role == Role.REGIONAL_COORDINATOR else None,
            )
        )
    if profile:
        session.add(Profile(user_id=user.id, birth_date=born, full_name=name))
    await session.flush()
    return user.id


async def _staff(session, role: Role = Role.ADMIN, **kwargs) -> dict[str, str]:
    """A staff account with a row of its own: exports are audited, and
    `audit_logs.actor_id` is a foreign key."""
    user_id = await _person(session, roles=(role,), **kwargs)
    return _token(user_id, role)


def _years_ago(years: int) -> date:
    """A birth date that makes her exactly `years` old today."""
    today = date.today()
    return today.replace(year=today.year - years, day=1) - timedelta(days=1)


async def _program(session, title: str = "Moliya asoslari", *, published: bool = True) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": title, "ru": f"{title} (ru)", "en": f"{title} (en)"},
        category=ProgramCategory.FINANCIAL_LITERACY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        is_published=published,
    )
    session.add(program)
    await session.flush()
    return program


async def _enroll(
    session,
    user_id: uuid.UUID,
    program: Program,
    started: datetime,
    *,
    completed: datetime | None = None,
    status: EnrollmentStatus | None = None,
) -> Enrollment:
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=status
        or (EnrollmentStatus.COMPLETED if completed else EnrollmentStatus.IN_PROGRESS),
        started_at=started,
        completed_at=completed,
        created_at=started,
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


async def _certificate(
    session, enrollment: Enrollment, issued: datetime, *, revoked: bool = False
) -> None:
    session.add(
        Certificate(
            enrollment_id=enrollment.id,
            user_id=enrollment.user_id,
            serial_number=uuid.uuid4().hex[:12],
            verification_code=uuid.uuid4().hex,
            issued_at=issued,
            revoked_at=issued + timedelta(days=1) if revoked else None,
        )
    )
    await session.flush()


async def _listing(
    session, kind: OpportunityType, *, starts: datetime | None = None, created=None
) -> Opportunity:
    listing = Opportunity(
        source=OpportunitySource.INTERNAL,
        type=kind,
        title_i18n={"uz": f"{kind.value} e'loni"},
        starts_at=starts,
        created_at=created or at("2026-01-10"),
    )
    session.add(listing)
    await session.flush()
    return listing


async def _apply(
    session,
    user_id: uuid.UUID,
    listing: Opportunity,
    submitted: datetime | None,
    status: ApplicationStatus = ApplicationStatus.SUBMITTED,
) -> None:
    session.add(
        Application(
            user_id=user_id, opportunity_id=listing.id, status=status, submitted_at=submitted
        )
    )
    await session.flush()


async def _get(client, path: str, headers, **params) -> dict:
    response = await client.get(f"{API}/{path}", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _metric(body: dict, key: str):
    return next(m["value"] for m in body["metrics"] if m["key"] == key)


def _rate(body: dict, key: str) -> dict:
    return next(r for r in body["rates"] if r["key"] == key)


# ---------------------------------------------------------------------------
# Who may read the figures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_staff_read_results(client, session):
    for path in (*ENDPOINTS, "export"):
        assert (await client.get(f"{API}/{path}")).status_code == 401, path

    for role in (Role.USER, Role.MOTHER, Role.MENTOR, Role.TRAINER, Role.PARTNER):
        headers = _token(uuid.uuid4(), role)
        for path in (*ENDPOINTS, "export"):
            response = await client.get(f"{API}/{path}", headers=headers)
            assert response.status_code == 403, (role, path)

    for role in (Role.ADMIN, Role.MODERATOR):
        headers = await _staff(session, role)
        for path in (*ENDPOINTS, "export"):
            response = await client.get(f"{API}/{path}", headers=headers)
            assert response.status_code == 200, (role, path, response.text)


@pytest.mark.asyncio
async def test_a_coordinator_sees_her_region_whatever_she_asks_for(client, session):
    for _ in range(2):
        await _person(session, region=Region.SAMARKAND)
    for _ in range(3):
        await _person(session, region=Region.BUKHARA)

    # Her scope comes from her role assignment, not from the token or the query.
    coordinator = await _staff(
        session, Role.REGIONAL_COORDINATOR, region=Region.TASHKENT_CITY, scope=Region.SAMARKAND
    )
    body = await _get(client, "overview", coordinator, region="bukhara")
    assert body["scope"]["region"] == "samarkand"
    assert body["participants"] == 2

    # With no scope on the role, the region on her account is the scope.
    fallback = await _staff(session, Role.REGIONAL_COORDINATOR, region=Region.BUKHARA)
    assert (await _get(client, "overview", fallback))["participants"] == 3

    # A coordinator nobody gave a region is refused, not shown the country.
    nowhere = await _staff(session, Role.REGIONAL_COORDINATOR, region=None)
    response = await client.get(f"{API}/overview", headers=nowhere)
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "no_region_scope"

    # The panel's user list follows the same rule.
    listed = await client.get(
        "/api/v1/admin/users", params={"region": "bukhara"}, headers=coordinator
    )
    assert {row["region"] for row in listed.json()} == {"samarkand"}

    admin = await _staff(session)
    assert (await _get(client, "overview", admin, region="bukhara"))["participants"] == 3
    # Staff accounts are not participants, wherever they are.
    assert (await _get(client, "overview", admin))["participants"] == 5


# ---------------------------------------------------------------------------
# Counting people
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_participants_are_women_not_staff_and_deleted_accounts_are_history(client, session):
    march = at("2026-03-10")
    kept = [await _person(session, created=march) for _ in range(2)]
    kept.append(await _person(session, created=march, roles=(Role.MOTHER,)))
    await _person(session, created=march, roles=(Role.USER, Role.MENTOR))  # also staff
    await _person(session, created=march, roles=(Role.TRAINER,))
    await _person(session, created=march, status=UserStatus.DELETED)
    admin = await _staff(session)  # created in January

    body = await _get(client, "overview", admin, **MARCH)
    assert body["participants"] == 3
    # A deleted account's registration still happened in March.
    assert _metric(body, "new_registrations") == 4

    # Signed-in visits: two participants, one of them twice; staff not counted.
    for user_id in (kept[0], kept[0], kept[1]):
        session.add(PageView(visitor_hash="x", path="/", user_id=user_id, occurred_at=march))
    staff_id = await _person(session, roles=(Role.MODERATOR,))
    session.add(PageView(visitor_hash="y", path="/", user_id=staff_id, occurred_at=march))
    session.add(PageView(visitor_hash="z", path="/", user_id=None, occurred_at=at("2026-03-02")))
    await session.flush()

    body = await _get(client, "overview", admin, **MARCH)
    assert _metric(body, "active_in_period") == 2
    assert body["activity_since"] == "2026-03-10"


@pytest.mark.asyncio
async def test_the_period_is_whole_tashkent_days(client, session):
    admin = await _staff(session)
    # 00:30 on 1 March in Tashkent is still 28 February in UTC.
    await _person(session, created=at("2026-03-01", 0, 30))
    await _person(session, created=at("2026-03-03", 23, 30))
    await _person(session, created=at("2026-03-04", 0, 10))

    body = await _get(client, "overview", admin, date_from="2026-03-01", date_to="2026-03-03")
    assert _metric(body, "new_registrations") == 2
    # Participants are counted as they stood at the end of the period.
    assert body["participants"] == 2
    assert body["scope"]["period"]["bucket"] == "day"
    points = body["registrations"]["points"]
    assert [(p["bucket"], p["value"]) for p in points] == [
        ("2026-03-01", 1),
        ("2026-03-02", 0),
        ("2026-03-03", 1),
    ]

    reversed_period = await client.get(
        f"{API}/overview",
        params={"date_from": "2026-03-05", "date_to": "2026-03-01"},
        headers=admin,
    )
    assert reversed_period.status_code == 422
    assert reversed_period.json()["detail"]["reason"] == "period_reversed"


@pytest.mark.asyncio
async def test_an_empty_period_is_zero_and_its_rates_are_no_data(client, session):
    admin = await _staff(session)
    program = await _program(session)
    learner = await _person(session)
    await _enroll(session, learner, program, at("2026-03-05"), completed=at("2026-03-09"))

    empty = {"date_from": "2025-06-01", "date_to": "2025-06-30"}
    learning = await _get(client, "learning", admin, **empty)
    assert _metric(learning, "enrollments_started") == 0
    assert _metric(learning, "course_completions") == 0
    for rate in learning["rates"]:
        assert rate["denominator"] == 0 and rate["value"] is None, rate
    # An empty June still has 30 days, each of them 0 — not a missing line.
    series = learning["series"][0]["points"]
    assert len(series) == 30 and {p["value"] for p in series} == {0}

    opportunities = await _get(client, "opportunities", admin, **empty)
    assert _rate(opportunities, "application_accepted_rate")["value"] is None


@pytest.mark.asyncio
async def test_unknown_ages_stay_unknown_and_small_groups_are_not_shown(client, session):
    admin = await _staff(session, region=None)
    for _ in range(5):
        await _person(session, born=_years_ago(30))
    for _ in range(2):
        await _person(session, born=_years_ago(20), region=Region.NAVOI)
    # No date of birth: unknown, even with a stated age bracket on the profile.
    for _ in range(3):
        await _person(session, born=None)
    for _ in range(3):
        await _person(session, profile=False, region=None)

    body = await _get(client, "overview", admin)
    assert body["participants"] == 13
    ages = {g["key"]: g for g in body["ages"]}
    assert ages["unknown"]["value"] == 6
    assert ages["45-54"] == {"key": "45-54", "value": 0, "suppressed": False}
    # Two people aged 20: hidden. Alone, they could be read back as 13 minus
    # the rest, so the next smallest group (five aged 30) is hidden with them.
    assert ages["18-24"] == {"key": "18-24", "value": None, "suppressed": True}
    assert ages["25-34"] == {"key": "25-34", "value": None, "suppressed": True}

    regions = {g["key"]: g for g in body["regions"]}
    assert regions["samarkand"]["value"] == 8
    assert regions["navoi"]["suppressed"] is True and regions["navoi"]["value"] is None
    # Three participants have no region: unknown, and too few to show.
    assert regions["unknown"]["suppressed"] is True
    # The documented threshold (docs/architecture.md).
    assert MIN_GROUP == 5


@pytest.mark.asyncio
async def test_no_personal_details_reach_the_figures(client, session):
    admin = await _staff(session)
    user_id = await _person(
        session, email="malika.private@example.uz", name="Malika Yusupova", born=_years_ago(27)
    )
    program = await _program(session)
    await _enroll(session, user_id, program, at("2026-03-05"))
    phone = (await session.get(User, user_id)).phone

    texts = [(await client.get(f"{API}/{path}", headers=admin)).text for path in ENDPOINTS] + [
        (await client.get(f"{API}/export", headers=admin)).text
    ]
    for text in texts:
        for secret in ("malika.private", "Malika", "Yusupova", phone, str(user_id)):
            assert secret not in text


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learning_counts_each_record_on_its_own_date(client, session):
    admin = await _staff(session)
    money = await _program(session, "Moliya asoslari")
    sewing = await _program(session, "Tikuvchilik")
    a, b, c, d = [await _person(session) for _ in range(4)]
    staff_id = await _person(session, roles=(Role.TRAINER,))

    done = await _enroll(session, a, money, at("2026-03-05"), completed=at("2026-03-20"))
    await _certificate(session, done, at("2026-03-20"))
    await _enroll(session, a, sewing, at("2026-03-06"))  # a second course, same learner
    await _enroll(session, b, money, at("2026-03-10"))
    early = await _enroll(session, c, money, at("2026-02-10"), completed=at("2026-03-02"))
    await _certificate(session, early, at("2026-03-02"), revoked=True)
    await _enroll(session, d, money, at("2026-03-12"), status=EnrollmentStatus.DROPPED)
    await _enroll(session, staff_id, money, at("2026-03-05"), completed=at("2026-03-06"))

    body = await _get(client, "learning", admin, **MARCH)
    assert _metric(body, "enrollments_started") == 4
    assert _metric(body, "learners_started") == 3  # a counted once
    assert _metric(body, "in_progress_now") == 2
    # c finished in March after starting in February: a March completion...
    assert _metric(body, "course_completions") == 2
    # ...but not in the March cohort's rate, whose denominator is March starts.
    rate = _rate(body, "course_completion_rate")
    assert (rate["numerator"], rate["denominator"], rate["value"]) == (1, 4, 25.0)
    # A revoked certificate is not a certificate.
    assert _metric(body, "certificates") == 1


@pytest.mark.asyncio
async def test_practical_tasks_and_paths_have_their_own_denominators(client, session):
    admin = await _staff(session)
    program = await _program(session)
    task = PracticalTask(
        slug=f"task-{uuid.uuid4().hex[:6]}", title_i18n={"uz": "Byudjet"}, program_id=program.id
    )
    path = LearningPath(slug=f"path-{uuid.uuid4().hex[:6]}", title_i18n={"uz": "Yoʻl"})
    session.add_all([task, path])
    await session.flush()
    a, b = await _person(session), await _person(session)

    def attempt(user_id, no, status, submitted: str, evaluated: str | None = None, by=None):
        return TaskAttempt(
            task_id=task.id,
            user_id=user_id,
            attempt_no=no,
            status=status,
            started_at=at(submitted) - timedelta(hours=2),
            submitted_at=at(submitted),
            evaluated_at=at(evaluated) if evaluated else None,
            evaluator_kind=by,
        )

    session.add_all(
        [
            attempt(
                a, 1, TaskStatus.NEEDS_IMPROVEMENT, "2026-03-06", "2026-03-07", EvaluatorKind.AI
            ),
            attempt(a, 2, TaskStatus.PASSED, "2026-03-08", "2026-03-09", EvaluatorKind.MENTOR),
            attempt(b, 1, TaskStatus.SUBMITTED, "2026-03-10"),
            UserLearningPath(
                user_id=a,
                path_id=path.id,
                started_at=at("2026-03-01"),
                completed_at=at("2026-03-25"),
            ),
            UserLearningPath(user_id=b, path_id=path.id, started_at=at("2026-03-02")),
        ]
    )
    await session.flush()

    body = await _get(client, "learning", admin, **MARCH)
    assert _metric(body, "tasks_submitted") == 3
    assert _metric(body, "tasks_evaluated") == 2
    assert _metric(body, "tasks_passed") == 1
    assert _rate(body, "task_pass_rate")["value"] == 50.0
    # Every kind of evaluator is listed, the ones with none as 0.
    assert {g["key"]: g["value"] for g in body["evaluations"]} == {
        kind.value: {EvaluatorKind.AI: 1, EvaluatorKind.MENTOR: 1}.get(kind, 0)
        for kind in EvaluatorKind
    }
    assert _metric(body, "paths_started") == 2
    assert _metric(body, "paths_completed") == 1
    assert _rate(body, "path_completion_rate")["value"] == 50.0

    table = await _get(client, "programmes", admin, **MARCH)
    assert table["items"][0]["tasks_submitted"] == 3


@pytest.mark.asyncio
async def test_the_programme_table_sorts_searches_and_pages_on_the_server(client, session):
    admin = await _staff(session)
    money = await _program(session, "Moliya asoslari")
    sewing = await _program(session, "Tikuvchilik")
    await _program(session, "Qoralama", published=False)  # nobody used it: not listed
    pilot = await _program(session, "Sinov kursi", published=False)
    people = [await _person(session) for _ in range(3)]
    for person in people:
        await _enroll(session, person, money, at("2026-03-05"))
    await _enroll(session, people[0], sewing, at("2026-03-05"), completed=at("2026-03-15"))
    await _enroll(session, people[1], pilot, at("2026-01-05"))

    table = await _get(client, "programmes", admin, **MARCH)
    assert table["total"] == 3
    rows = {row["title_i18n"]["uz"]: row for row in table["items"]}
    assert set(rows) == {"Moliya asoslari", "Tikuvchilik", "Sinov kursi"}
    assert [row["title_i18n"]["uz"] for row in table["items"]][0] == "Moliya asoslari"
    assert rows["Tikuvchilik"]["completion"]["value"] == 100.0
    # Nobody started the pilot in March: its rate has no denominator, not 0%.
    assert rows["Sinov kursi"]["enrolled"] == 0
    assert rows["Sinov kursi"]["completion"]["value"] is None
    assert rows["Sinov kursi"]["in_progress"] == 1

    by_title = await _get(client, "programmes", admin, sort="title", order="asc", size=1, page=2)
    assert by_title["total"] == 3 and len(by_title["items"]) == 1
    assert by_title["items"][0]["title_i18n"]["uz"] == "Sinov kursi"

    found = await _get(client, "programmes", admin, search="MOLIYA")
    assert [row["title_i18n"]["uz"] for row in found["items"]] == ["Moliya asoslari"]
    found = await _get(client, "programmes", admin, search="tikuvchilik (ru)")
    assert found["total"] == 1

    rejected = await client.get(f"{API}/programmes", params={"sort": "phone"}, headers=admin)
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_programme_names_sort_in_the_readers_language(client, session):
    admin = await _staff(session)
    for uz, en in (("Asalarichilik", "Beekeeping"), ("Zamonaviy kasblar", "Apple growing")):
        program = await _program(session, uz)
        program.title_i18n = {"uz": uz, "en": en}
    await session.flush()

    def names(body, lang):
        return [row["title_i18n"][lang] for row in body["items"]]

    params = {"sort": "title", "order": "asc"}
    uz = await _get(client, "programmes", admin, **params)
    assert names(uz, "uz") == ["Asalarichilik", "Zamonaviy kasblar"]
    en = await _get(client, "programmes", admin, **params, lang="en")
    assert names(en, "en") == ["Apple growing", "Beekeeping"]
    wrong = await client.get(f"{API}/programmes", params={"lang": "fr"}, headers=admin)
    assert wrong.status_code == 422


# ---------------------------------------------------------------------------
# Skills and the Development Score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learned_assessed_and_verified_skills_are_counted_apart(client, session):
    admin = await _staff(session)

    async def skill(slug: str) -> Skill:
        record = Skill(slug=slug, name_i18n={"uz": slug}, category=SkillCategory.PROFESSIONAL)
        session.add(record)
        await session.flush()
        return record

    budget, sewing = await skill("budgeting"), await skill("sewing")
    a, b, c = [await _person(session) for _ in range(3)]

    async def hold(user_id, skill_, status, *kinds, revoked=()):
        held = UserSkill(user_id=user_id, skill_id=skill_.id, status=status)
        session.add(held)
        await session.flush()
        for kind in kinds:
            session.add(
                SkillEvidence(
                    user_skill_id=held.id,
                    kind=kind,
                    source_type="test",
                    source_id=uuid.uuid4().hex,
                    created_at=at("2026-03-10"),
                    revoked_at=at("2026-03-11") if kind in revoked else None,
                )
            )
        await session.flush()

    # a: taught twice (course + certificate) and assessed once.
    await hold(
        a,
        budget,
        SkillStatus.ASSESSED,
        EvidenceKind.COURSE_COMPLETION,
        EvidenceKind.CERTIFICATE,
        EvidenceKind.AI_ASSESSMENT,
    )
    # b: a mentor put their name to it; an employer's word was withdrawn.
    await hold(
        b,
        budget,
        SkillStatus.VERIFIED,
        EvidenceKind.MENTOR_ASSESSMENT,
        EvidenceKind.EMPLOYER_ASSESSMENT,
        revoked=(EvidenceKind.EMPLOYER_ASSESSMENT,),
    )
    await hold(c, sewing, SkillStatus.SELF_REPORTED, EvidenceKind.SELF_REPORTED)

    body = await _get(client, "skills", admin, **MARCH)
    evidence = {g["key"]: g["value"] for g in body["evidence"]}
    assert evidence == {"self_reported": 1, "learned": 2, "assessed": 1, "verified": 1}
    # The top lists count people, not pieces of evidence.
    top = {
        status: [(s["skill"]["slug"], s["value"]) for s in rows]
        for status, rows in body["top"].items()
    }
    assert top == {
        "learned": [("budgeting", 1)],
        "assessed": [("budgeting", 1)],
        "verified": [("budgeting", 1)],
    }
    statuses = {g["key"]: g["value"] for g in body["statuses"]}
    assert statuses["assessed"] == 1 and statuses["verified"] == 1 and statuses["learned"] == 0

    # A verified skill is not a job.
    career = await _get(client, "opportunities", admin, **MARCH)
    assert _metric(career, "outcomes_recorded") == 0 and career["outcomes"] == []


@pytest.mark.asyncio
async def test_the_score_names_its_sample_and_needs_enough_people(client, session):
    admin = await _staff(session)

    async def scored(value: float, *, roles=(Role.USER,)) -> None:
        user_id = await _person(session, roles=roles)
        for dimension in ScoreDimension:
            session.add(DevelopmentScore(user_id=user_id, dimension=dimension, current=value))
        await session.flush()

    for _ in range(3):
        await scored(50)
    await scored(10, roles=(Role.MENTOR,))  # staff: not in the sample

    small = await _get(client, "score", admin)
    assert small["people"] == 3 and small["small_sample"] is True
    assert small["average"] is None
    assert small["distribution"] == [] and small["dimensions"] == []

    await scored(50)
    await scored(50)
    await scored(90)
    body = await _get(client, "score", admin)
    assert body["people"] == 6 and body["small_sample"] is True
    assert body["average"] == pytest.approx(56.7)
    # One person scores 90: hidden, and the five beside her too — with six
    # on screen, "5 in 40-59" would say where the sixth is.
    bands = {g["key"]: g for g in body["distribution"]}
    assert bands["80-100"] == {"key": "80-100", "value": None, "suppressed": True}
    assert bands["40-59"] == {"key": "40-59", "value": None, "suppressed": True}
    assert bands["0-19"]["value"] == 0
    first = body["dimensions"][0]
    assert first["people"] == 6 and first["focus"] == 0
    assert first["developing"] is None and first["strong"] is None


# ---------------------------------------------------------------------------
# Career and events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_application_is_not_employment(client, session):
    admin = await _staff(session)
    vacancy = await _listing(session, OpportunityType.VACANCY)
    internship = await _listing(session, OpportunityType.INTERNSHIP)
    event = await _listing(session, OpportunityType.WORKSHOP, starts=at("2026-03-20"))
    a, b, c = [await _person(session) for _ in range(3)]
    program = await _program(session)
    await _enroll(session, a, program, at("2026-03-01"), completed=at("2026-03-05"))

    await _apply(session, a, vacancy, at("2026-03-10"))
    await _apply(session, a, internship, at("2026-03-11"), ApplicationStatus.ACCEPTED)
    await _apply(session, b, event, at("2026-03-12"))
    await _apply(session, c, vacancy, None, ApplicationStatus.DRAFT)

    body = await _get(client, "opportunities", admin, **MARCH)
    assert _metric(body, "applications") == 2  # not the event, not the draft
    assert _metric(body, "outcomes_recorded") == 0
    assert body["outcomes"] == []
    rate = _rate(body, "application_accepted_rate")
    assert (rate["numerator"], rate["denominator"], rate["value"]) == (1, 2, 50.0)
    assert {g["key"]: g["value"] for g in body["applications_by_type"]} == {
        "vacancy": 1,
        "internship": 1,
    }

    overview = await _get(client, "overview", admin, **MARCH)
    assert _metric(overview, "applications") == 2
    assert _metric(overview, "event_registrations") == 1
    assert _metric(overview, "outcomes_recorded") == 0

    # Only a recorded outcome is an outcome.
    session.add(
        OutcomeRecord(
            user_id=a,
            source=OpportunitySource.EDU_JOB,
            outcome_type="employment",
            occurred_at=at("2026-03-28"),
        )
    )
    await session.flush()
    body = await _get(client, "opportunities", admin, **MARCH)
    assert _metric(body, "outcomes_recorded") == 1
    assert body["outcomes"] == [{"key": "employment", "value": 1, "suppressed": False}]


@pytest.mark.asyncio
async def test_events_count_registrations_and_never_claim_attendance(client, session):
    admin = await _staff(session)
    held = await _listing(
        session, OpportunityType.SEMINAR, starts=at("2026-03-15"), created=at("2026-03-01")
    )
    held.format = EventFormat.OFFLINE
    await _listing(
        session,
        OpportunityType.FORUM,
        starts=datetime.now(UTC) + timedelta(days=30),
        created=at("2026-02-01"),
    )
    for _ in range(2):
        await _apply(session, await _person(session), held, at("2026-03-05"))
    await session.flush()

    body = await _get(client, "events", admin, **MARCH)
    assert _metric(body, "events_added") == 1
    assert _metric(body, "events_held") == 1
    assert _metric(body, "events_upcoming") == 1
    assert _metric(body, "event_registrations") == 2
    assert _metric(body, "attendance") is None
    assert [(item["id"], item["value"]) for item in body["top"]] == [(str(held.id), 2)]
    assert body["by_format"] == [{"key": "offline", "value": 2, "suppressed": False}]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_export_carries_definitions_and_is_audited(client, session):
    admin = await _staff(session)
    response = await client.get(f"{API}/export", params=MARCH, headers=admin)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "womanup-results-2026-03-01-2026-03-31.csv" in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == ["period_from", "2026-03-01", "period_to", "2026-03-31"]
    assert rows[1] == ["region", "all"]
    assert rows[3] == ["metric", "value", "numerator", "denominator", "definition"]
    figures = {row[0]: row for row in rows[4:]}
    for key, row in figures.items():
        assert row[4] == DEFINITIONS[key], key
    assert figures["attendance"][1] == "not recorded"
    assert figures["course_completion_rate"][1:4] == ["no data", "0", "0"]
    assert figures["participants"][1] == "0"

    logged = (
        await session.scalars(select(AuditLog).where(AuditLog.action == "results.export"))
    ).all()
    assert len(logged) == 1
    assert logged[0].changes == {"date_from": "2026-03-01", "date_to": "2026-03-31", "region": None}
