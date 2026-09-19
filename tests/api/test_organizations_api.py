"""Organisations: onboarding, the workspace, and what an employer may see.

What is pinned here, in order of how badly it would hurt to get wrong:

* An organisation sees a woman only as far as she let it: applications to its
  own listings and to no others, her profile only while her application stands
  and her consent to *that* organisation is current, and exactly the fields the
  apply page listed. A candidate who has not accepted an invitation is a
  pseudonym that differs per organisation — never an id, a name or a contact.
* Discovery is opt-in, adults only, active accounts only.
* Only the existing roles act for an organisation, checked against the
  database, not the token; every other organisation's records are a 404.
* A partner verifies practical work only for a verified organisation.
* "For your business" shows real business listings, each with a real reason.
"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.constants import (
    ConsentScope,
    DimensionBand,
    EvaluatorKind,
    Language,
    OpportunitySource,
    OpportunityType,
    ProficiencyLevel,
    Region,
    Role,
    SkillCategory,
    SkillStatus,
    TaskStatus,
    TaskSubmissionKind,
)
from app.core.security import create_token
from app.models.assessment import DevelopmentScore
from app.models.audit import AuditLog
from app.models.consent import ConsentLog
from app.models.integration import IntegrationEvent
from app.models.opportunity import Application, Opportunity
from app.models.organization import Organization
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.skill import Skill, UserSkill
from app.models.user import User, UserRole
from app.services import ai_coach, employer
from app.services.score_insights import band_for

API = "/api/v1"
ADMIN = f"{API}/admin/organizations"
ORGS = f"{API}/organizations"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _token(user_id: uuid.UUID, *roles: Role) -> dict[str, str]:
    roles = roles or (Role.USER,)
    token = create_token(user_id, roles[0], "access", {"roles": [r.value for r in roles]})
    return {"Authorization": f"Bearer {token}"}


async def _person(
    session,
    *,
    email: str | None = None,
    name: str = "Malika Yusupova",
    born: date | None = date(1991, 4, 2),
    region: Region | None = Region.SAMARKAND,
    status: str | None = None,
    interests=(),
    roles=(),
) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            phone=_phone(),
            email=email or f"{uuid.uuid4().hex[:10]}@example.uz",
            language=Language.UZ,
            region=region,
        )
    )
    await session.flush()
    session.add(
        Profile(
            user_id=user_id,
            full_name=name,
            birth_date=born,
            profession="Hisobchi",
            years_of_experience=3,
            education_level="oliy",
            employment_status=status,
            interests=list(interests),
            marital_status="turmushga chiqqan",
            children_count=2,
        )
    )
    for role in roles:
        session.add(UserRole(user_id=user_id, role=role))
    await session.flush()
    return user_id


async def _admin(session) -> dict[str, str]:
    admin_id = await _person(session, roles=(Role.ADMIN,))
    return _token(admin_id, Role.ADMIN)


async def _org(client, session, *, kind="employer", verified=False, name="Alfa Savdo MChJ"):
    """Onboard an organisation with an owner, the way an administrator does."""
    admin = await _admin(session)
    created = (await client.post(ADMIN, json={"name": name, "kind": kind}, headers=admin)).json()
    email = f"owner-{uuid.uuid4().hex[:6]}@alfa.uz"
    owner = await _person(session, email=email, name="Owner Person")
    added = await client.post(
        f"{ADMIN}/{created['id']}/members", json={"email": email, "role": "owner"}, headers=admin
    )
    assert added.status_code == 200, added.text
    if verified:
        await client.patch(f"{ADMIN}/{created['id']}", json={"is_verified": True}, headers=admin)
    role = Role.TRAINER if kind == "education_provider" else Role.PARTNER
    return uuid.UUID(created["id"]), owner, _token(owner, role), admin


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


async def _listing(client, org_id, headers, **overrides) -> dict:
    payload = {
        "type": "vacancy",
        "title_i18n": {"uz": "Buxgalter yordamchisi"},
        "skills": ["accounting"],
        "reward": {"salary_from": 5000000},
        "deadline": (datetime.now(UTC) + timedelta(days=20)).isoformat(),
        **overrides,
    }
    response = await client.post(f"{ORGS}/{org_id}/listings", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _hold(session, user_id: uuid.UUID, skill: Skill, status: SkillStatus) -> None:
    session.add(UserSkill(user_id=user_id, skill_id=skill.id, status=status))
    await session.flush()


async def _opt_in(client, user_id: uuid.UUID, accepted: bool = True) -> None:
    response = await client.post(
        f"{API}/users/me/consents",
        json={"scope": "candidate_discovery", "accepted": accepted},
        headers=_token(user_id),
    )
    assert response.status_code == 200, response.text


async def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for column, value in where.items():
        stmt = stmt.where(getattr(model, column) == value)
    return await session.scalar(stmt) or 0


PRIVATE = ("Yusupova", "turmushga", "+9989", "@example.uz", "1991")


# --- onboarding and roles ---------------------------------------------------


@pytest.mark.asyncio
async def test_only_an_administrator_onboards_and_every_act_is_audited(client, session):
    stranger = await _person(session, roles=(Role.PARTNER,))
    refused = await client.post(
        ADMIN, json={"name": "Boshqa", "kind": "employer"}, headers=_token(stranger, Role.PARTNER)
    )
    assert refused.status_code == 403

    org_id, owner, _, _ = await _org(client, session)

    org = await session.get(Organization, org_id)
    assert (org.is_public, org.is_verified, org.is_active) == (False, False, True)
    actions = set(
        (
            await session.scalars(select(AuditLog.action).where(AuditLog.entity_id == str(org_id)))
        ).all()
    )
    assert {"organization.create", "organization.member_add"} <= actions


@pytest.mark.asyncio
async def test_membership_reuses_the_existing_roles_and_adds_none(client, session):
    _, owner, _, _ = await _org(client, session, kind="employer")
    _, teacher, _, _ = await _org(client, session, kind="education_provider", name="Bilim Markazi")

    roles = {
        user_id: set(
            (await session.scalars(select(UserRole.role).where(UserRole.user_id == user_id))).all()
        )
        for user_id in (owner, teacher)
    }
    assert roles[owner] == {Role.PARTNER}
    assert roles[teacher] == {Role.TRAINER}
    assert not {"employer", "education_provider"} & {role.value for role in Role}


@pytest.mark.asyncio
async def test_adding_an_unknown_email_is_refused(client, session):
    org_id, _, _, admin = await _org(client, session)
    response = await client.post(
        f"{ADMIN}/{org_id}/members",
        json={"email": "yoq@example.uz", "role": "member"},
        headers=admin,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_non_member_sees_nothing_of_an_organisation(client, session):
    org_id, _, owner_headers, _ = await _org(client, session)
    other_id, _, other_headers, _ = await _org(client, session, name="Beta Savdo")
    listing = await _listing(client, org_id, owner_headers, skills=[])

    for path in ("", "/listings", "/applications", "/candidates", "/invitations", "/members"):
        assert (
            await client.get(f"{ORGS}/{org_id}{path}", headers=other_headers)
        ).status_code == 404
    edit = await client.patch(
        f"{ORGS}/{other_id}/listings/{listing['id']}",
        json={"title_i18n": {"uz": "Oʻgʻirlangan"}},
        headers=other_headers,
    )
    assert edit.status_code == 404
    woman = await _person(session)
    assert (await client.get(f"{ORGS}/{org_id}", headers=_token(woman))).status_code == 404


@pytest.mark.asyncio
async def test_a_revoked_role_stops_a_member_even_while_her_token_still_carries_it(client, session):
    org_id, owner, headers, _ = await _org(client, session)
    role = await session.scalar(
        select(UserRole).where(UserRole.user_id == owner, UserRole.role == Role.PARTNER)
    )
    await session.delete(role)
    await session.flush()

    assert (await client.get(f"{ORGS}/{org_id}", headers=headers)).status_code == 404
    assert (await client.get(f"{ORGS}/mine", headers=headers)).json() == []


@pytest.mark.asyncio
async def test_only_the_owner_edits_the_profile_and_the_public_page_shows_only_public_fields(
    client, session
):
    org_id, _, owner_headers, admin = await _org(client, session)
    member_email = f"m-{uuid.uuid4().hex[:6]}@alfa.uz"
    member = await _person(session, email=member_email)
    await client.post(
        f"{ADMIN}/{org_id}/members", json={"email": member_email, "role": "member"}, headers=admin
    )
    slug = (await session.get(Organization, org_id)).slug

    by_member = await client.patch(
        f"{ORGS}/{org_id}", json={"industry": "Savdo"}, headers=_token(member, Role.PARTNER)
    )
    assert by_member.status_code == 403
    assert (await client.get(f"{ORGS}/public/{slug}")).status_code == 404

    response = await client.patch(
        f"{ORGS}/{org_id}",
        json={
            "description_i18n": {"uz": "Kichik savdo kompaniyasi."},
            "industry": "Savdo",
            "website": "https://alfa.uz",
            "logo_url": "http://alfa.uz/logo.png",
            "is_public": True,
        },
        headers=owner_headers,
    )
    assert response.status_code == 422  # the logo must be https
    ok = await client.patch(
        f"{ORGS}/{org_id}",
        json={"industry": "Savdo", "website": "https://alfa.uz", "is_public": True},
        headers=owner_headers,
    )
    assert ok.status_code == 200

    public = (await client.get(f"{ORGS}/public/{slug}")).json()
    assert public["industry"] == "Savdo"
    assert set(public) == {
        "slug",
        "name",
        "kind",
        "is_verified",
        "description_i18n",
        "industry",
        "region",
        "city",
        "website",
        "logo_url",
        "listings",
        "programmes",
    }
    assert member_email not in json.dumps(public)


@pytest.mark.asyncio
async def test_a_suspended_organisation_disappears_everywhere(client, session):
    org_id, _, headers, admin = await _org(client, session)
    await _skill(session, "accounting", "Buxgalteriya")
    listing = await _listing(client, org_id, headers)
    await client.patch(f"{ORGS}/{org_id}", json={"is_public": True}, headers=headers)
    slug = (await session.get(Organization, org_id)).slug

    await client.patch(f"{ADMIN}/{org_id}", json={"is_active": False}, headers=admin)

    assert (await client.get(f"{ORGS}/{org_id}", headers=headers)).status_code == 404
    assert (await client.get(f"{ORGS}/public/{slug}")).status_code == 404
    assert (await client.get(f"{API}/opportunities/{listing['id']}")).status_code == 404
    ids = [
        item["id"] for item in (await client.get(f"{API}/opportunities/discover")).json()["items"]
    ]
    assert listing["id"] not in ids


# --- listings ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_published_listing_joins_the_catalogue_with_canonical_skills(client, session):
    org_id, _, headers, _ = await _org(client, session)
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")

    refused = await client.post(
        f"{ORGS}/{org_id}/listings",
        json={"type": "vacancy", "title_i18n": {"uz": "X lavozim"}, "skills": ["uchish"]},
        headers=headers,
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == {"reason": "unknown_skill", "fields": ["uchish"]}
    past = await client.post(
        f"{ORGS}/{org_id}/listings",
        json={
            "type": "vacancy",
            "title_i18n": {"uz": "Kech"},
            "deadline": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert past.status_code == 422

    listing = await _listing(client, org_id, headers, skills=["Buxgalteriya"])
    assert [skill["slug"] for skill in listing["skills"]] == ["accounting"]
    row = await session.get(Opportunity, uuid.UUID(listing["id"]))
    assert row.source == OpportunitySource.ORGANIZATION
    card = next(
        item
        for item in (await client.get(f"{API}/opportunities/discover")).json()["items"]
        if item["id"] == listing["id"]
    )
    assert card["organization"]["name"] == "Alfa Savdo MChJ"
    # Private until published: no link to its page yet.
    assert card["organization"]["slug"] is None


@pytest.mark.asyncio
async def test_a_closed_listing_stays_readable_and_takes_no_applications(client, session):
    org_id, _, headers, _ = await _org(client, session)
    listing = await _listing(client, org_id, headers, skills=[])
    await client.post(f"{ORGS}/{org_id}/listings/{listing['id']}/close", headers=headers)

    detail = (await client.get(f"{API}/opportunities/{listing['id']}")).json()
    assert detail["is_open"] is False
    woman = await _person(session)
    response = await client.post(
        f"{API}/opportunities/{listing['id']}/apply", json={"consent": True}, headers=_token(woman)
    )
    assert response.status_code == 409


# --- applications and what the organisation sees ----------------------------


async def _applied(client, session, org_id, headers, **person) -> tuple[uuid.UUID, dict, dict]:
    listing = await _listing(client, org_id, headers, skills=[])
    woman = await _person(session, **person)
    response = await client.post(
        f"{API}/opportunities/{listing['id']}/apply", json={"consent": True}, headers=_token(woman)
    )
    assert response.status_code == 201, response.text
    return woman, listing, response.json()


@pytest.mark.asyncio
async def test_applying_needs_consent_to_this_organisation_and_sends_nothing_outside(
    client, session
):
    org_id, _, headers, _ = await _org(client, session)
    listing = await _listing(client, org_id, headers, skills=[])
    woman = await _person(session)

    detail = (
        await client.get(f"{API}/opportunities/{listing['id']}", headers=_token(woman))
    ).json()
    assert detail["sharing"]["consent_scope"] == "share_with_employer"
    assert detail["sharing"]["fields"] == list(employer.EMPLOYER_FIELDS)
    assert detail["sharing"]["organization"]["name"] == "Alfa Savdo MChJ"

    refused = await client.post(
        f"{API}/opportunities/{listing['id']}/apply", json={"consent": False}, headers=_token(woman)
    )
    assert refused.status_code == 403
    assert await _count(session, Application, user_id=woman) == 0

    ok = await client.post(
        f"{API}/opportunities/{listing['id']}/apply", json={"consent": True}, headers=_token(woman)
    )
    assert ok.status_code == 201
    [consent] = (await session.scalars(select(ConsentLog).where(ConsentLog.user_id == woman))).all()
    assert (consent.scope, consent.accepted, consent.subject_ref) == (
        ConsentScope.SHARE_WITH_EMPLOYER,
        True,
        str(org_id),
    )
    assert await _count(session, IntegrationEvent, user_id=woman) == 0


@pytest.mark.asyncio
async def test_an_organisation_sees_exactly_the_listed_fields_and_nothing_private(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman, _, applied = await _applied(client, session, org_id, headers)

    [row] = (await client.get(f"{ORGS}/{org_id}/applications", headers=headers)).json()

    assert row["id"] == applied["id"]
    assert set(row["profile"]) == set(employer.EMPLOYER_FIELDS)
    assert row["profile"]["first_name"] == "Malika"
    text = json.dumps(row)
    for private in PRIVATE:
        assert private not in text, private
    assert str(woman) not in text


@pytest.mark.asyncio
async def test_consent_to_one_organisation_is_not_consent_to_another(client, session):
    org_a, _, headers_a, _ = await _org(client, session)
    org_b, _, headers_b, _ = await _org(client, session, name="Beta Savdo")
    woman, _, applied = await _applied(client, session, org_a, headers_a)

    assert await employer.shares_with(session, woman, await session.get(Organization, org_a))
    assert not await employer.shares_with(session, woman, await session.get(Organization, org_b))
    assert (
        await client.get(f"{ORGS}/{org_b}/applications/{applied['id']}", headers=headers_b)
    ).status_code == 404
    # A platform-wide reading never counts a scoped yes.
    consents = (await client.get(f"{API}/users/me/consents", headers=_token(woman))).json()
    assert consents["share_with_employer"] is False


@pytest.mark.asyncio
async def test_she_can_stop_sharing_and_the_profile_disappears_from_its_view(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman, _, applied = await _applied(client, session, org_id, headers)

    assert (
        await client.delete(f"{ORGS}/me/sharing/{org_id}", headers=_token(woman))
    ).status_code == 204
    row = (
        await client.get(f"{ORGS}/{org_id}/applications/{applied['id']}", headers=headers)
    ).json()
    assert row["profile"] is None
    assert row["hidden"] == "consent_withdrawn"
    assert (await client.get(f"{ORGS}/me/sharing", headers=_token(woman))).json() == []
    # The log only grew, and the withdrawal is audited with the organisation.
    assert await _count(session, ConsentLog, user_id=woman) == 2
    withdrawn = await session.scalar(
        select(AuditLog).where(AuditLog.action == "consent.withdraw", AuditLog.actor_id == woman)
    )
    assert withdrawn.changes == {"organization_id": str(org_id)}


@pytest.mark.asyncio
async def test_a_withdrawn_application_hides_her_and_cannot_be_moved(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman, _, applied = await _applied(client, session, org_id, headers)
    await client.post(
        f"{API}/opportunities/applications/{applied['id']}/withdraw", headers=_token(woman)
    )

    row = (
        await client.get(f"{ORGS}/{org_id}/applications/{applied['id']}", headers=headers)
    ).json()
    assert (row["profile"], row["hidden"], row["next"]) == (None, "withdrawn", [])
    moved = await client.post(
        f"{ORGS}/{org_id}/applications/{applied['id']}/status",
        json={"status": "accepted"},
        headers=headers,
    )
    assert moved.status_code == 409


@pytest.mark.asyncio
async def test_statuses_move_forward_only_and_the_note_stays_with_the_organisation(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman, _, applied = await _applied(client, session, org_id, headers)
    url = f"{ORGS}/{org_id}/applications/{applied['id']}/status"

    reviewed = await client.post(
        url, json={"status": "in_review", "note": "ichki: kuchli nomzod"}, headers=headers
    )
    assert reviewed.json()["next"] == ["accepted", "rejected"]
    assert (
        await client.post(url, json={"status": "submitted"}, headers=headers)
    ).status_code == 409
    accepted = await client.post(url, json={"status": "accepted"}, headers=headers)
    assert accepted.json()["status"] == "accepted"

    [mine] = (
        await client.get(f"{API}/opportunities/me/applications", headers=_token(woman))
    ).json()
    assert [entry["status"] for entry in mine["status_history"]] == [
        "submitted",
        "in_review",
        "accepted",
    ]
    assert "kuchli nomzod" not in json.dumps(mine)
    audits = await _count(session, AuditLog, action="application.status")
    assert audits == 2


@pytest.mark.asyncio
async def test_the_general_consent_endpoint_refuses_a_per_organisation_scope(client, session):
    woman = await _person(session)
    response = await client.post(
        f"{API}/users/me/consents",
        json={"scope": "share_with_employer", "accepted": True},
        headers=_token(woman),
    )
    assert response.status_code == 422


# --- discovery: opt-in, adults, anonymous ------------------------------------


@pytest.mark.asyncio
async def test_only_opted_in_adults_with_active_accounts_can_be_found(client, session):
    org_id, _, headers, _ = await _org(client, session)
    today = date.today()
    adult = await _person(session)
    minor = await _person(session, born=today.replace(year=today.year - 16))
    unknown = await _person(session, born=None)
    quiet = await _person(session)
    changed_mind = await _person(session)
    for user_id in (adult, minor, unknown, changed_mind):
        await _opt_in(client, user_id)
    await _opt_in(client, changed_mind, accepted=False)

    cards = (await client.get(f"{ORGS}/{org_id}/candidates", headers=headers)).json()

    refs = {card["ref"] for card in cards}
    org = await session.get(Organization, org_id)
    assert refs == {employer.candidate_ref(org, adult)}
    for user_id in (minor, unknown, quiet, changed_mind):
        assert employer.candidate_ref(org, user_id) not in refs


@pytest.mark.asyncio
async def test_a_candidate_card_is_anonymous_and_its_pseudonym_differs_per_organisation(
    client, session
):
    org_a, _, headers_a, _ = await _org(client, session)
    org_b, _, headers_b, _ = await _org(client, session, name="Beta Savdo")
    accounting = await _skill(session, "accounting", "Buxgalteriya")
    excel = await _skill(session, "excel", "Excel")
    woman = await _person(session)
    await _hold(session, woman, accounting, SkillStatus.VERIFIED)
    await _hold(session, woman, excel, SkillStatus.SELF_REPORTED)
    await _opt_in(client, woman)

    [card_a] = (await client.get(f"{ORGS}/{org_a}/candidates", headers=headers_a)).json()
    [card_b] = (await client.get(f"{ORGS}/{org_b}/candidates", headers=headers_b)).json()

    assert card_a["ref"] != card_b["ref"]
    assert str(woman) not in json.dumps(card_a)
    for private in (*PRIVATE, "Malika", "Hisobchi"):
        assert private not in json.dumps(card_a), private
    # Her own word is not shown to a stranger as a skill.
    assert [(s["skill"]["slug"], s["status"]) for s in card_a["skills"]] == [
        ("accounting", "verified")
    ]
    by_skill = (
        await client.get(f"{ORGS}/{org_a}/candidates", params={"skill": "excel"}, headers=headers_a)
    ).json()
    assert by_skill == []


@pytest.mark.asyncio
async def test_an_invitation_is_hers_to_answer_and_accepting_is_her_consent(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman = await _person(session)
    await _opt_in(client, woman)
    [card] = (await client.get(f"{ORGS}/{org_id}/candidates", headers=headers)).json()

    assert (
        await client.post(f"{ORGS}/{org_id}/invitations", json={"ref": "0" * 24}, headers=headers)
    ).status_code == 404
    sent = await client.post(
        f"{ORGS}/{org_id}/invitations",
        json={"ref": card["ref"], "message": "Bizning jamoaga taklif"},
        headers=headers,
    )
    assert sent.status_code == 201
    again = await client.post(
        f"{ORGS}/{org_id}/invitations", json={"ref": card["ref"]}, headers=headers
    )
    assert again.status_code == 409

    [pending] = (await client.get(f"{ORGS}/{org_id}/invitations", headers=headers)).json()
    assert pending["profile"] is None

    [mine] = (await client.get(f"{ORGS}/me/invitations", headers=_token(woman))).json()
    assert mine["organization"]["name"] == "Alfa Savdo MChJ"
    assert mine["fields"] == list(employer.EMPLOYER_FIELDS)
    stranger = await _person(session)
    assert (
        await client.post(
            f"{ORGS}/me/invitations/{mine['id']}", json={"accept": True}, headers=_token(stranger)
        )
    ).status_code == 404

    accepted = await client.post(
        f"{ORGS}/me/invitations/{mine['id']}", json={"accept": True}, headers=_token(woman)
    )
    assert accepted.json()["status"] == "accepted"
    assert (
        await client.post(
            f"{ORGS}/me/invitations/{mine['id']}", json={"accept": False}, headers=_token(woman)
        )
    ).status_code == 409
    [answered] = (await client.get(f"{ORGS}/{org_id}/invitations", headers=headers)).json()
    assert answered["profile"]["first_name"] == "Malika"
    assert await employer.shares_with(session, woman, await session.get(Organization, org_id))
    granted = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "consent.grant",
            AuditLog.actor_id == woman,
            AuditLog.entity_id == "share_with_employer",
        )
    )
    assert granted.changes == {"via": "invitation", "organization_id": str(org_id)}


@pytest.mark.asyncio
async def test_declining_shares_nothing_and_switching_discovery_off_ends_reach(client, session):
    org_id, _, headers, _ = await _org(client, session)
    woman = await _person(session)
    await _opt_in(client, woman)
    [card] = (await client.get(f"{ORGS}/{org_id}/candidates", headers=headers)).json()
    await client.post(f"{ORGS}/{org_id}/invitations", json={"ref": card["ref"]}, headers=headers)
    [mine] = (await client.get(f"{ORGS}/me/invitations", headers=_token(woman))).json()

    await client.post(
        f"{ORGS}/me/invitations/{mine['id']}", json={"accept": False}, headers=_token(woman)
    )
    [declined] = (await client.get(f"{ORGS}/{org_id}/invitations", headers=headers)).json()
    assert declined["profile"] is None
    assert not await employer.shares_with(session, woman, await session.get(Organization, org_id))

    await _opt_in(client, woman, accepted=False)
    again = await client.post(
        f"{ORGS}/{org_id}/invitations", json={"ref": card["ref"]}, headers=headers
    )
    assert again.status_code == 404


# --- verification by a partner ----------------------------------------------


async def _submitted(session, woman: uuid.UUID) -> TaskAttempt:
    await _skill(session, "accounting", "Buxgalteriya", "buxgalteriya")
    task = PracticalTask(
        slug=f"task-{uuid.uuid4().hex[:6]}",
        title_i18n={"uz": "Balans"},
        summary_i18n={},
        instructions_i18n={"uz": "Yozing"},
        outcome_i18n={},
        criteria=[{"key": "a", "text_i18n": {"uz": "A"}}],
        kind=TaskSubmissionKind.TEXT,
        min_chars=10,
        level=ProficiencyLevel.ELEMENTARY,
        skills_practised=["buxgalteriya"],
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    attempt = TaskAttempt(
        task_id=task.id,
        user_id=woman,
        attempt_no=1,
        status=TaskStatus.SUBMITTED,
        submission={"text": "Balans tuzildi, hisob-kitob tayyor."},
        submitted_at=datetime.now(UTC),
    )
    session.add(attempt)
    await session.flush()
    return attempt


VERDICT = {"passed": True, "score": 100, "feedback": "Aniq va toʻliq ish.", "criteria_met": []}


@pytest.mark.asyncio
async def test_a_partner_verifies_only_for_a_verified_organisation(client, session):
    woman = await _person(session)
    attempt = await _submitted(session, woman)
    url = f"{API}/practical-tasks/attempts/{attempt.id}/evaluate"

    loose = await _person(session, roles=(Role.PARTNER,))
    assert (
        await client.post(url, json=VERDICT, headers=_token(loose, Role.PARTNER))
    ).status_code == 403
    assert (
        await client.get(f"{API}/practical-tasks/review", headers=_token(loose, Role.PARTNER))
    ).status_code == 403

    _, _, unverified_headers, _ = await _org(client, session, name="Tekshirilmagan")
    assert (await client.post(url, json=VERDICT, headers=unverified_headers)).status_code == 403

    org_id, _, verified_headers, _ = await _org(client, session, verified=True, name="Ishonchli")
    response = await client.post(url, json=VERDICT, headers=verified_headers)
    assert response.status_code == 200, response.text

    await session.refresh(attempt)
    assert attempt.evaluator_kind == EvaluatorKind.PARTNER
    assert attempt.evaluator_org_id == org_id
    status = await session.scalar(
        select(UserSkill.status)
        .join(Skill, Skill.id == UserSkill.skill_id)
        .where(UserSkill.user_id == woman, Skill.slug == "accounting")
    )
    assert status == SkillStatus.VERIFIED


@pytest.mark.asyncio
async def test_an_education_providers_trainer_still_assesses(client, session):
    woman = await _person(session)
    attempt = await _submitted(session, woman)
    _, _, headers, _ = await _org(client, session, kind="education_provider", name="Bilim")

    response = await client.post(
        f"{API}/practical-tasks/attempts/{attempt.id}/evaluate", json=VERDICT, headers=headers
    )

    assert response.status_code == 200
    await session.refresh(attempt)
    assert attempt.evaluator_kind == EvaluatorKind.TRAINER
    assert attempt.evaluator_org_id is None


# --- for your business -------------------------------------------------------


async def _business_listing(
    session, *, kind=OpportunityType.GRANT, skills=(), days=20, title="Grant"
):
    item = Opportunity(
        source=OpportunitySource.INVEST_HUB,
        external_id=uuid.uuid4().hex,
        type=kind,
        title_i18n={"uz": title},
        description_i18n={},
        organisation="Invest HUB",
        required_skills=list(skills),
        eligibility={},
        reward={"amount": 50000000},
        deadline=datetime.now(UTC) + timedelta(days=days),
        is_active=True,
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.asyncio
async def test_for_your_business_shows_real_business_listings_each_with_a_reason(client, session):
    await _skill(session, "business-plan", "Biznes-reja", "biznes-reja")
    await _skill(session, "ecommerce", "Onlayn savdo", "onlayn savdo")
    grant = await _business_listing(session, skills=["biznes-reja"], title="Grant")
    market = await _business_listing(
        session, kind=OpportunityType.MARKETPLACE, skills=["onlayn savdo"], title="Bozor"
    )
    await _business_listing(session, kind=OpportunityType.GRANT, skills=["sewing"], title="Boshqa")
    vacancy = await _business_listing(session, kind=OpportunityType.VACANCY, skills=["biznes-reja"])
    await _business_listing(session, skills=["biznes-reja"], days=-2, title="Yopilgan")
    woman = await _person(session, status="oʻz ishi bor", interests=["onlayn savdo"])

    body = (await client.get(f"{API}/opportunities/business", headers=_token(woman))).json()

    ids = [item["id"] for item in body["items"]]
    assert str(grant.id) in ids and str(market.id) in ids
    assert str(vacancy.id) not in ids
    assert body["signals"]["own_business"] is True
    by_id = {item["id"]: item for item in body["items"]}
    kinds = {reason["kind"] for reason in by_id[str(market.id)]["fit"]["reasons"]}
    assert {"own_business", "interest"} <= kinds
    for item in body["items"]:
        assert item["type"] in {
            "grant",
            "investment",
            "mentorship",
            "marketplace",
            "competition",
            "consultation",
        }
        assert item["is_open"] is True


@pytest.mark.asyncio
async def test_with_nothing_to_go_on_nothing_is_pushed_at_her(client, session):
    await _business_listing(session, skills=["biznes-reja"])
    woman = await _person(session)

    body = (await client.get(f"{API}/opportunities/business", headers=_token(woman))).json()

    assert body["items"] == []
    assert body["total_open"] == 1


@pytest.mark.asyncio
async def test_a_weak_entrepreneurship_score_is_a_reason_and_a_minor_is_shown_none(client, session):
    await _business_listing(session, kind=OpportunityType.MENTORSHIP)
    woman = await _person(session)
    session.add(
        DevelopmentScore(user_id=woman, dimension="entrepreneurship", baseline=25, current=25)
    )
    await session.flush()
    assert band_for(25) != DimensionBand.STRONG

    body = (await client.get(f"{API}/opportunities/business", headers=_token(woman))).json()
    [item] = body["items"]
    [reason] = item["fit"]["reasons"]
    assert (reason["kind"], reason["dimension"], reason["score"]) == (
        "score",
        "entrepreneurship",
        25,
    )

    today = date.today()
    girl = await _person(session, status="oʻz ishi bor", born=today.replace(year=today.year - 15))
    assert (await client.get(f"{API}/opportunities/business", headers=_token(girl))).json()[
        "items"
    ] == []


@pytest.mark.asyncio
async def test_the_catalogue_can_be_narrowed_to_business_listings(client, session):
    grant = await _business_listing(session)
    await _business_listing(session, kind=OpportunityType.VACANCY)

    body = (await client.get(f"{API}/opportunities/discover", params={"business": True})).json()

    assert [item["id"] for item in body["items"]] == [str(grant.id)]


# --- the Coach ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_quotes_only_what_a_listing_records(client, session):
    woman = await _person(session)
    grant = await _business_listing(session)
    bare = Opportunity(
        source=OpportunitySource.INVEST_HUB,
        external_id=uuid.uuid4().hex,
        type=OpportunityType.MENTORSHIP,
        title_i18n={"uz": "Mentorlik"},
        required_skills=[],
        eligibility={},
        reward={},
        is_active=True,
    )
    session.add(bare)
    await session.flush()

    with_amount = await ai_coach.build(session, woman, language="en", opportunity_id=grant.id)
    without = await ai_coach.build(session, woman, language="en", opportunity_id=bare.id)

    assert "amount 50000000 UZS" in with_amount.as_prompt()
    assert "the listing states nothing — say so, do not estimate" in without.as_prompt()
