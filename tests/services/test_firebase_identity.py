"""Turning a verified Firebase token into an identity.

The Admin SDK's own checks — signature, issuer, audience, expiry — are Google's
code and are not re-tested here. What is tested is everything we add on top,
and the reason each one exists: a token that is perfectly valid can still
belong to somebody who must not be let in.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import firebase_auth
from app.services.firebase_auth import FirebaseAuthError


def claims(**overrides):
    base = {
        "uid": "firebase-uid-123",
        "sub": "firebase-uid-123",
        "email": "Dilnoza@Gmail.com",
        "email_verified": True,
        "name": "Dilnoza Karimova",
        "picture": "https://lh3.googleusercontent.com/a/photo",
        "firebase": {"sign_in_provider": "google.com"},
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def public_portal(monkeypatch):
    monkeypatch.setattr(settings, "firebase_allowed_domain", None)


def test_a_google_sign_in_yields_the_identity():
    identity = firebase_auth._identity_from(claims())
    assert identity.subject == "firebase-uid-123"
    # Addresses are matched case-insensitively downstream, so they are folded
    # here once rather than at every call site.
    assert identity.email == "dilnoza@gmail.com"
    assert identity.given_name == "Dilnoza"
    assert identity.family_name == "Karimova"
    assert identity.full_name == "Dilnoza Karimova"


def test_a_sign_in_from_another_provider_is_refused():
    """The single most important check we add.

    Switching on e-mail/password or anonymous sign-in in the Firebase console
    would otherwise become a second, unintended way into the portal — with a
    token the Admin SDK considers entirely valid.
    """
    for provider in ("password", "anonymous", "phone", "facebook.com"):
        with pytest.raises(FirebaseAuthError):
            firebase_auth._identity_from(claims(firebase={"sign_in_provider": provider}))


def test_a_token_without_provider_information_is_refused():
    with pytest.raises(FirebaseAuthError):
        firebase_auth._identity_from(claims(firebase={}))


def test_an_unverified_address_is_refused():
    """An unverified address proves nothing about who owns it, and must never
    be allowed to match an existing account."""
    with pytest.raises(FirebaseAuthError):
        firebase_auth._identity_from(claims(email_verified=False))


def test_a_token_without_an_address_is_refused():
    with pytest.raises(FirebaseAuthError):
        firebase_auth._identity_from(claims(email=""))


def test_an_outside_domain_is_refused_when_one_is_required(monkeypatch):
    monkeypatch.setattr(settings, "firebase_allowed_domain", "womanup.uz")
    with pytest.raises(FirebaseAuthError):
        firebase_auth._identity_from(claims())
    inside = firebase_auth._identity_from(claims(email="dilnoza@womanup.uz"))
    assert inside.email == "dilnoza@womanup.uz"


@pytest.mark.parametrize(
    ("full", "given", "family"),
    [
        ("Dilnoza Karimova", "Dilnoza", "Karimova"),
        ("Nilufar Abdullayeva Rustamovna", "Nilufar", "Abdullayeva Rustamovna"),
        ("Gulnora", "Gulnora", ""),
        ("", "", ""),
        ("   ", "", ""),
    ],
)
def test_one_google_name_becomes_two_form_fields(full, given, family):
    """Firebase sends a single `name`; the onboarding form asks for two.

    Whatever the split gets wrong she corrects on that form, which is why this
    is a starting point rather than a stored truth.
    """
    assert firebase_auth._split_name(full) == (given, family)


def test_a_missing_photo_is_not_invented():
    assert firebase_auth._identity_from(claims(picture=None)).picture is None
