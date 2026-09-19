"""Her week: one of each kind of next thing, each a real record or nothing."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.constants import OpportunityType
from app.schemas.event import EventCard
from app.schemas.recommendation import TaskSuggestion


class WeekLesson(BaseModel):
    """The course she touched last, and the first lesson she has not ticked."""

    program_id: uuid.UUID
    program_slug: str
    program_title_i18n: dict
    lesson_title_i18n: dict = {}
    progress: int = 0


class WeekOpportunity(BaseModel):
    id: uuid.UUID
    type: OpportunityType
    title_i18n: dict
    organisation: str | None = None
    deadline: datetime | None = None
    #: `saved` — one she kept; `suggested` — the engine's best match for her.
    why: str


class WeekRead(BaseModel):
    lesson: WeekLesson | None = None
    event: EventCard | None = None
    #: `yours` — she saved it, registered or set a reminder; `suggested`.
    event_why: str | None = None
    task: TaskSuggestion | None = None
    opportunity: WeekOpportunity | None = None
