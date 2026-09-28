"""Current user, profile, goals and consent."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession, client_ip
from app.core.constants import SUBJECT_SCOPES, ConsentScope, DataClassification, Role
from app.models.profile import Goal, Profile
from app.models.user import User
from app.schemas.admin import ConsentRead, ConsentUpdate
from app.schemas.common import Message
from app.schemas.user import (
    ActivityDay,
    ActivitySummary,
    GoalCreate,
    GoalRead,
    GoalUpdate,
    ProfileRead,
    ProfileReadFull,
    ProfileUpdate,
    UserRead,
    UserUpdate,
)
from app.services import activity, skills
from app.services.audit_service import current_consents, record_audit, record_consent

router = APIRouter(prefix="/users", tags=["users"])

# Fields that count towards the profile completeness indicator.
COMPLETENESS_FIELDS = (
    "full_name",
    "birth_date",
    "district",
    "education_level",
    "employment_status",
    "profession",
    "skills",
    "interests",
)


def _completeness(profile: Profile) -> int:
    filled = sum(1 for f in COMPLETENESS_FIELDS if getattr(profile, f, None))
    return round(filled * 100 / len(COMPLETENESS_FIELDS))


@router.get("/me", response_model=UserRead)
async def read_me(user: CurrentUserDep, session: DbSession) -> User:
    record = await session.get(User, uuid.UUID(user.id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return record


@router.patch("/me", response_model=UserRead)
async def update_me(payload: UserUpdate, user: CurrentUserDep, session: DbSession) -> User:
    record = await session.get(User, uuid.UUID(user.id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    await session.flush()
    return record


@router.get("/me/profile", response_model=ProfileReadFull)
async def read_profile(user: CurrentUserDep, session: DbSession) -> Profile:
    """A user always sees her own full record, sensitive block included."""
    profile = await session.scalar(select(Profile).where(Profile.user_id == uuid.UUID(user.id)))
    if profile is None:
        profile = Profile(user_id=uuid.UUID(user.id))
        session.add(profile)
        await session.flush()
    return profile


@router.put("/me/profile", response_model=ProfileReadFull)
async def update_profile(
    payload: ProfileUpdate,
    user: CurrentUserDep,
    session: DbSession,
    request: Request,
) -> Profile:
    profile = await session.scalar(select(Profile).where(Profile.user_id == uuid.UUID(user.id)))
    if profile is None:
        profile = Profile(user_id=uuid.UUID(user.id))
        session.add(profile)

    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(profile, field, value)
    profile.completeness_percent = _completeness(profile)

    await record_audit(
        session,
        action="profile.update",
        entity_type="profile",
        entity_id=str(profile.id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.PERSONAL,
        changes={"fields": sorted(changes)},
        ip_address=client_ip(request),
    )
    await session.flush()

    if "skills" in changes:
        # The skills she lists are her own word about herself, mirrored into
        # the skill layer as exactly that. Removing one withdraws the claim and
        # nothing else: a course she finished stays on the record.
        await skills.sync_self_reported(session, uuid.UUID(user.id), profile.skills)

    # `updated_at` is filled by the database (`onupdate=func.now()`), so the
    # UPDATE leaves it expired on the instance. Serialising the response then
    # reaches for it outside the async context and the request dies with
    # MissingGreenlet — refreshing here fetches it while we still can.
    await session.refresh(profile)
    return profile


@router.get("/{user_id}/profile", response_model=ProfileRead)
async def read_other_profile(
    user_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> Profile:
    """Staff view. Returns the non-sensitive block only — the sensitive fields
    are simply not in `ProfileRead`, so they cannot leak through this route."""
    if not user.has_role(Role.ADMIN, Role.REGIONAL_COORDINATOR, Role.MENTOR):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile


@router.get("/me/goals", response_model=list[GoalRead])
async def list_goals(user: CurrentUserDep, session: DbSession) -> list[Goal]:
    rows = await session.execute(
        select(Goal).where(Goal.user_id == uuid.UUID(user.id)).order_by(Goal.created_at)
    )
    return list(rows.scalars())


@router.post("/me/goals", response_model=GoalRead, status_code=status.HTTP_201_CREATED)
async def create_goal(payload: GoalCreate, user: CurrentUserDep, session: DbSession) -> Goal:
    goal = Goal(user_id=uuid.UUID(user.id), **payload.model_dump())
    session.add(goal)
    await session.flush()
    return goal


@router.patch("/me/goals/{goal_id}", response_model=GoalRead)
async def update_goal(
    goal_id: uuid.UUID, payload: GoalUpdate, user: CurrentUserDep, session: DbSession
) -> Goal:
    goal = await session.get(Goal, goal_id)
    if goal is None or goal.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(goal, field, value)
    await session.flush()
    return goal


@router.delete("/me/goals/{goal_id}", response_model=Message)
async def delete_goal(goal_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> Message:
    goal = await session.get(Goal, goal_id)
    if goal is None or goal.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    await session.delete(goal)
    return Message(detail="Goal deleted")


@router.get("/me/consents", response_model=dict[str, bool])
async def read_consents(user: CurrentUserDep, session: DbSession) -> dict[str, bool]:
    """Which data-sharing scopes the user currently allows."""
    consents = await current_consents(session, uuid.UUID(user.id))
    return {scope.value: consents.get(scope, False) for scope in ConsentScope}


@router.post("/me/consents", response_model=ConsentRead)
async def update_consent(
    payload: ConsentUpdate,
    user: CurrentUserDep,
    session: DbSession,
    request: Request,
):
    """Grant or withdraw one scope. Appends a new record; nothing is rewritten."""
    try:
        scope = ConsentScope(payload.scope)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown consent scope '{payload.scope}'",
        ) from None
    if scope in SUBJECT_SCOPES:
        # Sharing with an organisation is given to that organisation — on its
        # listing or its invitation — and withdrawn there, never in general.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Consent scope '{scope.value}' is given per organisation",
        )

    entry = await record_consent(
        session,
        user_id=uuid.UUID(user.id),
        scope=scope,
        accepted=payload.accepted,
        policy_version=payload.policy_version,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await record_audit(
        session,
        action="consent.grant" if payload.accepted else "consent.withdraw",
        entity_type="consent",
        entity_id=scope.value,
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.PERSONAL,
        ip_address=client_ip(request),
    )
    await session.flush()
    return entry


@router.get("/me/activity", response_model=ActivitySummary)
async def my_activity(
    user: CurrentUserDep,
    session: DbSession,
    days: int = 365,
    year: int | None = None,
) -> ActivitySummary:
    """Her activity calendar, streak and points.

    Everything is derived from what she actually did, so there is nothing here
    to game and nothing extra to store.
    """
    result = await activity.summary(session, user.id, days=max(30, min(days, 366)), year=year)
    return ActivitySummary(
        days=[ActivityDay(**day) for day in result.days],
        total_actions=result.total_actions,
        active_days=result.active_days,
        points=result.points,
        current_streak=result.current_streak,
        best_streak=result.best_streak,
        from_date=result.from_date,
        to_date=result.to_date,
    )


@router.post("/me/onboarding/complete", response_model=UserRead)
async def complete_onboarding(user: CurrentUserDep, session: DbSession) -> User:
    record = await session.get(User, uuid.UUID(user.id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if record.onboarding_completed_at is None:
        record.onboarding_completed_at = datetime.now(UTC)
    await session.flush()
    return record


@router.delete("/me", response_model=Message)
async def delete_account(user: CurrentUserDep, session: DbSession, request: Request) -> Message:
    """Right to erasure: identifiers are cleared, activity history is kept
    anonymised so aggregate KPI figures stay correct."""
    record = await session.get(User, uuid.UUID(user.id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    from app.core.constants import UserStatus

    record.phone = None
    record.email = None
    record.password_hash = None
    record.status = UserStatus.DELETED
    record.anonymised_at = datetime.now(UTC)

    profile = await session.scalar(select(Profile).where(Profile.user_id == record.id))
    if profile is not None:
        profile.full_name = None
        profile.birth_date = None
        profile.bio = None
        profile.avatar_url = None

    await record_audit(
        session,
        action="user.anonymise",
        entity_type="user",
        entity_id=str(record.id),
        actor_id=record.id,
        actor_role=user.role.value,
        classification=DataClassification.PERSONAL,
        ip_address=client_ip(request),
    )
    return Message(detail="Account anonymised")
