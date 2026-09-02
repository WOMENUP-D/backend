"""Opportunities, applications and outcomes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUserDep, DbSession
from app.core.constants import (
    ApplicationStatus,
    ConsentScope,
    IntegrationSystem,
    OpportunitySource,
    OpportunityType,
)
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.profile import Profile
from app.models.user import User
from app.schemas.common import Page, PaginationParams
from app.schemas.opportunity import (
    ApplicationCreate,
    ApplicationRead,
    OpportunityMatch,
    OpportunityRead,
    OpportunityStats,
    OutcomeRead,
    SkillGap,
)
from app.services.audit_service import has_consent
from app.services.integration_gateway import (
    ConsentMissingError,
    minimal_profile_payload,
    queue_event,
)
from app.services.recommendation import analyse_skill_gap, match_opportunities

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

SOURCE_TO_SYSTEM = {
    OpportunitySource.EDU_JOB: IntegrationSystem.EDU_JOB,
    OpportunitySource.INVEST_HUB: IntegrationSystem.INVEST_HUB,
    OpportunitySource.COMMERCE: IntegrationSystem.COMMERCE,
}


@router.get("", response_model=Page[OpportunityRead])
async def list_opportunities(
    session: DbSession,
    pagination: PaginationParams = Depends(),
    source: OpportunitySource | None = None,
    type: OpportunityType | None = None,
    region: str | None = None,
) -> Page[OpportunityRead]:
    """Active partner listings, open to visitors.

    A vacancy with a salary on it is the most convincing thing this portal can
    show someone who has not signed up. Only active listings are returned and
    nothing here is personal — matching, applying and consent all stay behind
    an account.
    """
    stmt = select(Opportunity).where(Opportunity.is_active.is_(True))
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
    active = Opportunity.is_active.is_(True)

    total = await session.scalar(select(func.count()).select_from(Opportunity).where(active)) or 0

    async def _counts(column) -> dict[str, int]:
        rows = await session.execute(select(column, func.count()).where(active).group_by(column))
        # str_enum keeps the database on enum *values*; a driver may still hand
        # back the Python enum, so normalise before it reaches the API.
        return {getattr(key, "value", key): count for key, count in rows.all()}

    regions = (
        await session.scalar(
            select(func.count(func.distinct(Opportunity.region))).where(
                active, Opportunity.region.is_not(None)
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


@router.get("/recommended", response_model=list[OpportunityMatch])
async def recommended(
    user: CurrentUserDep, session: DbSession, limit: int = 10
) -> list[OpportunityMatch]:
    """Ranked by skill coverage, each with its explanation."""
    matches = await match_opportunities(session, user_id=uuid.UUID(user.id), limit=limit)
    return [
        OpportunityMatch(
            **OpportunityRead.model_validate(opportunity).model_dump(),
            match_score=coverage,
            matched_skills=matched,
            missing_skills=missing,
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
) -> Application:
    """Submit an application and queue it for the partner platform.

    Requires consent for the destination platform; without it the request is
    refused rather than silently downgraded.
    """
    user_id = uuid.UUID(user.id)
    opportunity = await session.get(Opportunity, payload.opportunity_id)
    if opportunity is None or not opportunity.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    existing = await session.scalar(
        select(Application).where(
            Application.user_id == user_id,
            Application.opportunity_id == payload.opportunity_id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already applied to this opportunity",
        )

    now = datetime.now(UTC)
    application = Application(
        user_id=user_id,
        opportunity_id=payload.opportunity_id,
        status=ApplicationStatus.SUBMITTED,
        payload=payload.payload,
        submitted_at=now,
        status_history=[{"status": ApplicationStatus.SUBMITTED.value, "at": now.isoformat()}],
    )
    session.add(application)
    await session.flush()

    if opportunity.source in SOURCE_TO_SYSTEM:
        record = await session.get(User, user_id)
        profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
        try:
            await queue_event(
                session,
                system=SOURCE_TO_SYSTEM[opportunity.source],
                event_type="application.submit",
                user_id=user_id,
                reference=str(application.id),
                payload={
                    "application_id": str(application.id),
                    "opportunity_external_id": opportunity.external_id,
                    "applicant": minimal_profile_payload(record, profile),
                    "answers": payload.payload,
                },
            )
        except ConsentMissingError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return application


@router.get("/me/applications", response_model=list[ApplicationRead])
async def my_applications(user: CurrentUserDep, session: DbSession) -> list[Application]:
    rows = await session.execute(
        select(Application)
        .where(Application.user_id == uuid.UUID(user.id))
        .order_by(Application.created_at.desc())
    )
    return list(rows.scalars())


@router.post("/applications/{application_id}/withdraw", response_model=ApplicationRead)
async def withdraw(
    application_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> Application:
    application = await session.get(Application, application_id)
    if application is None or application.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    if application.status in (ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resolved application cannot be withdrawn",
        )

    now = datetime.now(UTC)
    application.status = ApplicationStatus.WITHDRAWN
    application.resolved_at = now
    application.status_history = [
        *application.status_history,
        {"status": ApplicationStatus.WITHDRAWN.value, "at": now.isoformat()},
    ]
    await session.flush()
    return application


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
