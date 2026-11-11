"""Registration, OTP and token payloads."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

import email_validator
from pydantic import BaseModel, BeforeValidator, EmailStr, Field, model_validator
from pydantic_core import PydanticCustomError

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


#: The passwords every list of leaked passwords opens with. A password on
#: this list is the first guess an attacker makes, so it is not a password.
COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "11111111",
        "00000000",
        "iloveyou",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "welcome1",
        "admin123",
        "letmein1",
        "monkey123",
        "dragon123",
        "abc12345",
        "abcd1234",
        "1q2w3e4r",
        "1q2w3e4r5t",
        "qazwsx123",
        "123123123",
        "987654321",
        "superman",
        "trustno1",
        "whatever",
        "computer",
        "internet",
        "samsung1",
        "changeme",
        "p@ssw0rd",
        "womanup1",
        "womanup123",
        "womanup2026",
        "tashkent",
        "toshkent",
        "uzbekistan",
    }
)


#: The shortest password accepted, counted without surrounding spaces.
MIN_PASSWORD_LENGTH = 8

_RUNS = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")


def _is_simple_run(candidate: str) -> bool:
    """`12345678`, `87654321`, `abcdefgh`, `qwertyui` — a keyboard or
    alphabet walk is guessed as early as any word on the list."""
    for run in _RUNS:
        for sequence in (run, run[::-1]):
            if candidate in sequence or candidate in sequence * 2:
                return True
    return False


def password_is_weak(password: str, email: str | None = None) -> bool:
    """Whether a password is one an attacker tries first.

    Eight characters is the floor, not the goal, and it is counted on what is
    left once surrounding spaces are taken away — eight spaces is not a
    password. No composition rules: they push people towards "Parol1!" and
    away from anything memorable. What is refused is a password from the
    list every attacker opens with, one character repeated or two
    alternating, a straight walk along the keyboard or the alphabet, and her
    own address.
    """
    stripped = password.strip()
    if len(stripped) < MIN_PASSWORD_LENGTH:
        return True
    candidate = stripped.lower()
    if candidate in COMMON_PASSWORDS or len(set(candidate)) <= 2:
        return True
    if _is_simple_run(candidate):
        return True
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and candidate == local:
            return True
    return False


def weak_password_error() -> PydanticCustomError:
    """The 422 a client recognises: `detail` becomes `weak_password`."""
    return PydanticCustomError("weak_password", "this password is too easy to guess")


def _registration_email(value: object) -> str:
    """An address mail can actually reach.

    A domain that is reserved or special-use (`.test`, `.example`,
    `.localhost`, …) can never receive a message, so it is refused with its
    own code, `email_domain_unsupported`, rather than a generic "invalid" —
    the person can act on "use another address".
    """
    if not isinstance(value, str):
        raise PydanticCustomError("value_error", "value is not a valid email address")
    try:
        result = email_validator.validate_email(value.strip(), check_deliverability=False)
    except email_validator.EmailNotValidError as exc:
        reason = str(exc)
        if "special-use or reserved" in reason or "should have a period" in reason:
            raise PydanticCustomError(
                "email_domain_unsupported", "this e-mail domain cannot receive mail"
            ) from None
        raise PydanticCustomError(
            "value_error", "value is not a valid email address: {reason}", {"reason": reason}
        ) from None
    return result.normalized


RegistrationEmail = Annotated[
    str,
    BeforeValidator(_registration_email),
    Field(json_schema_extra={"format": "email"}, max_length=320),
]


class RegisterRequest(BaseModel):
    """Opening an account with an address and a password."""

    email: RegistrationEmail
    # See `password_is_weak` for what is refused, and why so little.
    password: str = Field(max_length=200, json_schema_extra={"minLength": MIN_PASSWORD_LENGTH})

    @model_validator(mode="after")
    def password_is_not_a_guess(self) -> Self:
        if password_is_weak(self.password, str(self.email)):
            raise weak_password_error()
        return self

    # Agreeing to the privacy policy is part of opening an account, not a
    # setting to be found later: the account cannot exist without a lawful basis
    # for holding her data. `Literal[True]` rather than `bool` so a client that
    # omits it or sends false is rejected by the schema instead of quietly
    # creating an account with no consent behind it.
    accepted_privacy_policy: Literal[True]
    policy_version: str = Field(default=PRIVACY_POLICY_VERSION, max_length=20)


class PasswordForgot(BaseModel):
    """Asking for a reset link. The answer is the same whoever asks."""

    email: EmailStr
    # The language the message is written in — whatever she was reading.
    language: str = Field(default="uz", pattern="^(uz|ru|en)$")


class PasswordReset(BaseModel):
    """Setting a new password with the link from the message."""

    token: str = Field(min_length=10, max_length=512)
    password: str = Field(max_length=200, json_schema_extra={"minLength": MIN_PASSWORD_LENGTH})


class PasswordLogin(BaseModel):
    """Coming back to an account."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TokenPair(BaseModel):
    """The session as it is handed to a client.

    The refresh token is set as an HttpOnly cookie on the response and left
    out of the body, so a script in the page cannot read it. A client that
    cannot hold cookies asks for it in the body with `X-Refresh-Transport:
    body`.
    """

    access_token: str
    refresh_token: str | None = None
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
    """The token in the body is optional: browsers present it as a cookie."""

    refresh_token: str | None = Field(default=None, max_length=4096)


class LogoutRequest(BaseModel):
    """Signing out. The refresh token, when the client still has it, is
    revoked too, so the session cannot be resumed from a copy of it."""

    refresh_token: str | None = Field(default=None, max_length=4096)


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


class AccountDeletion(BaseModel):
    """Confirming an erasure: her current password, or — for an account
    without one (Google sign-in) — the word `DELETE`."""

    password: str | None = Field(default=None, max_length=200)
    confirm: str | None = Field(default=None, max_length=20)
