"""Password hashing, JWT issuance and OTP generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.constants import Role

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(
    subject: UUID | str,
    role: Role,
    token_type: TokenType = "access",
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    ttl = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if token_type == "access"
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role.value,
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(16),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    """Decode and validate a JWT. Raises jwt.PyJWTError subclasses on failure."""
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload


def generate_otp(length: int | None = None) -> str:
    """Cryptographically random numeric OTP."""
    n = length or settings.otp_length
    return "".join(secrets.choice("0123456789") for _ in range(n))


def hash_otp(code: str, salt: str) -> str:
    """OTP codes are never stored in clear text."""
    return hmac.new(
        settings.jwt_secret_key.encode(), f"{salt}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def verify_otp(code: str, salt: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_otp(code, salt), hashed)


def mask_phone(phone: str) -> str:
    """+998901234567 -> +998****4567. Used in logs and AI prompts."""
    if len(phone) < 8:
        return "*" * len(phone)
    return f"{phone[:4]}{'*' * (len(phone) - 8)}{phone[-4:]}"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "*" * len(email)
    keep = local[:1] if local else ""
    return f"{keep}{'*' * max(len(local) - 1, 1)}@{domain}"
