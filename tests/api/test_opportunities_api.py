"""Jobs and internships: discovering, applying and tracking — without inventing.

What is pinned here, in order of how badly it would hurt to get wrong:

* Nothing leaves the platform without her consent. Applying to a partner
  listing without consent creates nothing; confirming on the page appends a
  consent row first; what the partner receives is `minimal_profile_payload`
  and nothing more; a partner's internal note never reaches the browser.
* Eligibility is enforced on the server — closed listings, stated age rules,
  and a girl known to be under 18 — whatever the page shows.
* Her fit is explained from records only: the skill, how she holds it, the
  course that taught it, her career direction, her region.
* The catalogue names only live records, and says so when nothing matches.
* Her applications, saved listings and fit are hers alone.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.constants import (
    ApplicationStatus,
    CareerCategory,
    ConsentScope,
    Language,
    LessonKind,
    OpportunitySource,
    OpportunityType,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    Region,
    Role,
    SkillCategory,
)
from app.core.security import create_token
from app.models.audit import AuditLog
from app.models.career_path import CareerPath
from app.models.consent import ConsentLog
from app.models.integration import IntegrationEvent
from app.models.opportunity import Application, Opportunity, SavedOpportunity
from app.models.profile import Profile
from app.models.program import Program, ProgramLesson, ProgramModule
from app.models.skill import Skill
from app.models.user import User
from app.services import ai_coach, learning
from app.services.opportunities import SHARED_FIELDS

BASE = "/api/v1/opportunities"
DISCOVER = f"{BASE}/discover"
MINE = f"{BASE}/me/applications"
SAVED = f"{BASE}/me/saved"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _woman(
    session,
    user_id: uuid.UUID,
    *,
    born: date | None = date(1992, 3, 1),
    skills=(),
    region: Region | None = Region.TASHKENT_CITY,
) -> Profile:
    session.add(
        User(
            id=user_id,
            phone=_phone(),
            email=f"{uuid.uuid4().hex[:8]}@example.uz",
            language=Language.UZ,
            region=region,
        )
    )
    await session.flush()
    profile = Profile(
        user_id=user_id,
        full_name="Malika Yusupova",
        birth_date=born,
        skills=list(skills),
        profession="Hisobchi",
        years_of_experience=2,
    )
    session.add(profile)
    await session.flush()
    return profile


async def _skill(session, slug: str, label: str, *aliases: str) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.PROFESSIONAL,
        dimensions=[],
        aliases=[slug, label.lower(), *aliases],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _listing(
    session,
    *,
    skills=("buxgalteriya", "excel"),
    kind: OpportunityType = OpportunityType.VACANCY,
    source: OpportunitySource = OpportunitySource.EDU_JOB,
    region: str | None = "tashkent_city",
    days: int | None = 20,
    active: bool = True,
    rules: dict | None = None,
    title: str = "Buxgalter (kichik biznes)",
    organisation: str = "Alfa Savdo",
) -> Opportunity:
    item = Opportunity(
        source=source,
        external_id=uuid.uuid4().hex,
        type=kind,
        title_i18n={"uz": title, "ru": f"{title} (ru)", "en": f"{title} (en)"},
        description_i18n={"uz": "Tavsif"},
        organisation=organisation,
        region=region,
        required_skills=list(skills),
        eligibility=rules or {},
        reward={},
        deadline=datetime.now(UTC) + timedelta(days=days) if days is not None else None,
        is_active=active,
    )
    session.add(item)
    await session.flush()
    return item


async def _course(session, *, taught: list[str], title: str = "Buxgalteriya asoslari") -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": title, "en": title},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=ProficiencyLevel.BEGINNER,
        learning_outcomes=[],
        skills_taught=taught,
        has_certificate=False,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "Modul"})
    session.add(module)
    await session.flush()
    session.add(
        ProgramLesson(
            module_id=module.id,
            order_index=0,
            slug="1-dars",
            title_i18n={"uz": "Dars"},
            kind=LessonKind.READING,
            blocks=[],
        )
    )
    await session.flush()
    return program


async def _finish(session, user_id: uuid.UUID, program: Program) -> None:
    enrollment = await learning.enroll(session, user_id=user_id, program_id=program.id)
    lesson_id = await session.scalar(
        select(ProgramLesson.id)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program.id)
    )
    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lesson_id, program=program)


async def _consent(session, user_id: uuid.UUID, scope: ConsentScope, accepted: bool) -> None:
    session.add(
        ConsentLog(
            user_id=user_id,
            scope=scope,
            accepted=accepted,
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return await session.scalar(stmt) or 0


# --- the catalogue ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_visitor_sees_open_listings_and_nothing_personal(client, session):
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")
    open_one = await _listing(session)
    await _listing(session, days=-1, title="Muddati oʻtgan")
    await _listing(session, active=False, title="Yopilgan")

    body = (await client.get(DISCOVER)).json()

    assert [item["id"] for item in body["items"]] == [str(open_one.id)]
    item = body["items"][0]
    assert item["is_open"] is True
    assert item["fit"] is None
    assert item["eligibility"] is None
    assert item["application_status"] is None
    assert body["signed_in"] is False
    assert item["skills"][0]["slug"] == "accounting"


@pytest.mark.asyncio
async def test_closed_listings_appear_only_when_asked_for_and_say_they_are_closed(client, session):
    await _listing(session)
    closed = await _listing(session, days=-2, title="Eski eʼlon")
    await _listing(session, active=False, title="Olib tashlangan")

    body = (await client.get(DISCOVER, params={"include_closed": True})).json()

    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(closed.id)]["is_open"] is False
    # A listing the partner took down never comes back.
    assert body["total"] == 2
    # Open ones first.
    assert body["items"][-1]["id"] == str(closed.id)


@pytest.mark.asyncio
async def test_filters_compose_and_facets_never_offer_an_empty_option(client, session):
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya", "бухгалтерия")
    await _listing(session, kind=OpportunityType.VACANCY, region="samarkand")
    internship = await _listing(session, kind=OpportunityType.INTERNSHIP, region="samarkand")
    await _listing(session, kind=OpportunityType.INTERNSHIP, region="bukhara", skills=["sewing"])

    body = (
        await client.get(DISCOVER, params={"type": "internship", "skill": "бухгалтерия"})
    ).json()

    assert [item["id"] for item in body["items"]] == [str(internship.id)]
    facets = body["facets"]
    # Each facet is counted with the *other* filters applied.
    assert facets["types"] == {"vacancy": 1, "internship": 1}
    assert facets["regions"] == {"samarkand": 1}
    assert {entry["skill"]["slug"] for entry in facets["skills"]} >= {"accounting"}


@pytest.mark.asyncio
async def test_search_reads_every_language_and_the_organisation(client, session):
    target = await _listing(session, title="Kassir", organisation="Hilol Market")
    await _listing(session, title="Tikuvchi", organisation="Andijon Teks")

    for text in ("kassir (ru)", "HILOL"):
        body = (await client.get(DISCOVER, params={"search": text})).json()
        assert [item["id"] for item in body["items"]] == [str(target.id)], text


@pytest.mark.asyncio
async def test_nothing_matching_is_an_empty_answer_not_an_invented_one(client, session):
    await _listing(session)

    body = (await client.get(DISCOVER, params={"search": "astronavt"})).json()

    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_the_public_list_and_the_front_page_counts_leave_out_closed_listings(client, session):
    await _listing(session)
    await _listing(session, days=-1)

    listed = (await client.get(BASE)).json()
    stats = (await client.get(f"{BASE}/stats")).json()

    assert listed["total"] == 1
    assert stats["total"] == 1


# --- her fit ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_her_fit_is_explained_by_records_she_owns(client, session, user_id):
    await _woman(session, user_id, skills=["Excel"])
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")
    await _skill(session, "excel", "Excel")
    await _skill(session, "1c", "1C")
    course = await _course(session, taught=["buxgalteriya"])
    teaches_1c = await _course(session, taught=["1c"], title="1C bilan ishlash")
    await _finish(session, user_id, course)
    listing = await _listing(session, skills=["buxgalteriya", "excel", "1c"])

    detail = (await client.get(f"{BASE}/{listing.id}", headers=_headers(user_id))).json()

    fit = detail["fit"]
    assert (fit["have"], fit["total"]) == (2, 3)
    skills = {r["skill"]["slug"]: r for r in fit["reasons"] if r["kind"] == "skill"}
    # Learned from the course she finished — named by its title.
    assert skills["accounting"]["status"] == "learned"
    assert skills["accounting"]["source_i18n"]["uz"] == "Buxgalteriya asoslari"
    # Her own word stays her own word.
    assert skills["excel"]["status"] == "self_reported"
    # Her region matches.
    assert any(r["kind"] == "region" for r in fit["reasons"])
    # The missing skill comes with the real course that teaches it.
    [gap] = fit["missing"]
    assert gap["skill"]["slug"] == "1c"
    assert [p["id"] for p in gap["programs"]] == [str(teaches_1c.id)]
    assert detail["eligibility"]["status"] == "eligible"


@pytest.mark.asyncio
async def test_a_listing_on_her_career_direction_says_so(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")
    career = CareerPath(
        slug="buxgalter",
        title_i18n={"uz": "Buxgalter"},
        category=CareerCategory.EMPLOYMENT,
        skill_slugs=["accounting"],
        opportunity_types=["vacancy"],
        is_published=True,
    )
    session.add(career)
    await session.flush()
    await client.put(
        "/api/v1/career-paths/me", json={"slug": "buxgalter"}, headers=_headers(user_id)
    )
    listing = await _listing(session, skills=["buxgalteriya"])
    grant = await _listing(session, skills=["buxgalteriya"], kind=OpportunityType.GRANT)

    body = (await client.get(DISCOVER, headers=_headers(user_id))).json()

    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(listing.id)]["fit"]["on_career"] is True
    # A grant is not where an employment direction leads, whatever it asks for.
    assert by_id[str(grant.id)]["fit"]["on_career"] is False


@pytest.mark.asyncio
async def test_her_best_fit_comes_first(client, session, user_id):
    await _woman(session, user_id, skills=["Excel", "Buxgalteriya"])
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")
    await _skill(session, "excel", "Excel")
    weaker = await _listing(session, skills=["excel", "sewing"], days=2)
    stronger = await _listing(session, skills=["buxgalteriya", "excel"], days=30)

    body = (await client.get(DISCOVER, headers=_headers(user_id))).json()

    ids = [item["id"] for item in body["items"]]
    assert ids.index(str(stronger.id)) < ids.index(str(weaker.id))
    by_deadline = (
        await client.get(DISCOVER, params={"sort": "deadline"}, headers=_headers(user_id))
    ).json()
    assert by_deadline["items"][0]["id"] == str(weaker.id)


# --- eligibility, on the server -------------------------------------------


@pytest.mark.asyncio
async def test_a_girl_under_18_can_read_a_listing_but_cannot_apply(client, session, user_id):
    today = date.today()
    await _woman(session, user_id, born=today.replace(year=today.year - 15))
    listing = await _listing(session)
    headers = _headers(user_id)

    detail = (await client.get(f"{BASE}/{listing.id}", headers=headers)).json()
    assert detail["eligibility"] == {
        "status": "not_eligible",
        "reason": "adults_only",
        "age_min": None,
        "age_max": None,
        "may_apply": False,
    }

    response = await client.post(
        f"{BASE}/{listing.id}/apply", json={"consent": True}, headers=headers
    )
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "adults_only"
    assert await _count(session, Application, user_id=user_id) == 0
    assert await _count(session, ConsentLog, user_id=user_id) == 0
    # And nothing personal recommends it to her.
    assert (await client.get(f"{BASE}/recommended", headers=headers)).json() == []


@pytest.mark.asyncio
async def test_a_stated_age_rule_is_enforced_and_an_unknown_age_is_not_rounded_up(
    client, session, user_id
):
    today = date.today()
    youth = await _listing(
        session, rules={"age_min": 16, "age_max": 24}, source=OpportunitySource.INTERNAL
    )

    await _woman(session, user_id, born=today.replace(year=today.year - 17))
    assert (
        await client.post(f"{BASE}/{youth.id}/apply", json={}, headers=_headers(user_id))
    ).status_code == 201

    unknown = uuid.uuid4()
    await _woman(session, unknown, born=None)
    detail = (await client.get(f"{BASE}/{youth.id}", headers=_headers(unknown))).json()
    assert detail["eligibility"]["status"] == "unknown"
    assert detail["eligibility"]["reason"] == "age_unknown"
    response = await client.post(f"{BASE}/{youth.id}/apply", json={}, headers=_headers(unknown))
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "age_unknown"


@pytest.mark.asyncio
async def test_a_closed_listing_refuses_applications_on_both_routes(client, session, user_id):
    await _woman(session, user_id)
    closed = await _listing(session, days=-1, source=OpportunitySource.INTERNAL)
    headers = _headers(user_id)

    new = await client.post(f"{BASE}/{closed.id}/apply", json={}, headers=headers)
    old = await client.post(
        f"{BASE}/apply", json={"opportunity_id": str(closed.id)}, headers=headers
    )

    assert new.status_code == old.status_code == 409
    assert new.json()["detail"]["reason"] == "closed"
    assert await _count(session, Application, user_id=user_id) == 0


# --- applying and consent ---------------------------------------------------


@pytest.mark.asyncio
async def test_without_consent_nothing_is_created_and_nothing_leaves(client, session, user_id):
    await _woman(session, user_id)
    listing = await _listing(session)

    response = await client.post(
        f"{BASE}/{listing.id}/apply", json={"consent": False}, headers=_headers(user_id)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {"reason": "consent_required", "scope": "share_edu_job"}
    assert await _count(session, Application, user_id=user_id) == 0
    assert await _count(session, IntegrationEvent, user_id=user_id) == 0
    assert await _count(session, ConsentLog, user_id=user_id) == 0


@pytest.mark.asyncio
async def test_confirming_records_consent_first_and_sends_only_the_minimal_profile(
    client, session, user_id
):
    await _woman(session, user_id)
    listing = await _listing(session)
    headers = _headers(user_id)

    before = (await client.get(f"{BASE}/{listing.id}", headers=headers)).json()["sharing"]
    assert before == {
        "partner": "edu_job",
        "consent_scope": "share_edu_job",
        "consent_given": False,
        "fields": list(SHARED_FIELDS),
        "organization": None,
    }

    response = await client.post(
        f"{BASE}/{listing.id}/apply", json={"consent": True}, headers=headers
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "submitted"
    [consent] = (
        await session.execute(select(ConsentLog).where(ConsentLog.user_id == user_id))
    ).scalars()
    assert (consent.scope, consent.accepted) == (ConsentScope.SHARE_EDU_JOB, True)
    audit = await session.scalar(
        select(AuditLog).where(AuditLog.actor_id == user_id, AuditLog.action == "consent.grant")
    )
    assert audit is not None and audit.changes["via"] == "application"

    [event] = (
        await session.execute(select(IntegrationEvent).where(IntegrationEvent.user_id == user_id))
    ).scalars()
    applicant = event.payload["applicant"]
    assert set(applicant) == set(SHARED_FIELDS)
    flat = str(event.payload)
    for private in ("Malika", "Yusupova", "@example.uz", "+9989"):
        assert private not in flat
    assert event.consent_scope == "share_edu_job"


@pytest.mark.asyncio
async def test_consent_already_on_file_is_not_written_again(client, session, user_id):
    await _woman(session, user_id)
    await _consent(session, user_id, ConsentScope.SHARE_EDU_JOB, True)
    listing = await _listing(session)

    response = await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=_headers(user_id))

    assert response.status_code == 201
    assert await _count(session, ConsentLog, user_id=user_id) == 1


@pytest.mark.asyncio
async def test_a_withdrawn_consent_is_respected_and_the_log_only_grows(client, session, user_id):
    await _woman(session, user_id)
    await _consent(session, user_id, ConsentScope.SHARE_EDU_JOB, True)
    await _consent(session, user_id, ConsentScope.SHARE_EDU_JOB, False)
    listing = await _listing(session)
    headers = _headers(user_id)

    refused = await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=headers)
    assert refused.status_code == 403
    assert await _count(session, ConsentLog, user_id=user_id) == 2

    given = await client.post(f"{BASE}/{listing.id}/apply", json={"consent": True}, headers=headers)
    assert given.status_code == 201
    rows = (
        await session.execute(
            select(ConsentLog.accepted)
            .where(ConsentLog.user_id == user_id)
            .order_by(ConsentLog.created_at)
        )
    ).scalars()
    assert list(rows) == [True, False, True]


@pytest.mark.asyncio
async def test_a_womanup_listing_needs_no_partner_consent_and_sends_nothing(
    client, session, user_id
):
    await _woman(session, user_id)
    listing = await _listing(session, source=OpportunitySource.INTERNAL)

    detail = (await client.get(f"{BASE}/{listing.id}", headers=_headers(user_id))).json()
    assert detail["sharing"]["partner"] is None
    assert detail["sharing"]["fields"] == []

    response = await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=_headers(user_id))
    assert response.status_code == 201
    assert await _count(session, IntegrationEvent, user_id=user_id) == 0


@pytest.mark.asyncio
async def test_applying_twice_is_refused(client, session, user_id):
    await _woman(session, user_id)
    listing = await _listing(session, source=OpportunitySource.INTERNAL)
    headers = _headers(user_id)

    await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=headers)
    again = await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=headers)

    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "already_applied"
    assert await _count(session, Application, user_id=user_id) == 1


@pytest.mark.asyncio
async def test_the_older_apply_route_still_needs_consent_on_file(client, session, user_id):
    await _woman(session, user_id)
    listing = await _listing(session)
    headers = _headers(user_id)
    payload = {"opportunity_id": str(listing.id)}

    assert (await client.post(f"{BASE}/apply", json=payload, headers=headers)).status_code == 403
    await _consent(session, user_id, ConsentScope.SHARE_EDU_JOB, True)
    assert (await client.post(f"{BASE}/apply", json=payload, headers=headers)).status_code == 201


# --- tracking ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tracker_names_the_listing_and_never_a_partners_note(client, session, user_id):
    await _woman(session, user_id)
    listing = await _listing(session, source=OpportunitySource.INTERNAL)
    headers = _headers(user_id)
    created = (await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=headers)).json()

    application = await session.get(Application, uuid.UUID(created["id"]))
    application.status = ApplicationStatus.IN_REVIEW
    application.status_history = [
        *application.status_history,
        {
            "status": "in_review",
            "at": datetime.now(UTC).isoformat(),
            "note": "ichki izoh: 3-nomzod",
        },
    ]
    await session.flush()

    [item] = (await client.get(MINE, headers=headers)).json()

    assert item["opportunity"]["title_i18n"]["uz"] == "Buxgalter (kichik biznes)"
    assert [entry["status"] for entry in item["status_history"]] == ["submitted", "in_review"]
    assert "ichki izoh" not in str(item)
    assert item["can_withdraw"] is True
    assert item["status"] in {status.value for status in ApplicationStatus}


@pytest.mark.asyncio
async def test_she_withdraws_her_own_application_and_nobody_elses(client, session, user_id):
    other = uuid.uuid4()
    await _woman(session, user_id)
    await _woman(session, other)
    listing = await _listing(session, source=OpportunitySource.INTERNAL)
    created = (
        await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=_headers(user_id))
    ).json()
    url = f"{BASE}/applications/{created['id']}/withdraw"

    assert (await client.post(url, headers=_headers(other))).status_code == 404
    assert (await client.get(MINE, headers=_headers(other))).json() == []

    first = await client.post(url, headers=_headers(user_id))
    again = await client.post(url, headers=_headers(user_id))
    assert first.json()["status"] == again.json()["status"] == "withdrawn"
    assert first.json()["can_withdraw"] is False
    history = [e["status"] for e in again.json()["status_history"]]
    assert history == ["submitted", "withdrawn"]


@pytest.mark.asyncio
async def test_a_decided_application_cannot_be_withdrawn(client, session, user_id):
    await _woman(session, user_id)
    listing = await _listing(session, source=OpportunitySource.INTERNAL)
    headers = _headers(user_id)
    created = (await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=headers)).json()
    application = await session.get(Application, uuid.UUID(created["id"]))
    application.status = ApplicationStatus.ACCEPTED
    await session.flush()

    response = await client.post(f"{BASE}/applications/{created['id']}/withdraw", headers=headers)

    assert response.status_code == 409


# --- saving -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_saving_is_private_idempotent_and_tells_nobody(client, session, user_id):
    other = uuid.uuid4()
    await _woman(session, user_id)
    await _woman(session, other)
    listing = await _listing(session)
    headers = _headers(user_id)

    for _ in range(2):
        assert (await client.put(f"{BASE}/{listing.id}/save", headers=headers)).status_code == 204
    assert await _count(session, SavedOpportunity, user_id=user_id) == 1
    assert [item["id"] for item in (await client.get(SAVED, headers=headers)).json()] == [
        str(listing.id)
    ]
    assert (await client.get(SAVED, headers=_headers(other))).json() == []
    body = (await client.get(DISCOVER, headers=headers)).json()
    assert body["items"][0]["saved"] is True
    assert body["saved"] == 1
    # Saving is not applying: no application, no consent, no partner event.
    for model in (Application, ConsentLog, IntegrationEvent):
        assert await _count(session, model, user_id=user_id) == 0

    assert (await client.delete(f"{BASE}/{listing.id}/save", headers=headers)).status_code == 204
    assert (await client.get(SAVED, headers=headers)).json() == []


@pytest.mark.asyncio
async def test_personal_routes_need_an_account_and_none_takes_a_user_id(client, session):
    listing = await _listing(session)

    for method, url in (
        ("get", MINE),
        ("get", SAVED),
        ("post", f"{BASE}/{listing.id}/apply"),
        ("put", f"{BASE}/{listing.id}/save"),
    ):
        response = await getattr(client, method)(url, **({"json": {}} if method == "post" else {}))
        assert response.status_code == 401, url

    paths = (await client.get("/api/v1/openapi.json")).json()["paths"]
    assert not any("user" in path for path in paths if path.startswith(BASE))


@pytest.mark.asyncio
async def test_an_unknown_or_withdrawn_listing_is_404(client, session):
    gone = await _listing(session, active=False)

    assert (await client.get(f"{BASE}/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"{BASE}/{gone.id}")).status_code == 404


# --- the Coach --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_explains_a_listing_from_its_real_record(client, session, user_id):
    await _woman(session, user_id, skills=["Excel"])
    await _skill(session, "excel", "Excel")
    await _skill(session, "1c", "1C")
    teaches = await _course(session, taught=["1c"], title="1C bilan ishlash")
    listing = await _listing(session, skills=["excel", "1c"], source=OpportunitySource.INTERNAL)
    await client.post(f"{BASE}/{listing.id}/apply", json={}, headers=_headers(user_id))

    context = await ai_coach.build(session, user_id, language="uz", opportunity_id=listing.id)

    prompt = context.as_prompt()
    assert "the listing she is asking about" in prompt
    assert "she holds: Excel (self_reported)" in prompt
    assert "she is missing: 1C; WomanUP courses that teach it: 1C bilan ishlash" in prompt
    assert "she has already applied; status: submitted" in prompt
    assert "applications she has sent" in prompt
    assert str(listing.id) in context.offer
    assert str(teaches.id) in context.offer


@pytest.mark.asyncio
async def test_the_coach_is_given_nothing_for_a_listing_that_does_not_exist(
    client, session, user_id
):
    await _woman(session, user_id)

    context = await ai_coach.build(session, user_id, language="en", opportunity_id=uuid.uuid4())

    assert "the listing she is asking about" not in context.as_prompt()
    assert "applications she has sent: none" in context.as_prompt()
