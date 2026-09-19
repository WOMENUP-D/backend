"""The skill taxonomy, what each woman holds, and the evidence behind it.

Three tables, and the split between them is the point:

* `skills` is the vocabulary — a stable slug, names in every language the
  portal writes in, and the spellings found in the wild that mean the same
  thing. Nothing personal.
* `user_skills` is what one woman holds. `status` and `level` are **derived**
  from her evidence by `services.skills`; nothing else writes them, so the two
  can never drift from what backs them.
* `skill_evidence` is why. Every row names its source, so "verified" can always
  be traced to the mentor, employer or placement that verified it.

Courses, listings and mentor profiles keep their free-text skill columns: those
are the labels an author or a partner platform wrote, and they are resolved
against the aliases here rather than being rewritten under the author.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    EvidenceKind,
    ProficiencyLevel,
    SkillCategory,
    SkillStatus,
)
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class Skill(UUIDMixin, TimestampMixin, Base):
    """One skill, named once and displayed in three languages.

    `slug` is the identifier everything else refers to. It is deliberately not
    the Uzbek word: a display name gets reworded and translated, an identifier
    must not move under the rows pointing at it.
    """

    __tablename__ = "skills"
    __table_args__ = (
        Index("ix_skills_category_active", "category", "is_active"),
        # Label lookup is `aliases && ARRAY[...]` on every match, so it gets the
        # index type that answers array overlap.
        Index("ix_skills_aliases", "aliases", postgresql_using="gin"),
    )

    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    category: Mapped[SkillCategory] = mapped_column(
        str_enum(SkillCategory, 30),
        default=SkillCategory.PROFESSIONAL,
        nullable=False,
    )
    #: `ScoreDimension` values this skill moves. The link that lets the cabinet
    #: answer "you can improve this area by developing these skills".
    dimensions: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    #: Normalised spellings that mean this skill — every label found on a
    #: course, a listing or a profile, in any of the portal's languages.
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    #: False for a skill the platform minted from a label it had never seen, so
    #: an editor can find and name them properly later.
    is_curated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class UserSkill(UUIDMixin, TimestampMixin, Base):
    """A skill one woman holds, as strongly as her evidence supports.

    `status` and `level` are a cache of what `evidence` adds up to — kept as
    columns because employer matching will filter on them, and recomputed by
    `services.skills` whenever evidence changes. A row with `status = NULL`
    holds only evidence that has since been withdrawn.
    """

    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", name="uq_user_skills_user_id"),
        Index("ix_user_skills_skill_status", "skill_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[SkillStatus | None] = mapped_column(str_enum(SkillStatus, 20))
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))

    skill: Mapped[Skill] = relationship(lazy="selectin")
    evidence: Mapped[list[SkillEvidence]] = relationship(
        back_populates="user_skill",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SkillEvidence.created_at",
    )


class SkillEvidence(UUIDMixin, TimestampMixin, Base):
    """One reason to believe she has a skill.

    `source_type` + `source_id` point at the record that produced it — an
    enrollment, a certificate, later a practical task or an employer's review.
    They are also the idempotency key: replaying the same completion adds
    nothing, so a backfill and a live completion cannot double-count.

    Withdrawn evidence is marked, never deleted: a revoked certificate is part
    of the story of a skill, and dropping the row would quietly rewrite it.
    """

    __tablename__ = "skill_evidence"
    __table_args__ = (
        UniqueConstraint(
            "user_skill_id",
            "kind",
            "source_type",
            "source_id",
            name="uq_skill_evidence_user_skill_id",
        ),
        Index("ix_skill_evidence_created_at", "created_at"),
    )

    user_skill_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("user_skills.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[EvidenceKind] = mapped_column(str_enum(EvidenceKind, 30), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    #: A record id, or a stable key for evidence with no record of its own.
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The level this piece of evidence supports, when its source says so.
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))
    score: Mapped[float | None] = mapped_column(Float)
    #: The mentor, trainer or partner account that vouched for it, when a
    #: person did. Employer and mentor review connect here.
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user_skill: Mapped[UserSkill] = relationship(back_populates="evidence")
