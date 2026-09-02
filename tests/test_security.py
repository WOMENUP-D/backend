"""Token handling, OTP hashing and PII scrubbing."""

import uuid

import jwt
import pytest

from app.core.constants import Role
from app.core.logging import scrub_pii
from app.core.security import (
    create_token,
    decode_token,
    generate_otp,
    hash_otp,
    mask_email,
    mask_phone,
    verify_otp,
)


def test_access_token_round_trips():
    subject = uuid.uuid4()
    payload = decode_token(create_token(subject, Role.USER), "access")
    assert payload["sub"] == str(subject)
    assert payload["role"] == Role.USER.value


def test_a_refresh_token_is_not_accepted_as_an_access_token():
    token = create_token(uuid.uuid4(), Role.USER, "refresh")
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, "access")


def test_otp_is_numeric_and_of_the_configured_length():
    code = generate_otp(6)
    assert len(code) == 6 and code.isdigit()


def test_otp_verification_is_salt_bound():
    code, salt = "123456", "abc"
    hashed = hash_otp(code, salt)
    assert verify_otp(code, salt, hashed)
    assert not verify_otp("654321", salt, hashed)
    assert not verify_otp(code, "different-salt", hashed)


def test_identifiers_are_masked():
    assert mask_phone("+998901234567") == "+998*****4567"
    assert mask_email("dilnoza@example.uz").endswith("@example.uz")
    assert "dilnoza" not in mask_email("dilnoza@example.uz")


def test_pii_is_scrubbed_from_free_text():
    scrubbed = scrub_pii("Call +998901234567 or write to dilnoza@example.uz")
    assert "+998901234567" not in scrubbed
    assert "dilnoza@example.uz" not in scrubbed
    assert "<phone>" in scrubbed and "<email>" in scrubbed
