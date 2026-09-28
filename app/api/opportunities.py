"""Opportunities, applications and outcomes.

The catalogue and a single listing are open to visitors; her fit, her
eligibility, applying, saving and tracking answer for the caller in the token
and for nobody else. No route here takes a user id.

What a listing says about her is decided in `services.opportunities`; whether
she may apply is `services.eligibility`, checked again at the apply endpoint
whatever the page showed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUserDep, DbSession, OptionalUserDep, client_ip
from app.core.constants import (
    ApplicationStatus,
    ConsentScope,
    OpportunitySource,
    OpportunityType,
)
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.schemas.common import Page, PaginationParams
from app.schemas.opportunity import (
    ApplicationCreate,
    ApplicationRead,
    ApplyIn,
    BusinessRead,
    OpportunityCard,
    OpportunityDetail,
    OpportunityDiscover,
    OpportunityMatch,
    OpportunityRead,
    OpportunityStats,
    OutcomeRead,
    SkillGap,
)
from app.services import opportunities as discovery
from app.services import skills
from app.services.audit_service import has_consent
from app.services.integration_gateway import ConsentMissingError
from app.services.recommendation import analyse_skill_gap, match_opportunities

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

# Kept importable from here: the integrations tests and older callers read it.
SOURCE_TO_SYSTEM = discovery.SOURCE_TO_SYSTEM


def _open():
    """Active and not past its deadline — what "open" means everywhere. Events
    are listed on their own page (`/events`), so these counts leave them out."""
    return (
        Opportunity.is_active.is_(True),
        or_(Opportunity.deadline.is_(None), Opportunity.deadline > datetime.now(UTC)),
        Opportunity.starts_at.is_(None),
    )


@router.get("", response_model=Page[OpportunityRead])
async def list_opportunities(
    session: DbSession,
    pagination: PaginationParams = Depends(),
    source: OpportunitySource | None = None,
    type: OpportunityType | None = None,
    region: str | None = None,
) -> Page[OpportunityRead]:
    """Open partner listings, open to visitors.

    A vacancy with a salary on it is the most convincing thing this portal can
    show someone who has not signed up. Only listings that are active *and*
    not past their deadline are returned — a closed vacancy on the front page
    is a promise the portal cannot keep. Nothing here is personal: matching,
    applying and consent all stay behind an account.
    """
    stmt = select(Opportunity).where(*_open())
    if source:
        stmt = stmt.where(Opportunity.source == source)
    if type:
        stmt = stmt.where(Opportunity.type == type)
    if region:
        stmt = stmt.where(Opportunity.region == region)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(Opportunity.deadline.asc().nullslast())
        .offset(pagination.offset)
        .limit(pagination.size)
    )
    return Page[OpportunityRead](
        items=[OpportunityRead.model_validate(o) for o in rows.scalars()],
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


@router.get("/stats", response_model=OpportunityStats)
async def stats(session: DbSession) -> OpportunityStats:
    """What the catalogue holds right now, open to visitors.

    A visitor deciding whether to register is owed the size and the shape of
    the offer, not an adjective. Counted over the same active-only set the
    public listing returns; nothing here is personal, so it stays open while
    matching and applying stay behind an account.
    """
    active = _open()

    total = await session.scalar(select(func.count()).select_from(Opportunity).where(*active)) or 0

    async def _counts(column) -> dict[str, int]:
        rows = await session.execute(select(column, func.count()).where(*active).group_by(column))
        # str_enum keeps the database on enum *values*; a driver may still hand
        # back the Python enum, so normalise before it reaches the API.
        return {getattr(key, "value", key): count for key, count in rows.all()}

    regions = (
        await session.scalar(
            select(func.count(func.distinct(Opportunity.region))).where(
                *active, Opportunity.region.is_not(None)
            )
        )
        or 0
    )

    return OpportunityStats(
        total=total,
        by_type=await _counts(Opportunity.type),
        by_source=await _counts(Opportunity.source),
        regions=regions,
    )


@router.get("/discover", response_model=OpportunityDiscover)
async def discover(
    session: DbSession,
    user: OptionalUserDep,
    type: OpportunityType | None = None,
    region: str | None = Query(default=None, max_length=60),
    skill: str | None = Query(default=None, max_length=100),
    search: str | None = Query(default=None, max_length=100),
    include_closed: bool = False,
    business: bool = False,
    sort: str | None = Query(default=None, pattern="^(match|deadline)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
) -> OpportunityDiscover:
    """The catalogue as a woman searches it, with her fit on each listing.

    Open to visitors. Signed in, each listing also carries how her skills
    stand against it, whether she may apply, her application and whether she
    saved it — all computed on the server. Filters are only the ones the data
    can answer: kind, region, skill, open or closed. There is no remote or
    experience filter because no listing records either.
    """
    return await discovery.discover(
        session,
        uuid.UUID(user.id) if user else None,
        discovery.Filters(
            type=type,
            region=region,
            skill=skill,
            search=search,
            include_closed=include_closed,
            business=business,
            sort=sort,
            page=page,
            size=size,
        ),
    )


@router.get("/business", response_model=BusinessRead)
async def business(user: CurrentUserDep, session: DbSession) -> BusinessRead:
    """ "For your business": a few open listings that support a business of her
    own, each with why it is here. Never everything, and never one with no
    reason — see `services.opportunities.business_for`."""
    return await discovery.business_for(session, uuid.UUID(user.id))


@router.get("/recommended", response_model=list[OpportunityMatch])
async def recommended(
    user: CurrentUserDep, session: DbSession, limit: int = 10
) -> list[OpportunityMatch]:
    """Ranked by skill coverage, each with its explanation."""
    matches = await match_opportunities(session, user_id=uuid.UUID(user.id), limit=limit)
    # Resolved in one pass so the skills read in her language rather than in
    # whichever one the listing was written in.
    index = await skills.SkillIndex.load(
        session, [label for _, matched, missing, _ in matches for label in (*matched, *missing)]
    )
    return [
        OpportunityMatch(
            **OpportunityRead.model_validate(opportunity).model_dump(),
            match_score=coverage,
            matched_skills=index.refs(matched),
            missing_skills=index.refs(missing),
            explanation=(
                f"{len(matched)} ta ko‘nikmangiz mos keladi"
                + (f"; {len(missing)} ta ko‘nikma yetishmayapti" if missing else "")
            ),
        )
        for opportunity, matched, missing, coverage in matches
    ]


@router.get("/{opportunity_id}/skill-gap", response_model=SkillGap)
async def skill_gap(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> SkillGap:
    """What the user is missing for this role, and which courses close it."""
    gap = await analyse_skill_gap(
        session, user_id=uuid.UUID(user.id), opportunity_id=opportunity_id
    )
    if gap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return gap


@router.post("/apply", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
async def apply(
    payload: ApplicationCreate, user: CurrentUserDep, session: DbSession
) -> ApplicationRead:
    """Submit an application and queue it for the partner platform.

    Requires consent for the destination platform; without it the request is
    refused rather than silently downgraded.
    """
    opportunity = await discovery.visible(session, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    # The older route: consent must already be on file, as it always required.
    # It runs through the same checks as the confirm flow below.
    application = await _submit(session, user, opportunity, consent=False, answers=payload.payload)
    return discovery.application_read(application, opportunity)


async def _submit(
    session: DbSession,
    user,
    opportunity: Opportunity,
    *,
    consent: bool,
    answers: dict | None = None,
    request: Request | None = None,
) -> Application:
    try:
        return await discovery.apply(
            session,
            user_id=uuid.UUID(user.id),
            opportunity=opportunity,
            consent=consent,
            ip_address=client_ip(request) if request else None,
            user_agent=request.headers.get("user-agent") if request else None,
            answers=answers,
        )
    except discovery.ApplyError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"reason": exc.reason, **exc.detail}
        ) from exc
    except ConsentMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"reason": "consent_required"}
        ) from exc


@router.get("/me/applications", response_model=list[ApplicationRead])
async def my_applications(user: CurrentUserDep, session: DbSession) -> list[ApplicationRead]:
    """Her applications, newest first, each with the listing it went to and
    every status it has been in."""
    return await discovery.applications_for(session, uuid.UUID(user.id))


@router.get("/me/saved", response_model=list[OpportunityCard])
async def my_saved(user: CurrentUserDep, session: DbSession) -> list[OpportunityCard]:
    """Listings she kept, with the same reading the catalogue gives them."""
    return await discovery.saved_for(session, uuid.UUID(user.id))


@router.post("/applications/{application_id}/withdraw", response_model=ApplicationRead)
async def withdraw(
    application_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> ApplicationRead:
    """Withdraw her own open application. Recorded here; the partner is not told yet."""
    application = await session.get(Application, application_id)
    if application is None or application.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status in (ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resolved application cannot be withdrawn",
        )
    if application.status == ApplicationStatus.WITHDRAWN:
        # Already withdrawn: saying so again writes nothing new.
        return discovery.application_read(
            application, await session.get(Opportunity, application.opportunity_id)
        )

    now = datetime.now(UTC)
    application.status = ApplicationStatus.WITHDRAWN
    application.resolved_at = now
    application.status_history = [
        *application.status_history,
        {"status": ApplicationStatus.WITHDRAWN.value, "at": now.isoformat()},
    ]
    await session.flush()
    return discovery.application_read(
        application, await session.get(Opportunity, application.opportunity_id)
    )


@router.get("/me/outcomes", response_model=list[OutcomeRead])
async def my_outcomes(user: CurrentUserDep, session: DbSession) -> list[OutcomeRecord]:
    """Confirmed results: hired, business registered, funded, first sale."""
    rows = await session.execute(
        select(OutcomeRecord)
        .where(OutcomeRecord.user_id == uuid.UUID(user.id))
        .order_by(OutcomeRecord.occurred_at.desc().nullslast())
    )
    return list(rows.scalars())


@router.get("/consent-status", response_model=dict[str, bool])
async def consent_status(user: CurrentUserDep, session: DbSession) -> dict[str, bool]:
    """Which partner platforms this user has authorised."""
    user_id = uuid.UUID(user.id)
    return {
        "edu_job": await has_consent(session, user_id, ConsentScope.SHARE_EDU_JOB),
        "invest_hub": await has_consent(session, user_id, ConsentScope.SHARE_INVEST_HUB),
        "commerce": await has_consent(session, user_id, ConsentScope.SHARE_COMMERCE),
    }


# Declared last: a UUID path segment would otherwise swallow the static routes
# above ("/stats", "/discover", "/me/...") and answer them with a 422.


@router.get("/{opportunity_id}", response_model=OpportunityDetail)
async def read_opportunity(
    opportunity_id: uuid.UUID, session: DbSession, user: OptionalUserDep
) -> OpportunityDetail:
    """One listing: what it asks for, how she stands against it, whether she
    may apply, and — before she does — exactly what applying would share."""
    opportunity = await discovery.visible(session, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return await discovery.detail(session, opportunity, uuid.UUID(user.id) if user else None)


@router.post(
    "/{opportunity_id}/apply", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED
)
async def apply_to(
    opportunity_id: uuid.UUID,
    payload: ApplyIn,
    user: CurrentUserDep,
    session: DbSession,
    request: Request,
) -> ApplicationRead:
    """Apply, from the confirm step.

    `consent: true` is her agreement, given on that step, to share the listed
    fields with the listing's partner platform; it is recorded as a new consent
    row before anything is queued. Eligibility is checked here again whatever
    the page showed.
    """
    opportunity = await discovery.visible(session, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    application = await _submit(
        session, user, opportunity, consent=payload.consent, request=request
    )
    return discovery.application_read(application, opportunity)


@router.put("/{opportunity_id}/save", status_code=status.HTTP_204_NO_CONTENT)
async def save_opportunity(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> None:
    """Keep a listing to come back to. Idempotent; nobody is told."""
    opportunity = await discovery.visible(session, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    await discovery.save(session, user_id=uuid.UUID(user.id), opportunity=opportunity)


@router.delete("/{opportunity_id}/save", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_opportunity(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> None:
    """Stop keeping a listing. Idempotent."""
    await discovery.unsave(session, user_id=uuid.UUID(user.id), opportunity_id=opportunity_id)
