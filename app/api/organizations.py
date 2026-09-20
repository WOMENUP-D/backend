"""Organisations: the public page, the workspace, her invitations, onboarding.

Four audiences, four sets of routes, and each checks its own caller on the
server:

* **Anyone** reads a published organisation by its slug.
* **Its members** (PARTNER or TRAINER, and a member of *that* organisation)
  run the workspace under `/organizations/{org_id}/…`. Any other organisation's
  id — or a listing, an application or an invitation that is not theirs — is a
  404, the same answer as one that does not exist.
* **She** answers invitations and stops sharing with an organisation under
  `/organizations/me/…`, always for the user in the token.
* **Administrators** onboard, verify, suspend and staff organisations under
  `/admin/organizations`. Every one of those acts is audited.

No route takes a woman's user id. An organisation reaches a candidate who has
not accepted an invitation only by her pseudonym.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import AdminDep, CurrentUserDep, DbSession, client_ip
from app.core.constants import OrgMemberRole
from app.models.opportunity import Opportunity
from app.models.organization import Organization, OrganizationMember
from app.models.profile import Profile
from app.models.user import User
from app.schemas.common import Message
from app.schemas.opportunity import OrganizationBrief
from app.schemas.organization import (
    CandidateCard,
    CandidateProfile,
    InvitationAnswer,
    InviteIn,
    ListingIn,
    ListingUpdate,
    MemberIn,
    MemberRead,
    MyInvitationRead,
    OrganizationAdminRead,
    OrganizationCreate,
    OrganizationProfileIn,
    OrganizationPublic,
    OrganizationRead,
    OrganizationStatusIn,
    OrgApplicationRead,
    OrgInvitationRead,
    OrgListingRead,
    OrgListingSummary,
    OrgProgrammeSummary,
    StatusIn,
)
from app.services import employer, organizations
from app.services import opportunities as discovery
from app.services import skills as skill_service
from app.services.eligibility import is_open

router = APIRouter(prefix="/organizations", tags=["organizations"])
admin_router = APIRouter(prefix="/admin/organizations", tags=["admin"])


def _fail(exc: organizations.OrgError | employer.EmployerError) -> HTTPException:
    detail: dict = {"reason": exc.reason}
    if isinstance(exc, employer.EmployerError):
        detail.update(exc.detail)
    return HTTPException(status_code=exc.status_code, detail=detail)


async def _member(
    session: DbSession, user, org_id: uuid.UUID, *, owner: bool = False
) -> tuple[Organization, OrganizationMember]:
    try:
        return await organizations.require_member(session, user, org_id, owner=owner)
    except organizations.OrgError as exc:
        raise _fail(exc) from exc


def _read(org: Organization, role: OrgMemberRole | None = None) -> OrganizationRead:
    return OrganizationRead(
        id=org.id,
        slug=org.slug,
        name=org.name,
        kind=org.kind,
        description_i18n=org.description_i18n,
        industry=org.industry,
        region=org.region,
        city=org.city,
        website=org.website,
        logo_url=org.logo_url,
        is_public=org.is_public,
        is_verified=org.is_verified,
        verified_at=org.verified_at,
        is_active=org.is_active,
        my_role=role,
    )


def _summary(item: Opportunity) -> OrgListingSummary:
    return OrgListingSummary(
        id=item.id,
        type=item.type,
        title_i18n=item.title_i18n,
        region=item.region,
        deadline=item.deadline,
    )


# ---------------------------------------------------------------------------
# Her side — declared before `/{org_id}` so "me" is never read as an id
# ---------------------------------------------------------------------------


@router.get("/me/invitations", response_model=list[MyInvitationRead])
async def my_invitations(user: CurrentUserDep, session: DbSession) -> list[MyInvitationRead]:
    """Organisations that asked her to consider them, newest first."""
    rows = await employer.my_invitations(session, uuid.UUID(user.id))
    return [
        MyInvitationRead(
            id=invitation.id,
            status=invitation.status,
            message=invitation.message,
            created_at=invitation.created_at,
            organization=discovery.brief(org),
            opportunity=_summary(listing) if listing is not None else None,
            fields=list(employer.EMPLOYER_FIELDS),
        )
        for invitation, org, listing in rows
    ]


@router.post("/me/invitations/{invitation_id}", response_model=MyInvitationRead)
async def answer_invitation(
    invitation_id: uuid.UUID,
    payload: InvitationAnswer,
    user: CurrentUserDep,
    session: DbSession,
    request: Request,
) -> MyInvitationRead:
    """Accept or decline. Accepting is her consent to share her profile with
    that organisation — recorded before the organisation can see anything."""
    try:
        invitation = await employer.respond(
            session,
            user_id=uuid.UUID(user.id),
            invitation_id=invitation_id,
            accept=payload.accept,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    org = await session.get(Organization, invitation.organization_id)
    listing = (
        await session.get(Opportunity, invitation.opportunity_id)
        if invitation.opportunity_id
        else None
    )
    return MyInvitationRead(
        id=invitation.id,
        status=invitation.status,
        message=invitation.message,
        created_at=invitation.created_at,
        organization=discovery.brief(org),
        opportunity=_summary(listing) if listing else None,
        fields=list(employer.EMPLOYER_FIELDS),
    )


@router.get("/me/sharing", response_model=list[OrganizationBrief])
async def my_sharing(user: CurrentUserDep, session: DbSession) -> list[OrganizationBrief]:
    """Organisations her profile is shared with right now."""
    return [
        discovery.brief(org) for org in await employer.sharing_with(session, uuid.UUID(user.id))
    ]


@router.delete("/me/sharing/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_sharing(
    org_id: uuid.UUID, user: CurrentUserDep, session: DbSession, request: Request
) -> None:
    """Stop sharing with one organisation. Appends a consent row; from then on
    its people see her applications without her profile."""
    await employer.withdraw_sharing(
        session,
        user_id=uuid.UUID(user.id),
        org_id=org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/mine", response_model=list[OrganizationRead])
async def mine(user: CurrentUserDep, session: DbSession) -> list[OrganizationRead]:
    """The organisations the caller acts for."""
    return [_read(org, role) for org, role in await organizations.mine(session, user)]


@router.get("/public/{slug}", response_model=OrganizationPublic)
async def public_profile(slug: str, session: DbSession) -> OrganizationPublic:
    """A published organisation, as anyone may read it."""
    org = await organizations.public_by_slug(session, slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return OrganizationPublic(
        slug=org.slug,
        name=org.name,
        kind=org.kind,
        is_verified=org.is_verified,
        description_i18n=org.description_i18n,
        industry=org.industry,
        region=org.region,
        city=org.city,
        website=org.website,
        logo_url=org.logo_url,
        listings=[_summary(item) for item in await organizations.open_listings(session, org)],
        programmes=[
            OrgProgrammeSummary(id=p.id, slug=p.slug, title_i18n=p.title_i18n)
            for p in await organizations.programmes(session, org)
        ],
    )


# ---------------------------------------------------------------------------
# The workspace — members only
# ---------------------------------------------------------------------------


@router.get("/{org_id}", response_model=OrganizationRead)
async def read_organization(
    org_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> OrganizationRead:
    org, member = await _member(session, user, org_id)
    return _read(org, member.role)


@router.patch("/{org_id}", response_model=OrganizationRead)
async def update_organization(
    org_id: uuid.UUID, payload: OrganizationProfileIn, user: CurrentUserDep, session: DbSession
) -> OrganizationRead:
    """Its owner edits the profile and decides whether it is public."""
    org, member = await _member(session, user, org_id, owner=True)
    try:
        await organizations.update_profile(
            session, org, changes=payload.model_dump(exclude_unset=True), actor=user
        )
    except organizations.OrgError as exc:
        raise _fail(exc) from exc
    return _read(org, member.role)


@router.get("/{org_id}/members", response_model=list[MemberRead])
async def members(org_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> list[MemberRead]:
    """Its people, by first name and role — no contact details."""
    org, _ = await _member(session, user, org_id)
    rows = await session.execute(
        select(OrganizationMember, Profile.full_name)
        .outerjoin(Profile, Profile.user_id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org.id)
        .order_by(OrganizationMember.role)
    )
    return [
        MemberRead(
            user_id=member.user_id,
            role=member.role,
            first_name=(name or "").split()[0] if name else None,
        )
        for member, name in rows.all()
    ]


async def _listing_read(session: DbSession, item: Opportunity, count: int) -> OrgListingRead:
    index = await skill_service.SkillIndex.load(session, item.required_skills)
    return OrgListingRead(
        id=item.id,
        type=item.type,
        title_i18n=item.title_i18n,
        description_i18n=item.description_i18n,
        region=item.region,
        skills=index.refs(item.required_skills),
        reward=item.reward,
        eligibility=item.eligibility,
        deadline=item.deadline,
        starts_at=item.starts_at,
        ends_at=item.ends_at,
        format=item.format,
        venue=item.venue,
        is_open=is_open(item),
        applications=count,
        created_at=item.created_at,
    )


@router.get("/{org_id}/listings", response_model=list[OrgListingRead])
async def listings(
    org_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> list[OrgListingRead]:
    org, _ = await _member(session, user, org_id)
    return [
        await _listing_read(session, item, count)
        for item, count in await employer.listings(session, org)
    ]


@router.post(
    "/{org_id}/listings", response_model=OrgListingRead, status_code=status.HTTP_201_CREATED
)
async def publish_listing(
    org_id: uuid.UUID, payload: ListingIn, user: CurrentUserDep, session: DbSession
) -> OrgListingRead:
    """Publish a listing. It joins the catalogue at once, under the same
    eligibility rule and the same matching as every other listing."""
    org, _ = await _member(session, user, org_id)
    try:
        item = await employer.publish(
            session, org, data=payload.model_dump(mode="python"), actor=user
        )
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    return await _listing_read(session, item, 0)


@router.patch("/{org_id}/listings/{listing_id}", response_model=OrgListingRead)
async def edit_listing(
    org_id: uuid.UUID,
    listing_id: uuid.UUID,
    payload: ListingUpdate,
    user: CurrentUserDep,
    session: DbSession,
) -> OrgListingRead:
    org, _ = await _member(session, user, org_id)
    try:
        item = await employer.own_listing(session, org, listing_id)
        await employer.edit(session, item, data=payload.model_dump(exclude_unset=True), actor=user)
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    return await _listing_read(session, item, 0)


@router.post("/{org_id}/listings/{listing_id}/close", response_model=OrgListingRead)
async def close_listing(
    org_id: uuid.UUID, listing_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> OrgListingRead:
    """Stop taking applications. The listing stays readable, marked closed."""
    org, _ = await _member(session, user, org_id)
    try:
        item = await employer.own_listing(session, org, listing_id)
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    await employer.close(session, item, actor=user)
    return await _listing_read(session, item, 0)


def _application_read(found: employer.OrgApplication) -> OrgApplicationRead:
    application = found.application
    return OrgApplicationRead(
        id=application.id,
        status=application.status,
        submitted_at=application.submitted_at,
        resolved_at=application.resolved_at,
        status_history=list(application.status_history or []),
        listing=_summary(found.listing),
        profile=CandidateProfile(**found.profile) if found.profile else None,
        hidden=found.hidden,
        next=sorted(employer.TRANSITIONS.get(application.status, frozenset())),
    )


@router.get("/{org_id}/applications", response_model=list[OrgApplicationRead])
async def applications(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: DbSession,
    listing_id: uuid.UUID | None = None,
) -> list[OrgApplicationRead]:
    """Applications to its own listings, and to no others."""
    org, _ = await _member(session, user, org_id)
    return [
        _application_read(found)
        for found in await employer.applications(session, org, opportunity_id=listing_id)
    ]


@router.get("/{org_id}/applications/{application_id}", response_model=OrgApplicationRead)
async def application(
    org_id: uuid.UUID, application_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> OrgApplicationRead:
    org, _ = await _member(session, user, org_id)
    try:
        return _application_read(await employer.own_application(session, org, application_id))
    except employer.EmployerError as exc:
        raise _fail(exc) from exc


@router.post("/{org_id}/applications/{application_id}/status", response_model=OrgApplicationRead)
async def application_status(
    org_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: StatusIn,
    user: CurrentUserDep,
    session: DbSession,
) -> OrgApplicationRead:
    """Move an application on. Only forward, only to a status the platform
    has, and never one she withdrew. The note stays with the organisation."""
    org, _ = await _member(session, user, org_id)
    try:
        found = await employer.own_application(session, org, application_id)
        await employer.set_status(
            session,
            org,
            found.application,
            status=payload.status,
            note=payload.note,
            actor=user,
        )
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    return _application_read(await employer.own_application(session, org, application_id))


@router.get("/{org_id}/candidates", response_model=list[CandidateCard])
async def candidates(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: DbSession,
    skill: str | None = Query(default=None, max_length=100),
    region: str | None = Query(default=None, max_length=60),
) -> list[CandidateCard]:
    """Women who asked to be findable — by pseudonym, never by id or name."""
    org, _ = await _member(session, user, org_id)
    return [
        CandidateCard(**card)
        for card in await employer.search(session, org, skill=skill, region=region)
    ]


@router.post(
    "/{org_id}/invitations", response_model=OrgInvitationRead, status_code=status.HTTP_201_CREATED
)
async def invite(
    org_id: uuid.UUID, payload: InviteIn, user: CurrentUserDep, session: DbSession
) -> OrgInvitationRead:
    org, _ = await _member(session, user, org_id)
    try:
        invitation = await employer.invite(
            session,
            org,
            ref=payload.ref,
            opportunity_id=payload.opportunity_id,
            message=payload.message,
            actor=user,
        )
    except employer.EmployerError as exc:
        raise _fail(exc) from exc
    return OrgInvitationRead(
        id=invitation.id,
        ref=payload.ref,
        status=invitation.status,
        opportunity_id=invitation.opportunity_id,
        message=invitation.message,
        created_at=invitation.created_at,
    )


@router.get("/{org_id}/invitations", response_model=list[OrgInvitationRead])
async def invitations(
    org_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> list[OrgInvitationRead]:
    org, _ = await _member(session, user, org_id)
    return [
        OrgInvitationRead(
            **{**row, "profile": CandidateProfile(**row["profile"]) if row["profile"] else None}
        )
        for row in await employer.org_invitations(session, org)
    ]


# ---------------------------------------------------------------------------
# Administrators — every act audited
# ---------------------------------------------------------------------------


async def _admin_read(session: DbSession, org: Organization) -> OrganizationAdminRead:
    rows = await session.execute(
        select(OrganizationMember, User.email, Profile.full_name)
        .join(User, User.id == OrganizationMember.user_id)
        .outerjoin(Profile, Profile.user_id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org.id)
    )
    count = await session.scalar(
        select(func.count()).select_from(Opportunity).where(Opportunity.organization_id == org.id)
    )
    return OrganizationAdminRead(
        **_read(org).model_dump(),
        members=[
            MemberRead(
                user_id=member.user_id,
                role=member.role,
                first_name=(name or "").split()[0] if name else None,
                email=email,
            )
            for member, email, name in rows.all()
        ],
        listings=count or 0,
    )


async def _org_or_404(session: DbSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return org


@admin_router.get("", response_model=list[OrganizationAdminRead])
async def admin_list(user: AdminDep, session: DbSession) -> list[OrganizationAdminRead]:
    rows = await session.execute(select(Organization).order_by(Organization.name))
    return [await _admin_read(session, org) for org in rows.scalars()]


@admin_router.post("", response_model=OrganizationAdminRead, status_code=status.HTTP_201_CREATED)
async def admin_create(
    payload: OrganizationCreate, user: AdminDep, session: DbSession
) -> OrganizationAdminRead:
    """Onboard an organisation. It starts private and unverified."""
    try:
        org = await organizations.create(session, name=payload.name, kind=payload.kind, admin=user)
    except organizations.OrgError as exc:
        raise _fail(exc) from exc
    return await _admin_read(session, org)


@admin_router.patch("/{org_id}", response_model=OrganizationAdminRead)
async def admin_status(
    org_id: uuid.UUID, payload: OrganizationStatusIn, user: AdminDep, session: DbSession
) -> OrganizationAdminRead:
    """Verify or unverify; suspend or reinstate. Suspending takes its listings
    down everywhere at once."""
    org = await _org_or_404(session, org_id)
    await organizations.set_status(
        session, org, admin=user, verified=payload.is_verified, active=payload.is_active
    )
    return await _admin_read(session, org)


@admin_router.post("/{org_id}/members", response_model=OrganizationAdminRead)
async def admin_add_member(
    org_id: uuid.UUID, payload: MemberIn, user: AdminDep, session: DbSession
) -> OrganizationAdminRead:
    org = await _org_or_404(session, org_id)
    try:
        await organizations.add_member(
            session, org, email=payload.email, role=payload.role, admin=user
        )
    except organizations.OrgError as exc:
        raise _fail(exc) from exc
    return await _admin_read(session, org)


@admin_router.delete("/{org_id}/members/{user_id}", response_model=Message)
async def admin_remove_member(
    org_id: uuid.UUID, user_id: uuid.UUID, user: AdminDep, session: DbSession
) -> Message:
    org = await _org_or_404(session, org_id)
    try:
        await organizations.remove_member(session, org, user_id=user_id, admin=user)
    except organizations.OrgError as exc:
        raise _fail(exc) from exc
    return Message(detail="Member removed")
