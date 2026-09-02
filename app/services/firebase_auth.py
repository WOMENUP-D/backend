"""Establishing who signed in, from a Firebase ID token.

The browser does the whole OAuth dance with Google through the Firebase SDK
and comes back holding an ID token. That token is the only thing this server
accepts as evidence: nothing the page *says* about who she is is believed, and
the token is checked before a single row is touched.

`verify` runs the Firebase Admin SDK's own verification — RSA signature against
Google's published certificates, issuer, audience (our project, so a token
minted for somebody else's Firebase project cannot open an account here), and
expiry — and then adds two checks of our own:

* the sign-in provider really was Google, so enabling e-mail/password or
  anonymous sign-in in the console later cannot quietly become a second way in;
* Google itself considers the address verified, because an unverified address
  proves nothing about who owns it and must never match an existing account.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import firebase_admin
from anyio import to_thread
from firebase_admin import auth as fb_auth
from firebase_admin import credentials

from app.core.config import settings

# Firebase records which provider actually signed her in. Only this one counts.
GOOGLE_PROVIDER = "google.com"


class FirebaseAuthError(Exception):
    """The identity could not be established. Maps to 401."""


class FirebaseNotConfigured(FirebaseAuthError):
    """No service account in the environment. Maps to 503."""


class FirebaseUnavailable(FirebaseAuthError):
    """Firebase could not be reached. Maps to 503 — not the caller's fault."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """The verified claims, and only the ones we actually store."""

    subject: str
    email: str
    given_name: str
    family_name: str
    picture: str | None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.given_name, self.family_name) if part)


_app: firebase_admin.App | None = None
_lock = threading.Lock()


def _firebase_app() -> firebase_admin.App:
    """Initialise the Admin SDK once, on first use.

    Locked because verification runs in a worker thread: two sign-ins landing
    together would otherwise both try to initialise and one would raise.
    """
    global _app
    if _app is not None:
        return _app

    with _lock:
        if _app is not None:
            return _app
        if not settings.firebase_enabled:
            raise FirebaseNotConfigured("Google sign-in is not configured")

        certificate = credentials.Certificate(
            {
                "type": "service_account",
                "project_id": settings.firebase_project_id,
                "client_email": settings.firebase_client_email,
                "private_key": settings.firebase_private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
        _app = firebase_admin.initialize_app(
            certificate,
            {"projectId": settings.firebase_project_id},
            name="womanup-auth",
        )
        return _app


def _split_name(full: str) -> tuple[str, str]:
    """Firebase sends one `name`, the onboarding form asks for two fields.

    First word is the given name, the rest the family name — right for
    "Nilufar Yusupova" and for the great majority of names here. She can
    correct it on the onboarding screen, which is why this is a starting
    point rather than a stored truth.
    """
    parts = full.strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _identity_from(claims: dict[str, Any]) -> GoogleIdentity:
    firebase_claims = claims.get("firebase") or {}
    if firebase_claims.get("sign_in_provider") != GOOGLE_PROVIDER:
        raise FirebaseAuthError("this sign-in did not come from a Google account")

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise FirebaseAuthError("this Google account has no e-mail address")
    if not claims.get("email_verified"):
        raise FirebaseAuthError("this Google e-mail address is not verified")

    if settings.firebase_allowed_domain:
        domain = email.rpartition("@")[2]
        if domain != settings.firebase_allowed_domain.lower():
            raise FirebaseAuthError("this Google account is outside the allowed domain")

    given, family = _split_name(str(claims.get("name") or ""))
    picture = claims.get("picture")
    return GoogleIdentity(
        subject=str(claims.get("uid") or claims["sub"]),
        email=email,
        given_name=given,
        family_name=family,
        picture=str(picture) if picture else None,
    )


def verify_id_token(id_token: str) -> GoogleIdentity:
    """Validate a Firebase ID token. Blocking — call `verify` from async code."""
    app = _firebase_app()

    try:
        claims = fb_auth.verify_id_token(id_token, app=app)
    except (fb_auth.ExpiredIdTokenError, fb_auth.RevokedIdTokenError) as exc:
        raise FirebaseAuthError("this sign-in has expired, please try again") from exc
    except (fb_auth.InvalidIdTokenError, fb_auth.UserDisabledError, ValueError) as exc:
        # Malformed, wrong project, bad signature, disabled account — one
        # message for all of them, so the screen cannot be used to probe.
        raise FirebaseAuthError("invalid Google sign-in") from exc
    except fb_auth.CertificateFetchError as exc:
        raise FirebaseUnavailable("could not reach Google to verify the sign-in") from exc

    return _identity_from(claims)


async def verify(id_token: str) -> GoogleIdentity:
    """Async wrapper — certificate fetching and RSA verification are blocking."""
    return await to_thread.run_sync(verify_id_token, id_token)
