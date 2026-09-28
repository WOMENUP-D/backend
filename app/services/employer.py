"""What an organisation does on WomanUP, and exactly what it may see.

**Listings.** A member publishes, edits and closes its organisation's listings.
They join the same catalogue as every partner listing, go through the same
eligibility rule, and are matched by the same engine. Skills are canonical:
a label the taxonomy does not know is refused, never minted.

**Applications.** A member sees the applications to its own listings and to no
others — any other id is a 404. What it sees of the woman who applied is
`candidate_profile`, and only while two things hold: the application stands
(not withdrawn), and her consent to share with *this* organisation is current.
If she withdraws either, the profile disappears from its view.

**Candidate profile — the whole of it.** Her first name, region, language,
education level, employment status, profession, years of experience, her
skills with how the platform knows each (learned, assessed, verified, or her
own word) and her portfolio link if she made it public herself. Never her
surname, phone, e-mail, birth date or age, address, marital status, children,
disability, or register membership. The apply page lists these same fields
before she confirms.

**Discovery is opt-in and anonymous.** Only women who switched candidate
discovery on, whose age is known to be 18 or over and whose account is active
can be found. An organisation sees a pseudonym that is different for every
organisation — never her id, her name or her contact — plus her region, her
experience and her evidenced skills. It can invite; she decides. Accepting is
her consent to share her profile with that organisation, recorded as such.

**Every act is audited**: publishing, editing, closing, changing an
application's status, inviting.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    EVENT_ONLY_TYPES,
    EVENT_TYPES,
    PRIVACY_POLICY_VERSION,
    ApplicationStatus,
    ConsentScope,
    DataClassification,
    EventFormat,
    InvitationStatus,
    OpportunitySource,
    OpportunityType,
    SkillStatus,
    UserStatus,
)
from app.models.consent import ConsentLog
from app.models.opportunity import Application, Opportunity
from app.models.organization import Organization, OrganizationInvitation, OrganizationMember
from app.models.practice import TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate
from app.models.skill import Skill, UserSkill
from app.models.user import User
from app.schemas.auth import CurrentUser
from app.services import eligibility
from app.services import skills as skill_service
from app.services.age_gate import age_from_profile
from app.services.audit_service import has_consent, record_audit, record_consent

ADULT_AGE = 18
MAX_PENDING_INVITES = 50
DISCOVERY_LIMIT = 2000
#: Statuses a member may set, and from which.
TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.SUBMITTED: frozenset(
        {ApplicationStatus.IN_REVIEW, ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED}
    ),
    ApplicationStatus.IN_REVIEW: frozenset(
        {ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED}
    ),
}
#: The fields an organisation receives, in the order the apply page lists them.
EMPLOYER_FIELDS: tuple[str, ...] = (
    "first_name",
    "region",
    "language",
    "education_level",
    "employment_status",
    "profession",
    "years_of_experience",
    "skills",
    "public_portfolio",
)


class EmployerError(Exception):
    def __init__(self, reason: str, status_code: int = 422, detail: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def _titles(values: dict | None, *, required: bool, limit: int = 300) -> dict:
    """Text per language, uz/ru/en only. A title is short; a description may
    run to a few paragraphs."""
    out = {
        language: text.strip()[:limit]
        for language, text in (values or {}).items()
        if language in ("uz", "ru", "en") and isinstance(text, str) and text.strip()
    }
    if required and not out:
        raise EmployerError("title")
    return out


async def _canonical(session: AsyncSession, labels: list[str]) -> list[str]:
    """Skills as canonical slugs. A label the taxonomy does not know is
    refused with its name, so the form can say which one."""
    labels = [label.strip() for label in labels if label and label.strip()][:12]
    index = await skill_service.SkillIndex.load(session, labels)
    unknown = [label for label in labels if index.get(label) is None]
    if unknown:
        raise EmployerError("unknown_skill", detail={"fields": unknown})
    return list(dict.fromkeys(index.get(label).slug for label in labels))


def _reward(payload: dict | None) -> dict:
    """What the listing offers, in the shapes the catalogue already reads."""
    out: dict = {}
    for key in ("salary_from", "salary_to", "amount", "stipend"):
        value = (payload or {}).get(key)
        if isinstance(value, int | float) and value >= 0:
            out[key] = int(value)
    text = (payload or {}).get("text")
    if isinstance(text, str) and text.strip():
        out["text"] = text.strip()[:300]
    return out


def _rules(payload: dict | None) -> dict:
    out: dict = {}
    for key in ("age_min", "age_max"):
        value = (payload or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 100:
            out[key] = value
    if "age_min" in out and "age_max" in out and out["age_min"] > out["age_max"]:
        raise EmployerError("age_range")
    return out


@dataclass(frozen=True, slots=True)
class Timing:
    starts_at: datetime | None
    ends_at: datetime | None
    format: EventFormat | None
    venue: str | None


def _timing(
    kind: OpportunityType,
    deadline: datetime | None,
    data: dict,
    *,
    current_start: datetime | None = None,
) -> Timing:
    """When and where, checked the way she will read it. A workshop, seminar,
    conference, forum or networking evening has a start; a competition, a
    training or a consultation may. Registration closes before it starts."""
    starts, ends = data.get("starts_at"), data.get("ends_at")
    if starts is None:
        if kind in EVENT_ONLY_TYPES:
            raise EmployerError("starts_required")
        if ends is not None:
            raise EmployerError("ends_without_start")
        return Timing(None, None, None, None)
    if kind not in EVENT_TYPES:
        raise EmployerError("not_an_event")
    if starts <= datetime.now(UTC) and starts != current_start:
        raise EmployerError("starts_past")
    if ends is not None and ends < starts:
        raise EmployerError("ends_before_start")
    if deadline is not None and deadline > starts:
        raise EmployerError("deadline_after_start")
    fmt = data.get("format")
    venue = (data.get("venue") or "").strip()[:300] or None
    return Timing(starts, ends, EventFormat(fmt) if fmt else None, venue)


async def publish(
    session: AsyncSession, org: Organization, *, data: dict, actor: CurrentUser
) -> Opportunity:
    deadline = data.get("deadline")
    if deadline is not None and deadline <= datetime.now(UTC):
        raise EmployerError("deadline_past")
    kind = OpportunityType(data["type"])
    timing = _timing(kind, deadline, data)
    item = Opportunity(
        source=OpportunitySource.ORGANIZATION,
        external_id=None,
        type=kind,
        title_i18n=_titles(data.get("title_i18n"), required=True),
        description_i18n=_titles(data.get("description_i18n"), required=False, limit=4000),
        organisation=org.name,
        region=data.get("region"),
        required_skills=await _canonical(session, data.get("skills") or []),
        eligibility=_rules(data.get("eligibility")),
        reward=_reward(data.get("reward")),
        deadline=deadline,
        starts_at=timing.starts_at,
        ends_at=timing.ends_at,
        format=timing.format,
        venue=timing.venue,
        is_active=True,
        organization_id=org.id,
        created_by_id=uuid.UUID(actor.id),
    )
    session.add(item)
    await session.flush()
    await _audit(session, actor, "listing.publish", "opportunity", item.id, {"org": str(org.id)})
    return item


async def own_listing(
    session: AsyncSession, org: Organization, opportunity_id: uuid.UUID
) -> Opportunity:
    item = await session.get(Opportunity, opportunity_id)
    if item is None or item.organization_id != org.id:
        raise EmployerError("not_found", 404)
    return item


async def edit(
    session: AsyncSession, item: Opportunity, *, data: dict, actor: CurrentUser
) -> Opportunity:
    changed: list[str] = []
    if "title_i18n" in data:
        item.title_i18n = _titles(data["title_i18n"], required=True)
        changed.append("title_i18n")
    if "description_i18n" in data:
        item.description_i18n = _titles(data["description_i18n"], required=False, limit=4000)
        changed.append("description_i18n")
    if "skills" in data:
        item.required_skills = await _canonical(session, data["skills"] or [])
        changed.append("skills")
    if "region" in data:
        item.region = data["region"]
        changed.append("region")
    if "reward" in data:
        item.reward = _reward(data["reward"])
        changed.append("reward")
    if "eligibility" in data:
        item.eligibility = _rules(data["eligibility"])
        changed.append("eligibility")
    if "deadline" in data:
        if data["deadline"] is not None and data["deadline"] <= datetime.now(UTC):
            raise EmployerError("deadline_past")
        item.deadline = data["deadline"]
        changed.append("deadline")
    moved = False
    if any(key in data for key in ("starts_at", "ends_at", "format", "venue", "deadline")):
        merged = {
            key: data.get(key, getattr(item, key))
            for key in ("starts_at", "ends_at", "format", "venue")
        }
        timing = _timing(item.type, item.deadline, merged, current_start=item.starts_at)
        moved = timing.starts_at != item.starts_at
        for key in ("starts_at", "ends_at", "format", "venue"):
            if getattr(timing, key) != getattr(item, key):
                setattr(item, key, getattr(timing, key))
                changed.append(key)
    await session.flush()
    if moved:
        # Reminders she set follow the event to its new time.
        from app.services import events

        await events.reschedule(session, item)
    await _audit(session, actor, "listing.edit", "opportunity", item.id, {"fields": changed})
    return item


async def close(session: AsyncSession, item: Opportunity, *, actor: CurrentUser) -> Opportunity:
    """Stop taking applications. The listing stays readable, marked closed, so
    a woman who applied can still open it from her tracker."""
    item.deadline = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    await _audit(session, actor, "listing.close", "opportunity", item.id, {})
    return item


async def listings(session: AsyncSession, org: Organization) -> list[tuple[Opportunity, int]]:
    """Its listings, newest first, with how many applications each received."""
    counts = dict(
        (
            await session.execute(
                select(Application.opportunity_id, func.count())
                .join(Opportunity, Opportunity.id == Application.opportunity_id)
                .where(
                    Opportunity.organization_id == org.id,
                    Application.status != ApplicationStatus.DRAFT,
                )
                .group_by(Application.opportunity_id)
            )
        ).all()
    )
    rows = await session.execute(
        select(Opportunity)
        .where(Opportunity.organization_id == org.id)
        .order_by(Opportunity.created_at.desc())
    )
    return [(item, counts.get(item.id, 0)) for item in rows.scalars()]


# ---------------------------------------------------------------------------
# What an organisation may see about a woman
# ---------------------------------------------------------------------------


def subject(org: Organization | uuid.UUID) -> str:
    """How a consent row names an organisation."""
    return str(org.id if isinstance(org, Organization) else org)


async def shares_with(session: AsyncSession, user_id: uuid.UUID, org: Organization) -> bool:
    return await has_consent(
        session, user_id, ConsentScope.SHARE_WITH_EMPLOYER, subject_ref=subject(org)
    )


async def _skills_of(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        select(Skill, UserSkill.status)
        .join(UserSkill, UserSkill.skill_id == Skill.id)
        .where(UserSkill.user_id == user_id, UserSkill.status.is_not(None))
    )
    rank = {
        SkillStatus.VERIFIED: 0,
        SkillStatus.ASSESSED: 1,
        SkillStatus.LEARNED: 2,
        SkillStatus.SELF_REPORTED: 3,
    }
    out = [
        {"skill": skill_service.ref_for(skill).model_dump(), "status": status.value}
        for skill, status in rows.all()
    ]
    out.sort(key=lambda item: (rank.get(SkillStatus(item["status"]), 9), item["skill"]["slug"]))
    return out


async def candidate_profile(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """Exactly `EMPLOYER_FIELDS`, built field by field. Nothing else about her
    is read here, so nothing else can leak from here."""
    user = await session.get(User, user_id)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    first = (profile.full_name or "").split()[0] if profile and profile.full_name else None
    portfolio = (
        f"/portfolio/{profile.portfolio_slug}"
        if profile and profile.portfolio_public and profile.portfolio_slug
        else None
    )
    if portfolio and not await has_consent(session, user_id, ConsentScope.PUBLIC_PORTFOLIO):
        portfolio = None
    return {
        "first_name": first,
        "region": user.region.value if user and user.region else None,
        "language": user.language.value if user else None,
        "education_level": profile.education_level if profile else None,
        "employment_status": profile.employment_status if profile else None,
        "profession": profile.profession if profile else None,
        "years_of_experience": profile.years_of_experience if profile else None,
        "skills": await _skills_of(session, user_id),
        "public_portfolio": portfolio,
    }


# ---------------------------------------------------------------------------
# Applications to its listings
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrgApplication:
    application: Application
    listing: Opportunity
    profile: dict | None
    #: Why the profile is not shown, when it is not.
    hidden: str | None


async def applications(
    session: AsyncSession, org: Organization, *, opportunity_id: uuid.UUID | None = None
) -> list[OrgApplication]:
    stmt = (
        select(Application, Opportunity)
        .join(Opportunity, Opportunity.id == Application.opportunity_id)
        .where(
            Opportunity.organization_id == org.id,
            Application.status != ApplicationStatus.DRAFT,
        )
        .order_by(Application.submitted_at.desc().nullslast())
    )
    if opportunity_id is not None:
        stmt = stmt.where(Opportunity.id == opportunity_id)
    out: list[OrgApplication] = []
    for application, listing in (await session.execute(stmt)).all():
        out.append(await _read(session, org, application, listing))
    return out


async def _read(
    session: AsyncSession, org: Organization, application: Application, listing: Opportunity
) -> OrgApplication:
    if application.status == ApplicationStatus.WITHDRAWN:
        return OrgApplication(application, listing, None, "withdrawn")
    if not await shares_with(session, application.user_id, org):
        return OrgApplication(application, listing, None, "consent_withdrawn")
    return OrgApplication(
        application, listing, await candidate_profile(session, application.user_id), None
    )


async def own_application(
    session: AsyncSession, org: Organization, application_id: uuid.UUID
) -> OrgApplication:
    row = (
        await session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Opportunity.id == Application.opportunity_id)
            .where(Application.id == application_id, Opportunity.organization_id == org.id)
        )
    ).first()
    if row is None or row[0].status == ApplicationStatus.DRAFT:
        raise EmployerError("not_found", 404)
    return await _read(session, org, row[0], row[1])


async def set_status(
    session: AsyncSession,
    org: Organization,
    application: Application,
    *,
    status: ApplicationStatus,
    note: str | None,
    actor: CurrentUser,
) -> Application:
    """Move an application on. Only forward, only to a status the platform
    already has; a withdrawn application is hers and cannot be touched. The
    note is internal to the organisation and never reaches her."""
    allowed = TRANSITIONS.get(application.status, frozenset())
    if status not in allowed:
        raise EmployerError("bad_transition", 409, {"from": application.status.value})
    now = datetime.now(UTC)
    application.status = status
    if status in (ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED):
        application.resolved_at = now
    application.status_history = [
        *application.status_history,
        {"status": status.value, "at": now.isoformat(), "note": (note or "")[:500] or None},
    ]
    await session.flush()
    await _audit(
        session,
        actor,
        "application.status",
        "application",
        application.id,
        {"status": status.value, "org": str(org.id)},
    )
    return application


# ---------------------------------------------------------------------------
# Discovery — opt-in, adults, anonymous
# ---------------------------------------------------------------------------


def candidate_ref(org: Organization | uuid.UUID, user_id: uuid.UUID) -> str:
    """A pseudonym for her that is stable for one organisation and different
    for every other — two organisations cannot tell they are looking at the
    same woman, and neither ever sees her id."""
    key = hashlib.sha256(f"candidate-ref:{settings.jwt_secret_key}".encode()).digest()
    message = f"{subject(org)}:{user_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:24]


async def discoverable(session: AsyncSession) -> list[uuid.UUID]:
    """Women who asked to be findable: latest discovery consent is yes, the
    account is active, and her age is known to be 18 or over."""
    rows = await session.execute(
        select(ConsentLog.user_id, ConsentLog.accepted)
        .where(
            ConsentLog.scope == ConsentScope.CANDIDATE_DISCOVERY,
            ConsentLog.subject_ref.is_(None),
        )
        .order_by(ConsentLog.user_id, ConsentLog.created_at.desc())
    )
    latest: dict[uuid.UUID, bool] = {}
    for user_id, accepted in rows.all():
        latest.setdefault(user_id, accepted)
    opted = [user_id for user_id, accepted in latest.items() if accepted][:DISCOVERY_LIMIT]
    if not opted:
        return []
    people = await session.execute(
        select(User.id, User.status, Profile)
        .join(Profile, Profile.user_id == User.id)
        .where(User.id.in_(opted))
    )
    out = []
    for user_id, status, profile in people.all():
        if status in (UserStatus.SUSPENDED, UserStatus.DELETED):
            continue
        age = age_from_profile(profile)
        if age is not None and age >= ADULT_AGE:
            out.append(user_id)
    return out


async def anonymous_card(session: AsyncSession, org: Organization, user_id: uuid.UUID) -> dict:
    """What an organisation sees before she accepts: no name, no contact, no
    id, no free text she wrote. Her region, experience, education level, her
    evidenced skills and how many certificates and passed tasks back them."""
    user = await session.get(User, user_id)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    skills = [
        item for item in await _skills_of(session, user_id) if item["status"] != "self_reported"
    ]
    certificates = await session.scalar(
        select(func.count())
        .select_from(Certificate)
        .where(Certificate.user_id == user_id, Certificate.revoked_at.is_(None))
    )
    passed = await session.scalar(
        select(func.count(func.distinct(TaskAttempt.task_id))).where(
            TaskAttempt.user_id == user_id, TaskAttempt.status == "passed"
        )
    )
    return {
        "ref": candidate_ref(org, user_id),
        "region": user.region.value if user and user.region else None,
        "education_level": profile.education_level if profile else None,
        "years_of_experience": profile.years_of_experience if profile else None,
        "skills": skills,
        "certificates": certificates or 0,
        "passed_tasks": passed or 0,
    }


async def search(
    session: AsyncSession,
    org: Organization,
    *,
    skill: str | None = None,
    region: str | None = None,
    limit: int = 30,
) -> list[dict]:
    members = set(
        (
            await session.scalars(
                select(OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == org.id
                )
            )
        ).all()
    )
    invited = dict(
        (
            await session.execute(
                select(OrganizationInvitation.user_id, OrganizationInvitation.status).where(
                    OrganizationInvitation.organization_id == org.id
                )
            )
        ).all()
    )
    wanted = None
    if skill:
        index = await skill_service.SkillIndex.load(session, [skill])
        wanted = index.key(skill)

    cards = []
    for user_id in await discoverable(session):
        if user_id in members:
            continue
        card = await anonymous_card(session, org, user_id)
        if region and card["region"] != region:
            continue
        if wanted and wanted not in {item["skill"]["slug"] for item in card["skills"]}:
            continue
        status = invited.get(user_id)
        card["invitation"] = status.value if status else None
        cards.append(card)
    cards.sort(key=lambda card: (-len(card["skills"]), -card["certificates"]))
    return cards[:limit]


async def invite(
    session: AsyncSession,
    org: Organization,
    *,
    ref: str,
    opportunity_id: uuid.UUID | None,
    message: str | None,
    actor: CurrentUser,
) -> OrganizationInvitation:
    """Invite a candidate by her pseudonym. The pseudonym is resolved only
    against women who are findable right now; one who switched discovery off
    since the search can no longer be reached."""
    target = next(
        (user_id for user_id in await discoverable(session) if candidate_ref(org, user_id) == ref),
        None,
    )
    if target is None:
        raise EmployerError("not_found", 404)
    if opportunity_id is not None:
        await own_listing(session, org, opportunity_id)
    pending = await session.scalar(
        select(func.count())
        .select_from(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == org.id,
            OrganizationInvitation.status == InvitationStatus.PENDING,
        )
    )
    if (pending or 0) >= MAX_PENDING_INVITES:
        raise EmployerError("too_many", 409)
    already = await session.scalar(
        select(OrganizationInvitation.id).where(
            OrganizationInvitation.organization_id == org.id,
            OrganizationInvitation.user_id == target,
            OrganizationInvitation.status == InvitationStatus.PENDING,
        )
    )
    if already is not None:
        raise EmployerError("already_invited", 409)

    invitation = OrganizationInvitation(
        organization_id=org.id,
        user_id=target,
        opportunity_id=opportunity_id,
        message=(message or "").strip()[:500] or None,
        status=InvitationStatus.PENDING,
        created_by_id=uuid.UUID(actor.id),
    )
    session.add(invitation)
    await session.flush()
    # The audit names the invitation, not the woman: an administrator reading
    # the log does not need to learn who an organisation was looking at.
    await _audit(
        session, actor, "candidate.invite", "invitation", invitation.id, {"org": str(org.id)}
    )
    return invitation


async def org_invitations(session: AsyncSession, org: Organization) -> list[dict]:
    """Its invitations. A candidate who accepted is shown by her profile; one
    who has not is still only her pseudonym."""
    rows = await session.execute(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.organization_id == org.id)
        .order_by(OrganizationInvitation.created_at.desc())
    )
    out = []
    for invitation in rows.scalars():
        profile = None
        if invitation.status == InvitationStatus.ACCEPTED and await shares_with(
            session, invitation.user_id, org
        ):
            profile = await candidate_profile(session, invitation.user_id)
        out.append(
            {
                "id": invitation.id,
                "ref": candidate_ref(org, invitation.user_id),
                "status": invitation.status,
                "opportunity_id": invitation.opportunity_id,
                "message": invitation.message,
                "created_at": invitation.created_at,
                "responded_at": invitation.responded_at,
                "profile": profile,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Her side of an invitation
# ---------------------------------------------------------------------------


async def my_invitations(session: AsyncSession, user_id: uuid.UUID) -> list[tuple]:
    rows = await session.execute(
        select(OrganizationInvitation, Organization, Opportunity)
        .join(Organization, Organization.id == OrganizationInvitation.organization_id)
        .outerjoin(Opportunity, Opportunity.id == OrganizationInvitation.opportunity_id)
        .where(OrganizationInvitation.user_id == user_id, Organization.is_active.is_(True))
        .order_by(OrganizationInvitation.created_at.desc())
    )
    return list(rows.all())


async def respond(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    invitation_id: uuid.UUID,
    accept: bool,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> OrganizationInvitation:
    """She answers. Accepting is her consent to share her profile with that
    organisation, appended to the consent log before anything is shown."""
    invitation = await session.get(OrganizationInvitation, invitation_id)
    if invitation is None or invitation.user_id != user_id:
        raise EmployerError("not_found", 404)
    if invitation.status != InvitationStatus.PENDING:
        raise EmployerError("answered", 409)
    now = datetime.now(UTC)
    if accept:
        await record_consent(
            session,
            user_id=user_id,
            scope=ConsentScope.SHARE_WITH_EMPLOYER,
            accepted=True,
            policy_version=PRIVACY_POLICY_VERSION,
            ip_address=ip_address,
            user_agent=user_agent,
            subject_ref=subject(invitation.organization_id),
        )
        # Audited the way consent given on an apply page is: the scope, how
        # it was given, and to which organisation.
        await record_audit(
            session,
            action="consent.grant",
            entity_type="consent",
            entity_id=ConsentScope.SHARE_WITH_EMPLOYER.value,
            actor_id=user_id,
            classification=DataClassification.PERSONAL,
            changes={"via": "invitation", "organization_id": str(invitation.organization_id)},
            ip_address=ip_address,
        )
    invitation.status = InvitationStatus.ACCEPTED if accept else InvitationStatus.DECLINED
    invitation.responded_at = now
    await session.flush()
    return invitation


async def withdraw_sharing(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """She stops sharing with one organisation. A new row, like every consent."""
    await record_consent(
        session,
        user_id=user_id,
        scope=ConsentScope.SHARE_WITH_EMPLOYER,
        accepted=False,
        policy_version=PRIVACY_POLICY_VERSION,
        ip_address=ip_address,
        user_agent=user_agent,
        subject_ref=subject(org_id),
    )
    await record_audit(
        session,
        action="consent.withdraw",
        entity_type="consent",
        entity_id=ConsentScope.SHARE_WITH_EMPLOYER.value,
        actor_id=user_id,
        classification=DataClassification.PERSONAL,
        changes={"organization_id": str(org_id)},
        ip_address=ip_address,
    )
    await session.flush()


async def sharing_with(session: AsyncSession, user_id: uuid.UUID) -> list[Organization]:
    """The organisations her profile is currently shared with."""
    rows = await session.execute(
        select(ConsentLog.subject_ref, ConsentLog.accepted)
        .where(
            ConsentLog.user_id == user_id,
            ConsentLog.scope == ConsentScope.SHARE_WITH_EMPLOYER,
            ConsentLog.subject_ref.is_not(None),
        )
        .order_by(ConsentLog.subject_ref, ConsentLog.created_at.desc())
    )
    latest: dict[str, bool] = {}
    for ref, accepted in rows.all():
        latest.setdefault(ref, accepted)
    ids = []
    for ref, accepted in latest.items():
        if accepted:
            try:
                ids.append(uuid.UUID(ref))
            except ValueError:
                continue
    if not ids:
        return []
    return list(
        (
            await session.execute(
                select(Organization).where(Organization.id.in_(ids)).order_by(Organization.name)
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------


def listing_is_open(item: Opportunity) -> bool:
    return eligibility.is_open(item)


async def _audit(
    session: AsyncSession,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    changes: dict,
) -> None:
    await record_audit(
        session,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor_id=uuid.UUID(actor.id),
        actor_role=actor.role.value,
        classification=DataClassification.PERSONAL
        if entity_type in ("application", "invitation")
        else DataClassification.INTERNAL,
        changes=changes,
    )
