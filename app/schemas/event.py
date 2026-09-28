"""Events as she reads them: what, when, where, for whom — and why for her.

An event is a listing with a time (`opportunities.starts_at`), so an
`EventCard` is an `OpportunityCard` with the few things only an event has.
Its `fit` is left empty: an event does not ask for skills, it covers them, so
it is explained by `reasons` instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.opportunity import (
    ApplicationRead,
    OpportunityCard,
    SharingRead,
    SkillFacet,
)
from app.schemas.skill import SkillRef


class EventReason(BaseModel):
    """One true thing that makes an event worth her time.

    * `career` — it covers a skill on the direction she chose.
    * `learning` — it covers a skill a course she is taking teaches.
    * `skill` — it goes deeper into a skill she already has.
    * `interest` — it is about an interest she chose.
    * `score` — it covers an area where her Development Score is still growing.
    * `own_business` — her profile says she runs a business and it is for that.
    * `region` — it is held in her region. Never a reason on its own.
    """

    kind: str
    skill: SkillRef | None = None
    career_slug: str | None = None
    career_title_i18n: dict = {}
    program_title_i18n: dict = {}
    dimension: str | None = None
    score: int | None = None


class EventCard(OpportunityCard):
    #: The age range the organiser states, for "Who is it for" — shown to
    #: visitors too. Neither set means adults (the platform's one age rule).
    age_min: int | None = None
    age_max: int | None = None
    #: Started and not yet over — for a multi-day event.
    happening: bool = False
    #: When her reminder is due, if she asked for one.
    reminder_at: datetime | None = None
    #: Signed in only; empty for a visitor.
    reasons: list[EventReason] = []


class EventFacets(BaseModel):
    """What the filters can offer, counted over the events that exist."""

    types: dict[str, int] = {}
    formats: dict[str, int] = {}
    regions: dict[str, int] = {}
    topics: list[SkillFacet] = []


class EventCatalogue(BaseModel):
    items: list[EventCard]
    total: int
    facets: EventFacets = EventFacets()
    signed_in: bool = False


class EventDetail(EventCard):
    sharing: SharingRead = SharingRead()
    application: ApplicationRead | None = None
    other_requirements: list[str] = []
    #: How she registers: on the organiser's own site, or here.
    registration: str = "platform"


class ReminderRead(BaseModel):
    event_id: uuid.UUID
    remind_at: datetime
