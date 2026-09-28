"""User, role assignment and OTP challenge."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import Language, Region, Role, UserStatus
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum

if TYPE_CHECKING:
    from app.models.consent import ConsentLog
    from app.models.profile import Profile


class User(UUIDMixin, TimestampMixin, Base):
    """A WomanUP ID. Either phone or email is present; both are unique."""

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_region_status", "region", "status"),
        Index("ix_users_last_active_at", "last_active_at"),
        Index("ix_users_created_at", "created_at"),
    )

    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))

    # Firebase's stable uid for the account. Unique, and the first thing a
    # Google sign-in looks up: the e-mail on a Google account can be changed by
    # its owner, the uid cannot, so matching on the uid is what stops one
    # person ending up with two WomanUP IDs.
    firebase_uid: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    # How the account was opened: "otp" (phone or e-mail code), "google", or
    # "password" for a desk account. Recorded for support and analytics; it is
    # not what authorisation is decided on.
    auth_provider: Mapped[str] = mapped_column(
        String(20), default="otp", server_default="otp", nullable=False
    )

    status: Mapped[UserStatus] = mapped_column(
        str_enum(UserStatus, 20),
        default=UserStatus.PENDING,
        nullable=False,
    )
    language: Mapped[Language] = mapped_column(
        str_enum(Language, 5),
        default=Language.UZ,
        nullable=False,
    )
    region: Mapped[Region | None] = mapped_column(str_enum(Region, 30), index=True)

    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anonymised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # UserRole has two FKs to users (the holder and the granting admin), so the
    # join column must be named explicitly.
    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="UserRole.user_id",
    )
    profile: Mapped[Profile | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    consents: Mapped[list[ConsentLog]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def role_set(self) -> set[Role]:
        return {assignment.role for assignment in self.roles}

    @property
    def primary_role(self) -> Role:
        """Highest-privilege role, used for the JWT `role` claim."""
        order = [
            Role.ADMIN,
            Role.MODERATOR,
            Role.REGIONAL_COORDINATOR,
            Role.TRAINER,
            Role.PARTNER,
            Role.MENTOR,
            Role.MOTHER,
            Role.USER,
        ]
        assigned = self.role_set
        return next((role for role in order if role in assigned), Role.USER)


class UserRole(UUIDMixin, TimestampMixin, Base):
    """Role assignment. `scope_region` limits a coordinator to their region."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_roles_user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role] = mapped_column(str_enum(Role, 30), nullable=False)
    scope_region: Mapped[Region | None] = mapped_column(str_enum(Region, 30))
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    user: Mapped[User] = relationship(back_populates="roles", foreign_keys=[user_id])


class OtpChallenge(UUIDMixin, TimestampMixin, Base):
    """Pending OTP verification. The code itself is stored hashed."""

    __tablename__ = "otp_challenges"
    __table_args__ = (Index("ix_otp_challenges_identifier_expires", "identifier", "expires_at"),)

    identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), default="login", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
