"""Mentor directory and session booking."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUserDep, DbSession, StaffDep
from app.models.mentor import MentorProfile, MentorSession
from app.schemas.common import Message

router = APIRouter(prefix="/mentors", tags=["mentors"])


@router.get("", response_model=list[dict])
async def list_mentors(
    session: DbSession,
    user: CurrentUserDep,
    expertise: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Verified mentors only — an unvetted mentor is never surfaced."""
    stmt = select(MentorProfile).where(MentorProfile.is_verified.is_(True))
    if expertise:
        stmt = stmt.where(MentorProfile.expertise.any(expertise))

    rows = await session.execute(
        stmt.order_by(MentorProfile.rating_avg.desc().nullslast()).limit(min(limit, 100))
    )
    return [
        {
            "id": str(mentor.id),
            "headline": mentor.headline,
            "bio": mentor.bio,
            "expertise": mentor.expertise,
            "languages": mentor.languages,
            "years_of_experience": mentor.years_of_experience,
            "rating_avg": mentor.rating_avg,
            "sessions_count": mentor.sessions_count,
        }
        for mentor in rows.scalars()
    ]


@router.post("/{mentor_id}/request", response_model=dict, status_code=status.HTTP_201_CREATED)
async def request_session(
    mentor_id: uuid.UUID,
    user: CurrentUserDep,
    session: DbSession,
    topic: str | None = None,
    scheduled_at: datetime | None = None,
) -> dict:
    mentor = await session.get(MentorProfile, mentor_id)
    if mentor is None or not mentor.is_verified:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mentor not found")

    active = (
        await session.scalar(
            select(func.count(MentorSession.id)).where(
                MentorSession.mentor_id == mentor_id,
                MentorSession.status.in_(("requested", "confirmed")),
            )
        )
        or 0
    )
    if active >= mentor.max_mentees:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This mentor has no free slots right now",
        )

    booking = MentorSession(
        mentor_id=mentor_id,
        user_id=uuid.UUID(user.id),
        topic=topic,
        scheduled_at=scheduled_at,
        status="requested",
    )
    session.add(booking)
    await session.flush()
    return {"id": str(booking.id), "status": booking.status}


@router.get("/me/sessions", response_model=list[dict])
async def my_sessions(user: CurrentUserDep, session: DbSession) -> list[dict]:
    rows = await session.execute(
        select(MentorSession)
        .where(MentorSession.user_id == uuid.UUID(user.id))
        .order_by(MentorSession.created_at.desc())
    )
    return [
        {
            "id": str(booking.id),
            "mentor_id": str(booking.mentor_id),
            "topic": booking.topic,
            "status": booking.status,
            "scheduled_at": booking.scheduled_at.isoformat() if booking.scheduled_at else None,
            "rating": booking.rating,
        }
        for booking in rows.scalars()
    ]


@router.post("/sessions/{session_id}/feedback", response_model=Message)
async def leave_feedback(
    session_id: uuid.UUID,
    user: CurrentUserDep,
    session: DbSession,
    rating: int,
    feedback: str | None = None,
) -> Message:
    if not 1 <= rating <= 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rating must be between 1 and 5",
        )

    booking = await session.get(MentorSession, session_id)
    if booking is None or booking.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    booking.rating = rating
    booking.feedback = feedback
    booking.status = "completed"

    mentor = await session.get(MentorProfile, booking.mentor_id)
    if mentor is not None:
        average = await session.scalar(
            select(func.avg(MentorSession.rating)).where(
                MentorSession.mentor_id == mentor.id, MentorSession.rating.is_not(None)
            )
        )
        mentor.rating_avg = round(float(average), 2) if average else None
        mentor.sessions_count = (
            await session.scalar(
                select(func.count(MentorSession.id)).where(
                    MentorSession.mentor_id == mentor.id, MentorSession.status == "completed"
                )
            )
            or 0
        )

    return Message(detail="Thank you for your feedback")


@router.post("/{mentor_id}/verify", response_model=Message)
async def verify_mentor(mentor_id: uuid.UUID, user: StaffDep, session: DbSession) -> Message:
    mentor = await session.get(MentorProfile, mentor_id)
    if mentor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mentor not found")
    mentor.is_verified = True
    mentor.verified_at = datetime.now(UTC)
    return Message(detail="Mentor verified")
