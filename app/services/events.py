"""Events: workshops, seminars, conferences, forums, trainings, consultations,
competitions and networking — each one a real listing with a time.

An event is an `Opportunity` whose `starts_at` is set. Nothing about it is kept
twice: the organiser, the topics (the skills column), the age rule, saving and
registering are the listing's own, and they are read by the same code the jobs
catalogue uses (`services.opportunities`, `services.eligibility`). What this
module adds is what only an event has:

* **When and where** — the catalogue is read by date, and an event that has
  ended is not listed. Registration closes when it begins (`eligibility`).
* **Why it is worth her time** — not "you have 2 of 3 skills it asks for": an
  event does not ask for skills, it covers them. So it is explained by what it
  covers against her own records — the direction she chose, a course she is
  taking, a skill she holds, an interest she picked, a Development Score area
  still growing, a business she runs. Her region is mentioned but never enough
  on its own. No record, no reason, no recommendation.
* **A reminder she asked for** — an in-app notification scheduled for the day
  before, delivered by the worker that already delivers notifications. Email,
  SMS and push are not offered: their adapters are logging stand-ins, and a
  reminder that is never sent is worse than none.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    BUSINESS_OPPORTUNITY_TYPES,
    ApplicationStatus,
    DimensionBand,
    EnrollmentStatus,
    EventFormat,
    NotificationChannel,
    NotificationTrigger,
    OpportunityType,
    ScoreDimension,
)
from app.models.notification import Notification
from app.models.opportunity import Opportunity
from app.models.organization import Organization
from app.models.profile import Profile
from app.models.user import User
from app.schemas.event import (
    EventCard,
    EventCatalogue,
    EventDetail,
    EventFacets,
    EventReason,
    ReminderRead,
)
from app.schemas.opportunity import SkillFacet
from app.services import eligibility
from app.services import opportunities as listings
from app.services import skills as skill_service
from app.services.age_gate import age_from_profile
from app.services.score_insights import band_for

CATALOGUE_LIMIT = 500
MAX_RECOMMENDED = 3
MAX_TOPIC_FACETS = 12
#: A reminder is due the day before; an event less than a day away gets one an
#: hour before instead; one less than an hour away gets none.
REMIND_BEFORE = timedelta(hours=24)
REMIND_LATE = timedelta(hours=1)
#: Uzbekistan keeps one offset all year, so a fixed zone needs no tz database.
TASHKENT = timezone(timedelta(hours=5))

#: How much each kind of reason says about her, strongest first.
REASON_WEIGHT = {
    "career": 5,
    "learning": 4,
    "score": 3,
    "own_business": 3,
    "interest": 2,
    "skill": 1,
    "region": 0,
}
_OPEN_ENROLLMENT = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)
_LIVE_APPLICATION = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.IN_REVIEW,
    ApplicationStatus.ACCEPTED,
)


class EventError(Exception):
    """A request about an event was refused. `reason` is a key the page words."""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def ends(item: Opportunity) -> datetime:
    return item.ends_at or item.starts_at


def happening(item: Opportunity, now: datetime) -> bool:
    return item.starts_at is not None and item.starts_at <= now <= ends(item)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


async def _upcoming(
    session: AsyncSession, now: datetime
) -> tuple[list[Opportunity], dict[uuid.UUID, Organization]]:
    """Every event not yet over, soonest first — only while its organiser, if
    it has one on WomanUP, is active."""
    rows = list(
        (
            await session.execute(
                select(Opportunity)
                .where(
                    Opportunity.is_active.is_(True),
                    Opportunity.starts_at.is_not(None),
                    func.coalesce(Opportunity.ends_at, Opportunity.starts_at) >= now,
                )
                .order_by(Opportunity.starts_at.asc())
                .limit(CATALOGUE_LIMIT)
            )
        ).scalars()
    )
    orgs = await listings._orgs(session, rows)
    return [row for row in rows if listings._published_by_active(row, orgs)], orgs


def _reminder_filter(user_id: uuid.UUID, now: datetime):
    return (
        Notification.user_id == user_id,
        Notification.trigger == NotificationTrigger.EVENT_REMINDER,
        Notification.channel == NotificationChannel.IN_APP,
        Notification.sent_at.is_(None),
        Notification.scheduled_for > now,
    )


async def _reminders(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> dict[uuid.UUID, datetime]:
    """Her reminders still to come, by event."""
    rows = await session.execute(
        select(Notification.payload, Notification.scheduled_for).where(
            *_reminder_filter(user_id, now)
        )
    )
    out: dict[uuid.UUID, datetime] = {}
    for payload, due in rows.all():
        try:
            out[uuid.UUID(str((payload or {}).get("opportunity_id")))] = due
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Why an event is worth her time
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Signals:
    """What an event is read against, beyond the engine's own context."""

    own_business: bool = False
    interest_keys: set[str] = field(default_factory=set)
    #: skill key -> the title of the course she is taking that teaches it.
    course_keys: dict[str, dict] = field(default_factory=dict)


async def signals_for(session: AsyncSession, reader: listings.Reader) -> Signals:
    ctx = reader.ctx
    profile = await session.scalar(select(Profile).where(Profile.user_id == ctx.user_id))
    status = skill_service.normalise(profile.employment_status or "") if profile else ""
    interests = [label for label in (profile.interests if profile else []) if label]
    await listings._widen(session, ctx.index, interests)

    courses: dict[str, dict] = {}
    for enrollment in ctx.enrollments.values():
        if enrollment.status not in _OPEN_ENROLLMENT:
            continue
        program = ctx.programs.get(enrollment.program_id)
        if program is None:
            continue
        for label in program.skills_taught or []:
            courses.setdefault(ctx.index.key(label), program.title_i18n)
    return Signals(
        own_business=bool(status) and status in listings.OWN_BUSINESS,
        interest_keys={ctx.index.key(label) for label in interests},
        course_keys=courses,
    )


def _dimensions(skill) -> list[ScoreDimension]:
    out = []
    for value in getattr(skill, "dimensions", None) or []:
        try:
            out.append(ScoreDimension(value))
        except ValueError:
            continue
    return out


def reasons_for(
    reader: listings.Reader,
    signals: Signals,
    index: skill_service.SkillIndex,
    item: Opportunity,
) -> list[EventReason]:
    """Every true reason, each named by the record behind it. One of each kind
    at most: three reasons read; eight do not."""
    ctx = reader.ctx
    topics = [(index.key(label), label) for label in item.required_skills or [] if label.strip()]
    out: list[EventReason] = []

    def first(test) -> tuple[str, str] | None:
        return next(((key, label) for key, label in topics if test(key)), None)

    if reader.career is not None:
        hit = first(lambda key: key in reader.career_keys)
        if hit:
            out.append(
                EventReason(
                    kind="career",
                    skill=index.ref(hit[1]),
                    career_slug=reader.career.slug,
                    career_title_i18n=reader.career.title_i18n,
                )
            )
    hit = first(lambda key: key in signals.course_keys)
    if hit:
        out.append(
            EventReason(
                kind="learning",
                skill=index.ref(hit[1]),
                program_title_i18n=signals.course_keys[hit[0]],
            )
        )
    hit = first(lambda key: key in ctx.held)
    if hit:
        out.append(EventReason(kind="skill", skill=index.ref(hit[1])))
    hit = first(lambda key: key in signals.interest_keys)
    if hit:
        out.append(EventReason(kind="interest", skill=index.ref(hit[1])))

    entrepreneurial = False
    for _, label in topics:
        dimensions = _dimensions(index.get(label))
        entrepreneurial = entrepreneurial or ScoreDimension.ENTREPRENEURSHIP in dimensions
        if any(reason.kind == "score" for reason in out):
            continue
        for dimension in dimensions:
            score = ctx.scores.get(dimension)
            if score is not None and band_for(score.current) != DimensionBand.STRONG:
                out.append(
                    EventReason(
                        kind="score",
                        skill=index.ref(label),
                        dimension=dimension.value,
                        score=round(score.current),
                    )
                )
                break

    if signals.own_business and (item.type in BUSINESS_OPPORTUNITY_TYPES or entrepreneurial):
        out.append(EventReason(kind="own_business"))
    if ctx.region and item.format != EventFormat.ONLINE and item.region == ctx.region:
        out.append(EventReason(kind="region"))
    return out


def weight(reasons: list[EventReason]) -> int:
    return sum(REASON_WEIGHT.get(reason.kind, 0) for reason in reasons)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _card(
    item: Opportunity,
    index: skill_service.SkillIndex,
    reader: listings.Reader | None,
    *,
    now: datetime,
    org: Organization | None,
    reasons: list[EventReason],
    reminders: dict[uuid.UUID, datetime],
) -> EventCard:
    base = listings._card(item, index, reader, now=now, org=org)
    low, high = eligibility.age_limits(item)
    return EventCard(
        **base.model_dump(exclude={"fit"}),
        age_min=low,
        age_max=high,
        happening=happening(item, now),
        reminder_at=reminders.get(item.id),
        reasons=reasons,
    )


@dataclass(slots=True)
class Filters:
    type: OpportunityType | None = None
    format: EventFormat | None = None
    region: str | None = None
    topic: str | None = None
    #: Only the events she saved, registered for or asked to be reminded of.
    mine: bool = False
    #: Only events that overlap this window — a month of the calendar.
    window_from: datetime | None = None
    window_to: datetime | None = None
    size: int = 50


def _keep(
    item: Opportunity,
    f: Filters,
    index: skill_service.SkillIndex,
    mine: set[uuid.UUID],
    skip: str | None = None,
) -> bool:
    if f.type and skip != "type" and item.type != f.type:
        return False
    if f.format and skip != "format" and item.format != f.format:
        return False
    if f.region and skip != "region" and item.region != f.region:
        return False
    if f.topic and skip != "topic":
        wanted = index.key(f.topic)
        if wanted not in {index.key(label) for label in item.required_skills or []}:
            return False
    if f.mine and item.id not in mine:
        return False
    if f.window_from and ends(item) < f.window_from:
        return False
    return not (f.window_to and item.starts_at >= f.window_to)


def _mine(reader: listings.Reader | None, reminders: dict[uuid.UUID, datetime]) -> set[uuid.UUID]:
    if reader is None:
        return set()
    registered = {
        key for key, row in reader.applications.items() if row.status in _LIVE_APPLICATION
    }
    return set(reader.saved) | registered | set(reminders)


@dataclass(slots=True)
class _Her:
    """Her side, read once per request: nothing for a visitor."""

    reader: listings.Reader | None
    index: skill_service.SkillIndex
    signals: Signals
    reminders: dict[uuid.UUID, datetime]

    def reasons(self, item: Opportunity) -> list[EventReason]:
        if self.reader is None:
            return []
        return reasons_for(self.reader, self.signals, self.index, item)


async def _her(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    labels: list[str],
    now: datetime,
    reader: listings.Reader | None = None,
) -> _Her:
    if user_id is None:
        return _Her(None, await skill_service.SkillIndex.load(session, labels), Signals(), {})
    reader = reader or await listings.reader_for(session, user_id)
    await listings._widen(session, reader.ctx.index, labels)
    return _Her(
        reader,
        reader.ctx.index,
        await signals_for(session, reader),
        await _reminders(session, user_id, now),
    )


def _cards(
    rows: list[Opportunity], her: _Her, orgs: dict[uuid.UUID, Organization], now: datetime
) -> list[EventCard]:
    return [
        _card(
            item,
            her.index,
            her.reader,
            now=now,
            org=orgs.get(item.organization_id),
            reasons=her.reasons(item),
            reminders=her.reminders,
        )
        for item in rows
    ]


async def catalogue(session: AsyncSession, user_id: uuid.UUID | None, f: Filters) -> EventCatalogue:
    """Every event not yet over that the filters keep, soonest first.

    Open to visitors. Signed in, each card also says why it is worth her time,
    whether she may register, and whether she saved it, registered or asked for
    a reminder. Facets are counted with every other filter applied, so no
    option is offered that would return nothing.
    """
    now = datetime.now(UTC)
    rows, orgs = await _upcoming(session, now)
    labels = [label for item in rows for label in item.required_skills or []]
    if f.topic:
        labels.append(f.topic)
    her = await _her(session, user_id, labels, now)
    index = her.index
    mine = _mine(her.reader, her.reminders)

    kept = [item for item in rows if _keep(item, f, index, mine)]
    facets = EventFacets(
        types=dict(Counter(i.type.value for i in rows if _keep(i, f, index, mine, "type"))),
        formats=dict(
            Counter(i.format.value for i in rows if i.format and _keep(i, f, index, mine, "format"))
        ),
        regions=dict(
            Counter(i.region for i in rows if i.region and _keep(i, f, index, mine, "region"))
        ),
        topics=_topic_facets(rows, f, index, mine),
    )
    return EventCatalogue(
        items=_cards(kept[: f.size], her, orgs, now),
        total=len(kept),
        facets=facets,
        signed_in=her.reader is not None,
    )


def _topic_facets(
    rows: list[Opportunity], f: Filters, index: skill_service.SkillIndex, mine: set[uuid.UUID]
) -> list[SkillFacet]:
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for item in rows:
        if not _keep(item, f, index, mine, "topic"):
            continue
        for key, label in {index.key(label): label for label in item.required_skills or []}.items():
            counts[key] += 1
            labels.setdefault(key, label)
    return [
        SkillFacet(skill=index.ref(labels[key]), count=count)
        for key, count in counts.most_common(MAX_TOPIC_FACETS)
    ]


async def her_events(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int = MAX_RECOMMENDED,
    *,
    reader: listings.Reader | None = None,
) -> tuple[list[EventCard], list[EventCard]]:
    """What is for her, read once: up to `limit` events worth her time, and
    the events ahead she saved, registered for or set a reminder on.

    Recommended events are the engine's own set (`LearnerContext.events`) —
    open for registration and not excluded by the platform's one age rule —
    each with at least one reason other than her region. Never padded.
    """
    now = datetime.now(UTC)
    rows, orgs = await _upcoming(session, now)
    labels = [label for item in rows for label in item.required_skills or []]
    her = await _her(session, user_id, labels, now, reader)
    reader = her.reader
    if reader is None:
        return [], []
    events_orgs = {**orgs, **await listings._orgs(session, reader.ctx.events)}

    ranked: list[tuple[tuple, Opportunity]] = []
    for item in reader.ctx.events:
        if not listings._published_by_active(item, events_orgs):
            continue
        reasons = her.reasons(item)
        if [reason for reason in reasons if reason.kind != "region"]:
            ranked.append(((-weight(reasons), item.starts_at), item))
    ranked.sort(key=lambda row: row[0])
    recommended = _cards([item for _, item in ranked[:limit]], her, events_orgs, now)

    mine = _mine(reader, her.reminders)
    own = _cards([item for item in rows if item.id in mine], her, orgs, now)
    return recommended, own


async def recommended(
    session: AsyncSession, user_id: uuid.UUID, limit: int = MAX_RECOMMENDED
) -> list[EventCard]:
    return (await her_events(session, user_id, limit))[0]


async def mine(session: AsyncSession, user_id: uuid.UUID) -> list[EventCard]:
    """The events ahead that she saved, registered for or asked to be reminded
    of, soonest first."""
    return (await her_events(session, user_id))[1]


async def detail(
    session: AsyncSession, item: Opportunity, user_id: uuid.UUID | None
) -> EventDetail:
    """One event: what it is, why it may be useful to her, when, where, for
    whom, what she needs, and how she registers."""
    now = datetime.now(UTC)
    her = await _her(session, user_id, list(item.required_skills or []), now)
    org = await session.get(Organization, item.organization_id) if item.organization_id else None
    [card] = _cards([item], her, {org.id: org} if org else {}, now)
    application = her.reader.applications.get(item.id) if her.reader else None
    return EventDetail(
        **card.model_dump(),
        sharing=await listings._sharing(session, item, user_id),
        application=listings.application_read(application, item, now=now) if application else None,
        other_requirements=eligibility.unreadable(item),
        registration="external" if item.external_url else "platform",
    )


async def visible(session: AsyncSession, event_id: uuid.UUID) -> Opportunity | None:
    """An event anyone may open — the listing rule, and it must be an event."""
    item = await listings.visible(session, event_id)
    return item if item is not None and item.starts_at is not None else None


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

_MONTHS = {
    "uz": (
        "yanvar", "fevral", "mart", "aprel", "may", "iyun",
        "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
    ),
    "ru": (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ),
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
}  # fmt: skip
_WORDS = {
    "uz": {"day": "Ertaga: {title}", "hour": "Bir soatdan keyin: {title}", "online": "Onlayn"},
    "ru": {"day": "Завтра: {title}", "hour": "Через час: {title}", "online": "Онлайн"},
    "en": {"day": "Tomorrow: {title}", "hour": "In an hour: {title}", "online": "Online"},
}


def remind_at(item: Opportunity, now: datetime) -> datetime | None:
    if item.starts_at is None:
        return None
    for before in (REMIND_BEFORE, REMIND_LATE):
        due = item.starts_at - before
        if due > now:
            return due
    return None


def _when(moment: datetime, language: str) -> str:
    local = moment.astimezone(TASHKENT)
    month = _MONTHS[language][local.month - 1]
    day = {"uz": f"{local.day}-{month}", "ru": f"{local.day} {month}", "en": f"{local.day} {month}"}
    return f"{day[language]}, {local:%H:%M}"


def reminder_text(item: Opportunity, due: datetime, language: str) -> tuple[str, str]:
    """The words of her reminder, in her language, from the event itself."""
    language = language if language in _WORDS else "uz"
    words = _WORDS[language]
    titles = item.title_i18n or {}
    title = next(
        (titles[key] for key in (language, "uz", "ru", "en") if titles.get(key)), ""
    ).strip()
    lead = words["day"] if item.starts_at - due >= REMIND_BEFORE else words["hour"]
    place = words["online"] if item.format == EventFormat.ONLINE else (item.venue or "").strip()
    body = _when(item.starts_at, language) + (f" — {place}" if place else "")
    return lead.format(title=title)[:255], body


async def _drop(session: AsyncSession, user_id: uuid.UUID, event_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    await session.execute(
        delete(Notification).where(
            *_reminder_filter(user_id, now),
            Notification.payload["opportunity_id"].astext == str(event_id),
        )
    )


async def set_reminder(
    session: AsyncSession, *, user_id: uuid.UUID, item: Opportunity
) -> ReminderRead:
    """Remind her the day before — or an hour before, when it is sooner. One
    reminder per event: asking again replaces it."""
    now = datetime.now(UTC)
    due = remind_at(item, now)
    if due is None:
        raise EventError("too_soon", 409)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    verdict = eligibility.check(item, age=age_from_profile(profile), now=now)
    if verdict.reason in ("too_young", "too_old", "adults_only"):
        raise EventError("not_eligible", 403)

    user = await session.get(User, user_id)
    language = getattr(getattr(user, "language", None), "value", "uz")
    title, body = reminder_text(item, due, language)
    await _drop(session, user_id, item.id)
    session.add(
        Notification(
            user_id=user_id,
            channel=NotificationChannel.IN_APP,
            trigger=NotificationTrigger.EVENT_REMINDER,
            title=title,
            body=body,
            action_url=f"/tadbirlar/{item.id}",
            payload={"opportunity_id": str(item.id)},
            scheduled_for=due,
        )
    )
    await session.flush()
    return ReminderRead(event_id=item.id, remind_at=due)


async def cancel_reminder(
    session: AsyncSession, *, user_id: uuid.UUID, event_id: uuid.UUID
) -> None:
    await _drop(session, user_id, event_id)
    await session.flush()


async def _pending_for(session: AsyncSession, event_ids: list[uuid.UUID]) -> list[Notification]:
    now = datetime.now(UTC)
    if not event_ids:
        return []
    return list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.trigger == NotificationTrigger.EVENT_REMINDER,
                    Notification.sent_at.is_(None),
                    Notification.scheduled_for > now,
                    Notification.payload["opportunity_id"].astext.in_(
                        [str(event_id) for event_id in event_ids]
                    ),
                )
            )
        ).scalars()
    )


async def reschedule(session: AsyncSession, item: Opportunity) -> None:
    """The organiser moved the event: every reminder still to come moves with
    it, in the words of the new time — or goes, if the new time is too close."""
    now = datetime.now(UTC)
    for note in await _pending_for(session, [item.id]):
        due = remind_at(item, now)
        if due is None:
            await session.delete(note)
            continue
        user = await session.get(User, note.user_id)
        language = getattr(getattr(user, "language", None), "value", "uz")
        note.title, note.body = reminder_text(item, due, language)
        note.scheduled_for = due
    await session.flush()


async def drop_reminders(session: AsyncSession, event_ids: list[uuid.UUID]) -> int:
    """The events were taken down: no reminder may point at them."""
    notes = await _pending_for(session, event_ids)
    for note in notes:
        await session.delete(note)
    await session.flush()
    return len(notes)
