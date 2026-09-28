"""Profile and goals — the personal cabinet's core records."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import GoalHorizon, Priority, ScoreDimension
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum
from app.models.user import User


class Profile(UUIDMixin, TimestampMixin, Base):
    """Personal, educational, professional and family attributes.

    Family and health-adjacent fields are `sensitive` under section 08 and are
    only exposed through scopes that explicitly request them.
    """

    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    full_name: Mapped[str | None] = mapped_column(String(255))
    birth_date: Mapped[date | None] = mapped_column(Date)
    age_group: Mapped[str | None] = mapped_column(String(20))
    district: Mapped[str | None] = mapped_column(String(120))

    education_level: Mapped[str | None] = mapped_column(String(60))
    education_field: Mapped[str | None] = mapped_column(String(120))
    employment_status: Mapped[str | None] = mapped_column(String(60))
    profession: Mapped[str | None] = mapped_column(String(120))
    years_of_experience: Mapped[int | None] = mapped_column(Integer)

    # Sensitive block — minimal collection, separate access scope.
    marital_status: Mapped[str | None] = mapped_column(String(40))
    children_count: Mapped[int | None] = mapped_column(Integer)
    has_disability: Mapped[bool | None] = mapped_column(Boolean)
    is_in_women_register: Mapped[bool | None] = mapped_column(
        Boolean, comment="Listed in the state 'Ayollar daftari' register"
    )

    skills: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    interests: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    # `NewsTopic` values, chosen on the news-preferences screen. Kept apart
    # from `interests` above, which is free text she wrote about herself and
    # which the assistant reads as prose: mixing a controlled vocabulary the
    # ranker matches on into it would corrupt both.
    news_interests: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(String(500))

    # --- portfolio visibility -------------------------------------------
    # Kept on the profile rather than in a table of their own: they are one
    # row per woman, and they describe how her own record is shown. Private
    # until she says otherwise, and never public for a minor or a woman whose
    # age is unknown — see `services.portfolio.may_publish`.
    portfolio_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: An opaque token, never derived from her id — a public link must not be
    #: a way of enumerating accounts. Minted on first publish, kept after.
    portfolio_slug: Mapped[str | None] = mapped_column(String(40), unique=True)
    portfolio_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Which sections a public reader sees: {"skills": true, ...}. A section
    #: missing from the map counts as shown, so adding one later does not
    #: silently hide it from everyone who published before it existed.
    portfolio_sections: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    completeness_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class Goal(UUIDMixin, TimestampMixin, Base):
    """A user-chosen objective on a 3/6/12/36-month horizon."""

    __tablename__ = "goals"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    horizon: Mapped[GoalHorizon] = mapped_column(str_enum(GoalHorizon, 5), nullable=False)
    dimension: Mapped[ScoreDimension | None] = mapped_column(str_enum(ScoreDimension, 40))
    priority: Mapped[Priority] = mapped_column(
        str_enum(Priority, 10),
        default=Priority.MEDIUM,
        nullable=False,
    )
    target_date: Mapped[date | None] = mapped_column(Date)
    achieved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class LearningProfile(UUIDMixin, TimestampMixin, Base):
    """What she wants to learn, and how ready she is to learn it.

    Separate from `Profile` (who she is) and from `Assessment` (how she is
    doing across the eight dimensions): this one exists to aim the assistant
    at what she actually wants to learn.

    `answers` is keyed by the question ids in `services.questionnaire`, which
    is data rather than a table — so rewording a question is an edit, not a
    migration. `version` records which wording she actually answered.
    """

    __tablename__ = "learning_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # A short, readable summary the assistant reads instead of raw answers.
    derived: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
