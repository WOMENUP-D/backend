"""Registration, OTP and token payloads."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.core.constants import (
    PRIVACY_POLICY_VERSION,
    Language,
    Region,
    Role,
)

UZ_PHONE = re.compile(r"^\+998\d{9}$")


class OtpRequest(BaseModel):
    """Start registration or login. Exactly one identifier is required."""

    phone: str | None = Field(default=None, examples=["+998901234567"])
    email: EmailStr | None = None
    purpose: str = Field(default="login", pattern="^(login|register|reset)$")
    # Which language the message is written in — whatever she was reading when
    # she asked for the code.
    language: str = Field(default="uz", pattern="^(uz|ru|en)$")

    @model_validator(mode="after")
    def one_identifier(self) -> Self:
        if bool(self.phone) == bool(self.email):
            raise ValueError("provide exactly one of phone or email")
        if self.phone and not UZ_PHONE.match(self.phone):
            raise ValueError("phone must match +998XXXXXXXXX")
        return self

    @property
    def identifier(self) -> str:
        return self.phone or str(self.email)


class OtpVerify(BaseModel):
    phone: str | None = None
    email: EmailStr | None = None
    code: str = Field(min_length=4, max_length=8)

    @model_validator(mode="after")
    def one_identifier(self) -> Self:
        if bool(self.phone) == bool(self.email):
            raise ValueError("provide exactly one of phone or email")
        return self

    @property
    def identifier(self) -> str:
        return self.phone or str(self.email)


class RegisterRequest(BaseModel):
    """Opening an account with an address and a password."""

    email: EmailStr
    # Eight characters is the floor, not the goal. No composition rules: they
    # push people towards "Parol1!" and away from anything memorable, and the
    # length is what actually costs an attacker.
    password: str = Field(min_length=8, max_length=200)
    # Agreeing to the privacy policy is part of opening an account, not a
    # setting to be found later: the account cannot exist without a lawful basis
    # for holding her data. `Literal[True]` rather than `bool` so a client that
    # omits it or sends false is rejected by the schema instead of quietly
    # creating an account with no consent behind it.
    accepted_privacy_policy: Literal[True]
    policy_version: str = Field(default=PRIVACY_POLICY_VERSION, max_length=20)


class PasswordLogin(BaseModel):
    """Coming back to an account."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class GoogleAuthRequest(BaseModel):
    """The Firebase ID token the browser holds after signing in with Google.

    It is evidence, not a claim: the server verifies it against Firebase before
    anything is read or written, so what the page says about who she is never
    matters.
    """

    id_token: str = Field(min_length=32, max_length=8192)


class GoogleTokenPair(TokenPair):
    """A token pair plus what the sign-in screen needs to decide where to go."""

    is_new_user: bool = False
    onboarding_completed: bool = False
    email: str | None = None
    given_name: str = ""
    family_name: str = ""
    avatar_url: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class CurrentUser(BaseModel):
    """Decoded JWT subject, attached to every authenticated request."""

    id: str
    role: Role
    roles: list[Role] = []
    region: Region | None = None
    language: Language = Language.UZ

    def has_role(self, *roles: Role) -> bool:
        return bool(set(roles) & (set(self.roles) | {self.role}))


class StaffLogin(BaseModel):
    """A desk account signs in with a login and a password, not an SMS code."""

    login: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=200)
