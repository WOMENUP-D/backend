"""In-app notifications and channel preferences."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, or_, select, update

from app.api.deps import CurrentUserDep, DbSession
from app.core.constants import NotificationChannel
from app.models.notification import Notification, NotificationPreference
from app.schemas.admin import (
    NotificationPreferenceUpdate,
    NotificationRead,
)
from app.schemas.common import Message

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _hers_and_due(user_id: str):
    """Her in-app notifications that are due. A reminder scheduled for
    tomorrow is not shown today — it would say "tomorrow" a day early."""
    return (
        Notification.user_id == uuid.UUID(user_id),
        Notification.channel == NotificationChannel.IN_APP,
        or_(
            Notification.scheduled_for.is_(None),
            Notification.scheduled_for <= datetime.now(UTC),
        ),
    )


@router.get("", response_model=list[NotificationRead])
async def list_notifications(
    user: CurrentUserDep, session: DbSession, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    stmt = select(Notification).where(*_hers_and_due(user.id))
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    rows = await session.execute(
        stmt.order_by(Notification.created_at.desc()).limit(min(limit, 200))
    )
    return list(rows.scalars())


@router.get("/unread-count", response_model=dict[str, int])
async def unread_count(user: CurrentUserDep, session: DbSession) -> dict[str, int]:
    count = await session.scalar(
        select(func.count(Notification.id)).where(
            *_hers_and_due(user.id),
            Notification.read_at.is_(None),
        )
    )
    return {"unread": count or 0}


@router.post("/{notification_id}/read", response_model=NotificationRead)
async def mark_read(
    notification_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> Notification:
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
    await session.flush()
    return notification


@router.post("/read-all", response_model=Message)
async def mark_all_read(user: CurrentUserDep, session: DbSession) -> Message:
    await session.execute(
        update(Notification)
        .where(
            *_hers_and_due(user.id),
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    return Message(detail="All notifications marked as read")


@router.get("/preferences", response_model=NotificationPreferenceUpdate)
async def read_preferences(user: CurrentUserDep, session: DbSession) -> NotificationPreference:
    prefs = await session.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == uuid.UUID(user.id))
    )
    if prefs is None:
        prefs = NotificationPreference(user_id=uuid.UUID(user.id))
        session.add(prefs)
        await session.flush()
    return prefs


@router.put("/preferences", response_model=NotificationPreferenceUpdate)
async def update_preferences(
    payload: NotificationPreferenceUpdate, user: CurrentUserDep, session: DbSession
) -> NotificationPreference:
    """Channels are opt-in and freely revocable — no dark patterns (section 10)."""
    prefs = await session.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == uuid.UUID(user.id))
    )
    if prefs is None:
        prefs = NotificationPreference(user_id=uuid.UUID(user.id))
        session.add(prefs)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(prefs, field, value)
    await session.flush()
    return prefs
