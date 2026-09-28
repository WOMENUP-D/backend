"""Events: the catalogue by date, one event, what is for her, and reminders.

Saving and registering are the listing's own endpoints
(`PUT /opportunities/{id}/save`, `POST /opportunities/{id}/apply`): an event
is a listing, and registering shares exactly what applying shares, under the
same consent rules.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUserDep, DbSession, OptionalUserDep
from app.core.constants import EventFormat, OpportunityType
from app.schemas.event import EventCard, EventCatalogue, EventDetail, ReminderRead
from app.services import events

router = APIRouter(prefix="/events", tags=["events"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")


@router.get("", response_model=EventCatalogue)
async def catalogue(
    session: DbSession,
    user: OptionalUserDep,
    type: OpportunityType | None = None,
    format: EventFormat | None = None,
    region: str | None = Query(default=None, max_length=60),
    topic: str | None = Query(default=None, max_length=100),
    mine: bool = False,
    window_from: datetime | None = Query(default=None, alias="from"),
    window_to: datetime | None = Query(default=None, alias="to"),
    size: int = Query(default=50, ge=1, le=200),
) -> EventCatalogue:
    """Events not yet over, soonest first. Open to visitors; signed in, each
    also says why it is worth her time and where she stands with it.

    `from`/`to` keep the events that overlap a window — a calendar month.
    `mine` keeps the ones she saved, registered for or asked to be reminded of.
    """
    if mine and user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in")
    return await events.catalogue(
        session,
        uuid.UUID(user.id) if user else None,
        events.Filters(
            type=type,
            format=format,
            region=region,
            topic=topic,
            mine=mine,
            window_from=window_from,
            window_to=window_to,
            size=size,
        ),
    )


@router.get("/recommended", response_model=list[EventCard])
async def recommended(user: CurrentUserDep, session: DbSession) -> list[EventCard]:
    """Up to three events worth her time, each with the records that say so.
    Empty when none has a reason — never padded."""
    return await events.recommended(session, uuid.UUID(user.id))


@router.get("/me", response_model=list[EventCard])
async def mine(user: CurrentUserDep, session: DbSession) -> list[EventCard]:
    """The events ahead that she saved, registered for or set a reminder on."""
    return await events.mine(session, uuid.UUID(user.id))


@router.get("/{event_id}", response_model=EventDetail)
async def read_event(event_id: uuid.UUID, session: DbSession, user: OptionalUserDep) -> EventDetail:
    """One event: what, why for her, when, where, for whom, what she needs,
    and how to register."""
    item = await events.visible(session, event_id)
    if item is None:
        raise _not_found()
    return await events.detail(session, item, uuid.UUID(user.id) if user else None)


@router.put("/{event_id}/reminder", response_model=ReminderRead)
async def set_reminder(
    event_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> ReminderRead:
    """Remind her in WomanUP the day before (an hour before, when it is sooner).
    In-app only: no other channel can deliver yet."""
    item = await events.visible(session, event_id)
    if item is None:
        raise _not_found()
    try:
        return await events.set_reminder(session, user_id=uuid.UUID(user.id), item=item)
    except events.EventError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"reason": exc.reason}) from exc


@router.delete("/{event_id}/reminder", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reminder(event_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> None:
    await events.cancel_reminder(session, user_id=uuid.UUID(user.id), event_id=event_id)
