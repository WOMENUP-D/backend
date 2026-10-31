"""Recording what happens: events, presence, and what a browser may tell us.

The write side of product analytics. `traffic.py` counts visits; `insights.py`
answers the administrator's questions; this module is the only place an
`analytics_events` or `visitor_presence` row is written, and the only place a
User-Agent is looked at.

Rules that hold for every row written here:

* Nothing from the browser is stored as free text. Event names come from a
  fixed list, paths are folded to a known route, properties are a handful of
  short scalars with allow-listed keys, and the User-Agent is reduced to three
  words and dropped.
* A business event is written beside the record it describes, in the same
  transaction, and carries a `dedupe_key` so a retried request lands on the
  unique index instead of counting twice. The INSERT is `ON CONFLICT DO
  NOTHING`, so the collision never fails the real request.
* Recording must never break the request it rides on. Server events are
  written through `record_event`, which swallows its own database errors and
  logs them; the registration, the enrollment, the diagnostic still succeed.
* Staff browsing is marked, not dropped, so the panel's own traffic can be
  excluded from every figure without ever being lost.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import Role
from app.models.analytics import AnalyticsEvent, VisitorPresence
from app.schemas.auth import CurrentUser
from app.services.traffic import hash_visitor, normalise_path

logger = logging.getLogger(__name__)

VISITOR_COOKIE = "wu_vid"
SESSION_HEADER = "x-wu-session"

#: Roles whose own browsing is the platform's, not a participant's.
STAFF_ROLES = frozenset(role for role in Role if role not in (Role.USER, Role.MOTHER))

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Events the server writes beside the record they describe.
SERVER_EVENTS: frozenset[str] = frozenset(
    {
        "registration_completed",
        "login_completed",
        "onboarding_completed",
        "development_test_completed",
        "program_enrolled",
        "lesson_completed",
        "program_completed",
        "plan_generated",
        "plan_accepted",
        "cv_updated",
        "ai_assistant_used",
        "api_error",
    }
)

#: Events a browser may report. Evidence of intent, never of outcome: nothing
#: on the dashboard counts an outcome from one of these.
CLIENT_EVENTS: frozenset[str] = frozenset(
    {
        "registration_started",
        "development_test_started",
        "program_viewed",
        "catalogue_search",
        "client_error",
    }
)

EVENT_NAMES = SERVER_EVENTS | CLIENT_EVENTS

#: Property keys a browser may send, and the only ones kept from it.
CLIENT_PROPERTY_KEYS: frozenset[str] = frozenset(
    {"resumed", "results", "category", "name", "message", "section", "slug"}
)
MAX_PROPERTIES = 8
MAX_PROPERTY_TEXT = 120
MAX_ENTITY_ID = 64
MAX_ENTITY_TYPE = 30

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
_ENTITY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS_RE = re.compile(r"\d{6,}")


class EventRejected(ValueError):
    """A browser sent something the pipeline refuses to store."""


# ---------------------------------------------------------------------------
# Reducing what the browser tells us
# ---------------------------------------------------------------------------


def normalise_session(raw: str | None) -> str | None:
    """A browser session id, or None for anything that is not one."""
    if raw and _SESSION_RE.match(raw):
        return raw
    return None


def referrer_host(raw: str | None, *, own_hosts: frozenset[str] = frozenset()) -> str | None:
    """The host a visit came from — never the page, never the query.

    Own hosts are dropped: a click from one WomanUP page to another is
    navigation, not a source. "www." is folded so one site is one line.
    """
    if not raw:
        return None
    try:
        host = (urlsplit(raw.strip()[:500]).hostname or "").lower()
    except ValueError:
        return None
    host = host.removeprefix("www.")
    if not host or host in own_hosts or host in {"localhost", "127.0.0.1"}:
        return None
    return host[:120]


@dataclass(frozen=True, slots=True)
class Client:
    device: str | None
    browser: str | None
    os: str | None


_BOT = re.compile(r"bot|crawl|spider|slurp|preview|fetch|monitor|headless|lighthouse", re.I)


def classify_user_agent(user_agent: str | None) -> Client:
    """Three words out of a User-Agent, then the header is forgotten.

    Families only — "Chrome", not "Chrome 131.0.6778.86"; "Android", not the
    handset. Coarse on purpose: this is for "most of our visitors are on
    phones", not for recognising anybody.
    """
    if not user_agent:
        return Client(None, None, None)
    ua = user_agent[:600]
    low = ua.lower()

    if _BOT.search(low):
        return Client("bot", "bot", None)

    if (
        "ipad" in low
        or ("tablet" in low and "android" in low)
        or ("android" in low and "mobile" not in low)
    ):
        device = "tablet"
    elif "mobi" in low or "iphone" in low or "ipod" in low or "android" in low:
        device = "mobile"
    else:
        device = "desktop"

    if "iphone" in low or "ipad" in low or "ipod" in low:
        os = "iOS"
    elif "android" in low:
        os = "Android"
    elif "windows" in low:
        os = "Windows"
    elif "mac os" in low or "macintosh" in low:
        os = "macOS"
    elif "cros" in low:
        os = "ChromeOS"
    elif "linux" in low:
        os = "Linux"
    else:
        os = "Other"

    if "edg/" in low or "edge/" in low:
        browser = "Edge"
    elif "opr/" in low or "opera" in low:
        browser = "Opera"
    elif "yabrowser" in low:
        browser = "Yandex"
    elif "samsungbrowser" in low:
        browser = "Samsung"
    elif "firefox" in low or "fxios" in low:
        browser = "Firefox"
    elif "chrome" in low or "crios" in low:
        browser = "Chrome"
    elif "safari" in low:
        browser = "Safari"
    else:
        browser = "Other"
    return Client(device, browser, os)


def scrub_text(value: str) -> str:
    """A short label with anything that could be a person taken out."""
    text = _EMAIL_RE.sub("[email]", value)
    text = _LONG_DIGITS_RE.sub("[number]", text)
    return text.strip()[:MAX_PROPERTY_TEXT]


def clean_properties(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The browser's property bag reduced to what the schema allows.

    Allow-listed keys, scalar values, short strings. Anything else is dropped
    rather than refused: a stray key must not cost the event, and nothing in
    here is ever trusted enough to be worth an error message.
    """
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CLIENT_PROPERTY_KEYS or len(out) >= MAX_PROPERTIES:
            continue
        if isinstance(value, bool) or (isinstance(value, int | float) and abs(value) < 10**9):
            out[key] = value
        elif isinstance(value, str) and value.strip():
            out[key] = scrub_text(value)
    return out


def clean_entity(entity_type: str | None, entity_id: str | None) -> tuple[str | None, str | None]:
    if not entity_type or not entity_id:
        return None, None
    kind = entity_type.strip().lower()[:MAX_ENTITY_TYPE]
    ident = entity_id.strip()
    if not re.match(r"^[a-z_]{1,30}$", kind) or not _ENTITY_RE.match(ident):
        raise EventRejected("entity")
    return kind, ident


# ---------------------------------------------------------------------------
# Who is acting: visitor, session, staff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """What a request tells us about who is behind it — nothing more."""

    user_id: uuid.UUID | None
    visitor_hash: str | None
    session_id: str | None
    is_staff: bool


def is_staff_user(user: CurrentUser | None) -> bool:
    return bool(user) and bool(STAFF_ROLES & ({user.role} | set(user.roles)))


def actor_of(request: Request | None, user: CurrentUser | None) -> Actor:
    """The actor behind a request: the signed-in account, the hashed visitor
    cookie and the browser session the client declared."""
    visitor = request.cookies.get(VISITOR_COOKIE) if request is not None else None
    session_id = (
        normalise_session(request.headers.get(SESSION_HEADER)) if request is not None else None
    )
    return Actor(
        user_id=uuid.UUID(user.id) if user is not None else None,
        visitor_hash=hash_visitor(visitor) if visitor else None,
        session_id=session_id,
        is_staff=is_staff_user(user),
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def record_event(
    session: AsyncSession,
    *,
    name: str,
    actor: Actor,
    path: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    properties: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    source: str = "server",
    locale: str | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    """Append one event. Returns False when it was a duplicate or failed.

    Analytics rides on the real request's transaction and must never sink it:
    an exception here is logged and swallowed, and the caller never learns.
    The INSERT is `ON CONFLICT DO NOTHING` on `dedupe_key`, so a retried
    request is dropped at the index rather than raising.
    """
    if name not in EVENT_NAMES:
        raise ValueError(f"unknown event {name!r}")
    try:
        stmt = (
            insert(AnalyticsEvent)
            .values(
                id=uuid.uuid4(),
                name=name,
                occurred_at=occurred_at or datetime.now(UTC),
                source=source,
                visitor_hash=actor.visitor_hash,
                user_id=actor.user_id,
                session_id=actor.session_id,
                path=normalise_path(path) if path else None,
                locale=(locale or "")[:10] or None,
                is_staff=actor.is_staff,
                entity_type=entity_type,
                entity_id=(entity_id or "")[:MAX_ENTITY_ID] or None,
                properties=properties or {},
                dedupe_key=(dedupe_key or "")[:120] or None,
            )
            .on_conflict_do_nothing(
                index_elements=["dedupe_key"], index_where=text("dedupe_key IS NOT NULL")
            )
        )
        # A savepoint: if the INSERT fails inside the database — a missing
        # table, a bad value — only it is rolled back, and the transaction the
        # real record sits in stays usable.
        async with session.begin_nested():
            result = await session.execute(stmt)
        return bool(result.rowcount)
    except Exception:  # noqa: BLE001 — analytics never fails the request
        logger.exception("could not record analytics event %s", name)
        return False


async def touch_presence(
    session: AsyncSession,
    *,
    visitor_hash: str,
    user_id: uuid.UUID | None,
    path: str | None,
    is_staff: bool,
    now: datetime | None = None,
) -> None:
    """She is here now. One row per visitor, overwritten in place."""
    now = now or datetime.now(UTC)
    stmt = insert(VisitorPresence).values(
        visitor_hash=visitor_hash,
        user_id=user_id,
        path=normalise_path(path) if path else None,
        is_staff=is_staff,
        last_seen_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["visitor_hash"],
        set_={
            "last_seen_at": now,
            "path": stmt.excluded.path,
            "is_staff": stmt.excluded.is_staff,
            # A visitor who signs in keeps her account on the row; one who
            # signs out keeps the last known one until the row is overwritten
            # by a later signed-in visit — the count of "signed in now" reads
            # last_seen_at, so a stale account id never inflates it.
            "user_id": stmt.excluded.user_id,
        },
    )
    await session.execute(stmt)


def business(
    name: str,
    *,
    key: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The arguments for a server event about one record, spelled once.

    `key` is the fact that can only happen once — "registered:<user id>",
    "enrolled:<enrollment id>" — and becomes the dedupe key.
    """
    return {
        "name": name,
        "dedupe_key": f"{name}:{key}",
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "properties": properties or {},
    }


def actor_from_record(request: Request | None, user: Any) -> Actor:
    """The actor for an account the server has just loaded or created, when
    there is no token yet to read it from — registration and sign-in."""
    visitor = request.cookies.get(VISITOR_COOKIE) if request is not None else None
    session_id = (
        normalise_session(request.headers.get(SESSION_HEADER)) if request is not None else None
    )
    roles = {assignment.role for assignment in getattr(user, "roles", [])}
    return Actor(
        user_id=user.id,
        visitor_hash=hash_visitor(visitor) if visitor else None,
        session_id=session_id,
        is_staff=bool(STAFF_ROLES & roles),
    )
