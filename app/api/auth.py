"""Registration, OTP verification and token refresh."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections import defaultdict

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession, client_ip
from app.core import security_log
from app.core.config import settings
from app.core.constants import ConsentScope
from app.core.security import decode_token
from app.models.user import User
from app.schemas.auth import (
    GoogleAuthRequest,
    GoogleTokenPair,
    LogoutRequest,
    OtpRequest,
    OtpVerify,
    PasswordLogin,
    RefreshRequest,
    RegisterRequest,
    StaffLogin,
    TokenPair,
)
from app.schemas.common import Message
from app.services import (
    auth_service,
    firebase_auth,
    mailer,
    rate_limit,
    telemetry,
    token_revocation,
)
from app.services.audit_service import record_consent
from app.services.auth_service import AuthError, EmailTaken, OtpThrottled, StaffOtpBlocked
from app.services.firebase_auth import (
    FirebaseAuthError,
    FirebaseNotConfigured,
    FirebaseUnavailable,
)

router = APIRouter(prefix="/auth", tags=["auth"])


OTP_OFF = "sign in with your e-mail address and password"
#: A client that cannot hold cookies (a native app, a script, a test) asks for
#: the refresh token in the body with this header. Browsers never send it.
BODY_TRANSPORT = "body"


def _cookie_path() -> str:
    return f"{settings.api_v1_prefix}/auth"


def _cookie_secure() -> bool:
    if settings.auth_cookie_secure is not None:
        return settings.auth_cookie_secure
    return settings.environment != "local"


def _set_session_cookie(response: Response, refresh_token: str) -> None:
    """The refresh token, where a script in the page cannot read it.

    HttpOnly, sent only to the auth endpoints (the only ones that take it),
    Secure outside local development, SameSite so a cross-site page cannot
    make the browser present it.
    """
    response.set_cookie(
        settings.auth_cookie_name,
        refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=_cookie_path(),
        domain=settings.auth_cookie_domain,
        httponly=True,
        secure=_cookie_secure(),
        samesite=settings.auth_cookie_samesite,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.auth_cookie_name,
        path=_cookie_path(),
        domain=settings.auth_cookie_domain,
        httponly=True,
        secure=_cookie_secure(),
        samesite=settings.auth_cookie_samesite,
    )


def _deliver(tokens: TokenPair, request: Request, response: Response) -> TokenPair:
    """Hand a session to the client: the refresh token as a cookie, and in
    the body only for a client that said it cannot take cookies."""
    assert tokens.refresh_token is not None
    _set_session_cookie(response, tokens.refresh_token)
    if request.headers.get("x-refresh-transport", "").lower() == BODY_TRANSPORT:
        return tokens
    return tokens.model_copy(update={"refresh_token": None})


def _presented_refresh(
    request: Request, payload: RefreshRequest | LogoutRequest | None
) -> str | None:
    """The refresh token this request carries: in the body, else the cookie."""
    if payload is not None and payload.refresh_token:
        return payload.refresh_token
    return request.cookies.get(settings.auth_cookie_name)


async def _refuse_if_held(session: DbSession, limit: rate_limit.Limit, identifier: str) -> None:
    """A held account answers the same 401 as a wrong password, so the hold
    itself does not confirm that the account exists."""
    if await rate_limit.exhausted(session, limit, identifier):
        security_log.event("auth.account_held", identifier=security_log.masked(identifier))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )


PHONE_OTP_OFF = "phone sign-in is not available yet — sign in with your e-mail address"
TOO_MANY_CODES = "too many code requests, please try again later"


def _otp_ip(request: Request) -> str:
    return client_ip(request) or "unknown"


def _refuse_unavailable(payload: OtpRequest | OtpVerify) -> None:
    """Refuse a way in that the portal does not offer.

    Codes are off by default: the portal signs people in with an address and a
    password, so this endpoint has nobody to serve and every reason to be
    closed. Phone has its own switch, because a code nothing sends can only be
    obtained by guessing it.
    """
    if not settings.otp_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=OTP_OFF)
    if payload.phone and not settings.phone_otp_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PHONE_OTP_OFF)


async def _within_limits(session: DbSession, limits, request: Request, identifier: str) -> None:
    by_ip, by_identifier = limits
    allowed = await rate_limit.take(session, by_ip, _otp_ip(request))
    if allowed:
        allowed = await rate_limit.take(session, by_identifier, identifier)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_CODES)


@router.post("/otp/request", response_model=Message)
async def request_otp(payload: OtpRequest, request: Request, session: DbSession) -> Message:
    """Send a one-time code to her e-mail address.

    The code leaves this function exactly once, in the message. It is returned
    in the response only in local development, where there is no mail account
    and the demo would otherwise be unusable; on staging and production that
    branch is unreachable and a send failure is reported as a failure rather
    than as a code she will wait for and never receive.

    A desk account gets the same answer as everyone else and no code at all:
    the reply must not say which addresses belong to staff.
    """
    _refuse_unavailable(payload)
    await _within_limits(session, rate_limit.otp_request_limits(), request, payload.identifier)

    sent = Message(detail="Verification code sent")
    try:
        _, code = await auth_service.issue_otp(
            session, payload.identifier, payload.purpose, is_email=payload.email is not None
        )
    except StaffOtpBlocked:
        return sent
    except OtpThrottled as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    try:
        await mailer.send_code(payload.identifier, code, payload.language)
    except mailer.MailNotConfigured:
        if settings.environment != "local":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="e-mail sending is not configured",
            ) from None
        # Local development has no mailbox: the code is handed back instead.
        return Message(detail=f"Verification code (local only): {code}")
    except mailer.MailFailed:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="could not send the code, please try again",
        ) from None
    return sent


class IpThrottle:
    """Failed attempts per IP in a rolling window.

    In-process, so it protects a single worker and resets on restart — enough
    to make guessing pointless at this scale, and the place to swap in Redis
    when there is more than one worker. Each endpoint gets its own instance so
    a wrong staff password cannot lock a woman out of Google sign-in.
    """

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self._limit = limit
        self._window = window
        self._failures: dict[str, list[float]] = defaultdict(list)

    def allowed(self, ip: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failures[ip] if now - t < self._window]
        self._failures[ip] = recent
        return len(recent) < self._limit

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.monotonic())


_staff_throttle = IpThrottle(limit=5)
# Same reasoning as the staff endpoint: a password is worth guessing at.
_login_throttle = IpThrottle(limit=8)
# Registration is throttled too, so the endpoint cannot be used to farm
# accounts or to walk a list of addresses looking for "already registered".
_register_throttle = IpThrottle(limit=6, window=300.0)
# Looser than the password endpoint: a Google code is single-use and already
# proves Google authenticated somebody, so the risk is retry storms rather
# than guessing.
_google_throttle = IpThrottle(limit=15)


def _client_ip(request: Request) -> str:
    """Who is actually knocking, not who forwarded the knock.

    Behind the ingress every request arrives from the proxy's own address, so
    keying the throttles on it gave everyone one shared bucket: eight wrong
    passwords a minute from anybody refused a password sign-in to every woman
    in the country, while the attacker's own allowance was the same one.

    Caddy overwrites `X-Forwarded-For` with the connecting address, so the
    left-most entry is the client and a header the client sets itself cannot
    survive the hop. `client_ip` is the one place that reads it.
    """
    return client_ip(request) or "unknown"


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, response: Response, session: DbSession
) -> TokenPair:
    """Open an account with an address and a password.

    She is signed in immediately: sending her back to a sign-in form to retype
    what she just typed is the single most common place a registration is
    abandoned.
    """
    ip = _client_ip(request)
    if not _register_throttle.allowed(ip):
        security_log.event("auth.throttled", endpoint="register", ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, please wait",
        )

    try:
        user = await auth_service.register_with_password(
            session, str(payload.email), payload.password
        )
    except EmailTaken as exc:
        _register_throttle.record_failure(ip)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Append-only, and written in the same transaction as the account: an
    # account that exists without the consent that justifies holding its data
    # is exactly the state this record is meant to make impossible.
    await record_consent(
        session,
        user_id=user.id,
        scope=ConsentScope.PRIVACY_POLICY,
        accepted=True,
        policy_version=payload.policy_version,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )

    await telemetry.record_event(
        session,
        actor=telemetry.actor_from_record(request, user),
        **telemetry.business(
            "registration_completed", key=user.id, properties={"provider": "password"}
        ),
    )

    tokens = auth_service.issue_tokens(user)
    await session.commit()
    return _deliver(tokens, request, response)


@router.post("/login", response_model=TokenPair)
async def login(
    payload: PasswordLogin, request: Request, response: Response, session: DbSession
) -> TokenPair:
    """Sign in with an address and a password."""
    ip = _client_ip(request)
    if not _login_throttle.allowed(ip):
        security_log.event("auth.throttled", endpoint="login", ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, wait a minute",
        )
    by_account, _ = rate_limit.login_failure_limits()
    identifier = str(payload.email).strip().lower()
    await _refuse_if_held(session, by_account, identifier)

    try:
        user = await auth_service.authenticate_user(session, str(payload.email), payload.password)
    except AuthError as exc:
        _login_throttle.record_failure(ip)
        await rate_limit.take(session, by_account, identifier)
        security_log.event(
            "auth.login_failed",
            endpoint="login",
            ip=ip,
            identifier=security_log.masked(str(payload.email)),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    await telemetry.record_event(
        session,
        name="login_completed",
        actor=telemetry.actor_from_record(request, user),
        properties={"provider": "password"},
    )
    tokens = auth_service.issue_tokens(user)
    await session.commit()
    return _deliver(tokens, request, response)


@router.post("/staff/login", response_model=TokenPair)
async def staff_login(
    payload: StaffLogin, request: Request, response: Response, session: DbSession
) -> TokenPair:
    """Sign in a staff account with a login and a password.

    Separate from the OTP flow on purpose: an office account belongs to a desk,
    not to somebody's personal handset, and a coordinator on a shared machine
    should not need an SMS to open a dashboard. Rate-limited by IP, because a
    password endpoint is worth guessing at and a one-time code is not.
    """
    ip = _client_ip(request)
    if not _staff_throttle.allowed(ip):
        security_log.event("auth.throttled", endpoint="staff_login", ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, wait a minute",
        )
    _, by_account = rate_limit.login_failure_limits()
    identifier = payload.login.strip().lower()
    await _refuse_if_held(session, by_account, identifier)

    try:
        user = await auth_service.authenticate_staff(session, payload.login, payload.password)
    except AuthError as exc:
        _staff_throttle.record_failure(ip)
        await rate_limit.take(session, by_account, identifier)
        # A wrong password on a desk account is the event most worth an
        # alert: staff accounts are the ones worth guessing at.
        security_log.event(
            "auth.login_failed",
            endpoint="staff_login",
            ip=ip,
            identifier=security_log.masked(payload.login),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    await telemetry.record_event(
        session,
        name="login_completed",
        actor=telemetry.actor_from_record(request, user),
        properties={"provider": "staff"},
    )
    tokens = auth_service.issue_tokens(user)
    await session.commit()
    return _deliver(tokens, request, response)


@router.post("/google", response_model=GoogleTokenPair)
async def google_login(
    payload: GoogleAuthRequest, request: Request, response: Response, session: DbSession
) -> GoogleTokenPair:
    """Sign in with Google.

    The browser never tells us who it is — it hands over the ID token Firebase
    gave it, and the identity is established here by verifying that token. An
    account is created on first sign-in; on every later one the same account is
    found again, by Firebase's stable uid and then by the verified address, so
    nobody ends up with two WomanUP IDs.
    """
    ip = _client_ip(request)
    if not _google_throttle.allowed(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, wait a minute",
        )

    try:
        identity = await firebase_auth.verify(payload.id_token)
    except (FirebaseNotConfigured, FirebaseUnavailable) as exc:
        # Not her fault and not a credential problem: the sign-in screen should
        # say "try again", not "wrong account".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except FirebaseAuthError as exc:
        _google_throttle.record_failure(ip)
        security_log.event("auth.login_failed", endpoint="google", ip=ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        user, created = await auth_service.login_with_google(session, identity)
    except AuthError as exc:
        _google_throttle.record_failure(ip)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    actor = telemetry.actor_from_record(request, user)
    if created:
        await telemetry.record_event(
            session,
            actor=actor,
            **telemetry.business(
                "registration_completed", key=user.id, properties={"provider": "google"}
            ),
        )
    else:
        await telemetry.record_event(
            session, name="login_completed", actor=actor, properties={"provider": "google"}
        )
    tokens = auth_service.issue_tokens(user)
    await session.commit()

    return GoogleTokenPair(
        **_deliver(tokens, request, response).model_dump(),
        is_new_user=created,
        onboarding_completed=user.onboarding_completed_at is not None,
        email=user.email,
        given_name=identity.given_name,
        family_name=identity.family_name,
        avatar_url=identity.picture,
    )


@router.post("/otp/verify", response_model=TokenPair)
async def verify_otp(
    payload: OtpVerify, request: Request, response: Response, session: DbSession
) -> TokenPair:
    """Verify the code; creates the account on first successful verification.

    Wrong guesses are counted against the code itself (which blocks after
    `otp_max_attempts`) and against the caller's address and IP, so a code
    cannot be searched for by asking for a new one every time.
    """
    _refuse_unavailable(payload)
    await _within_limits(session, rate_limit.otp_verify_limits(), request, payload.identifier)

    try:
        tokens = await auth_service.verify_and_login(
            session, payload.identifier, payload.code, is_email=payload.email is not None
        )
        return _deliver(tokens, request, response)
    except StaffOtpBlocked as exc:
        # She proved she holds the mailbox, so this says where to sign in
        # instead. Asking for the code never reveals as much (see above).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    payload: RefreshRequest | None = None,
) -> TokenPair:
    """Trade a refresh token for a new pair — once.

    Browsers present the token as the HttpOnly cookie; other clients send it
    in the body. The token presented is spent on the spot: a copy of it, or
    a replay of it, is refused from then on — and a replay also ends every
    other session of the account, because a token that is used twice has
    been in two hands.
    """
    presented = _presented_refresh(request, payload)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    try:
        claims = decode_token(presented, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from None
    # A refresh token from before "sign out everywhere" (or before a replay
    # ended the account's sessions) is refused like any other old token.
    with contextlib.suppress(ValueError):
        current = await session.scalar(
            select(User.session_generation).where(User.id == uuid.UUID(str(claims.get("sub"))))
        )
        if not token_revocation.generation_matches(claims, current):
            _clear_session_cookie(response)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
            )
    # Spent first, and committed on its own: of two requests racing with the
    # same token only one inserts the revocation row, and only that one is
    # answered. A failure after this point costs that client a sign-in, which
    # is the safe side of the trade.
    if not await token_revocation.revoke(session, claims, reason="rotated"):
        security_log.event("auth.refresh_replayed", user_id=str(claims.get("sub")))
        with contextlib.suppress(ValueError):
            await token_revocation.revoke_all(
                session, uuid.UUID(str(claims.get("sub"))), reason="reused"
            )
        await session.commit()
        _clear_session_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    await session.commit()

    try:
        tokens = await auth_service.refresh_tokens(session, claims["sub"])
    except AuthError as exc:
        _clear_session_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    await session.commit()
    # The body carries the refresh token only for the client that sent it in
    # the body; a cookie session stays a cookie session.
    if payload is not None and payload.refresh_token:
        _set_session_cookie(response, tokens.refresh_token or "")
        return tokens
    return _deliver(tokens, request, response)


_bearer = HTTPBearer(auto_error=False)


@router.post("/logout", response_model=Message)
async def logout(
    user: CurrentUserDep,
    request: Request,
    response: Response,
    session: DbSession,
    payload: LogoutRequest | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Message:
    """Sign out, on the server as well as in the browser.

    The access token that made this call is revoked, and so is the refresh
    token — the cookie, or the one in the body — when it belongs to the same
    account. The cookie is cleared. Either token presented again is a 401.
    """
    if credentials is not None:
        with contextlib.suppress(jwt.PyJWTError):
            await token_revocation.revoke(
                session, decode_token(credentials.credentials, "access"), reason="signed_out"
            )
    presented = _presented_refresh(request, payload)
    if presented:
        with contextlib.suppress(jwt.PyJWTError):
            claims = decode_token(presented, "refresh")
            if str(claims.get("sub")) == user.id:
                await token_revocation.revoke(session, claims, reason="signed_out")
    await token_revocation.forget_expired(session)
    await session.commit()
    _clear_session_cookie(response)
    return Message(detail="Logged out")


@router.post("/logout-all", response_model=Message)
async def logout_everywhere(
    user: CurrentUserDep, response: Response, session: DbSession
) -> Message:
    """End every session of this account — every device, every tab.

    For the woman who signed in on a shared handset and left, or who suspects
    a copy of her session is in somebody else's hands. Tokens issued before
    this moment are refused from now on; she signs in again on this device.
    """
    await token_revocation.revoke_all(session, uuid.UUID(user.id), reason="signed_out_all")
    await session.commit()
    _clear_session_cookie(response)
    security_log.event("auth.signed_out_everywhere", user_id=user.id, level=20)
    return Message(detail="Signed out everywhere")
