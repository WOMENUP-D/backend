"""Site traffic and product analytics.

Section 09 asks the dashboard for registered and active users, but nothing on
the platform counted a *visit* — a woman who browsed the catalogue and left was
invisible, and so was every guest the AI assistant answered.

Three tables, three questions:

* `page_views` — did anyone come, and what did they open. One row per screen
  opened by one visitor. Since 0029 a view also carries the browser session it
  belongs to, where the visit came from (a referrer host, never a full URL)
  and coarse device, browser and operating-system families parsed on the
  server from the User-Agent — the header itself is never stored.
* `analytics_events` — what happened. Business actions the server recorded
  as it wrote the real record (a registration, a completed diagnostic, an
  enrollment) and the few things only the browser can see (a sign-up form
  being started, a course page being read). Counts on the dashboard come from
  the authoritative tables; this stream exists for the steps that leave no
  other trace, for funnels and for the activity timeline.
* `visitor_presence` — who is here right now. One row per visitor, overwritten
  on every view and heartbeat, so "online now" costs one indexed count and the
  table never grows with traffic.

Two constraints shape all three. Data minimisation (section 08): no IP address,
no user agent, no query string, no free text from the browser is stored. And
the whole point is to count people, not requests, so a visitor carries a random
first-party id that is kept hashed — the raw cookie value never reaches the
database, and the rows cannot be tied back to a person by anyone reading them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class PageView(UUIDMixin, TimestampMixin, Base):
    """One screen opened by one visitor."""

    __tablename__ = "page_views"
    __table_args__ = (
        Index("ix_page_views_occurred", "occurred_at"),
        Index("ix_page_views_visitor_occurred", "visitor_hash", "occurred_at"),
        Index("ix_page_views_session_occurred", "session_id", "occurred_at"),
    )

    # HMAC of the visitor's cookie id. Enough to count unique people over a
    # window; useless for identifying one.
    visitor_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Set once she signs in, so the dashboard can separate guests from members.
    # Nullable by design: most traffic is anonymous and must still be counted.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Route only — "/dasturlar", never "/dasturlar?search=her+name". Dynamic
    # segments are folded to a placeholder: "/dasturlar/:id".
    path: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    locale: Mapped[str | None] = mapped_column(String(10))
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The browser session the view belongs to: a random id the browser keeps
    # for the tab and renews after thirty minutes of silence. Null for rows
    # written before sessions were tracked, and for clients that send none.
    session_id: Mapped[str | None] = mapped_column(String(32))
    # First view of its session — the landing page.
    is_entry: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # Where the session came from: the referrer's host only ("t.me",
    # "google.com"), recorded on the entry view, never the page or its query.
    referrer_host: Mapped[str | None] = mapped_column(String(120))
    # Coarse families parsed from the User-Agent on the server. The header is
    # read and dropped; only these words are kept.
    device: Mapped[str | None] = mapped_column(String(10))
    browser: Mapped[str | None] = mapped_column(String(20))
    os: Mapped[str | None] = mapped_column(String(20))
    # A staff account's own browsing. Kept, so the panel's traffic is not
    # silently lost, but excluded from every figure by default.
    is_staff: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalyticsEvent(UUIDMixin, TimestampMixin, Base):
    """One thing that happened — a step in a journey, named and timed.

    Properties are a small bag of plain values the dashboard can group by
    (a diagnostic version, a section of the assistant, a result count). Never
    a name, an address, an answer, a score or a message: the pipeline refuses
    anything but short scalars, and the names of what it will accept from a
    browser are a fixed list (`services.telemetry.CLIENT_EVENTS`).
    """

    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_events_name_occurred", "name", "occurred_at"),
        Index("ix_analytics_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_analytics_events_entity", "entity_type", "entity_id"),
        Index(
            "uq_analytics_events_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # "server" for an event written beside the record it describes, "client"
    # for one the browser reported. Server events are authoritative; client
    # events are evidence of intent.
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="server")

    visitor_hash: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    session_id: Mapped[str | None] = mapped_column(String(32))
    path: Mapped[str | None] = mapped_column(String(120))
    locale: Mapped[str | None] = mapped_column(String(10))
    is_staff: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # The record the event is about — "program" + its id, "assessment" + its id.
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    properties: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # One event per thing that can only happen once: "registered:<user>",
    # "enrolled:<enrollment>". A retried request or a double tap lands on the
    # unique index and is dropped instead of counted twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(120))


class VisitorPresence(Base):
    """The last moment each visitor was seen. Overwritten, never appended."""

    __tablename__ = "visitor_presence"
    __table_args__ = (Index("ix_visitor_presence_last_seen", "last_seen_at"),)

    visitor_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    path: Mapped[str | None] = mapped_column(String(120))
    is_staff: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
