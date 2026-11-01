"""Shared FastAPI dependencies: authentication and RBAC."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security_log
from app.core.constants import ROLE_PERMISSIONS, Permission, Region, Role, UserStatus
from app.core.logging import user_id_ctx
from app.core.security import decode_token
from app.db import get_session
from app.models.user import User, UserRole
from app.schemas.auth import CurrentUser
from app.services import token_revocation

bearer = HTTPBearer(auto_error=False)

# `scope="function"`: the session commits when the endpoint has returned and
# its response is serialised — before the response is sent. With the default
# request scope the commit ran after the client already had its answer, so a
# client that created something and asked for it straight away could be told
# it did not exist.
DbSession = Annotated[AsyncSession, Depends(get_session, scope="function")]


#: The roles a woman using the portal holds. Every other role is a desk, and a
#: desk's authority is re-read from the database on every request.
PARTICIPANT_ROLES = frozenset({Role.USER, Role.MOTHER})

BLOCKED_STATUSES = (UserStatus.SUSPENDED, UserStatus.DELETED)


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _verified(session: AsyncSession, payload: dict) -> CurrentUser:
    """The caller the token names, checked against what the database says now.

    The token is proof of who signed in; it is not the last word on whether
    she may still act. Three things are read back: that the token has not been
    revoked (signed out, or a refresh token already spent), that the account is
    not suspended or deleted, and — for a desk account — that every staff role
    the token carries is still granted. A revoked role falls away at once
    instead of surviving until the token expires; an account that no longer
    exists keeps nothing but a participant's standing.
    """
    if await token_revocation.is_revoked(session, payload):
        security_log.event("auth.revoked_token_used", user_id=str(payload.get("sub")))
        raise _unauthorised("Token revoked")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except KeyError, ValueError:
        raise _unauthorised("Invalid token") from None
    role = Role(payload["role"])
    roles = [Role(r) for r in payload.get("roles", [])]

    account = (
        await session.execute(
            select(User.status, User.session_generation).where(User.id == user_id)
        )
    ).first()
    if account is not None and account[0] in BLOCKED_STATUSES:
        security_log.event("auth.blocked_account_used", user_id=str(user_id))
        raise _unauthorised("Account is not active")
    if account is not None and not token_revocation.generation_matches(payload, account[1]):
        # Signed out everywhere, or a refresh token of hers was replayed:
        # every token from before that moment is refused.
        security_log.event("auth.revoked_token_used", user_id=str(user_id), scope="account")
        raise _unauthorised("Token revoked")

    claimed_staff = {r for r in (role, *roles) if r not in PARTICIPANT_ROLES}
    if claimed_staff:
        granted = set(
            (
                await session.scalars(
                    select(UserRole.role).where(
                        UserRole.user_id == user_id, UserRole.role.in_(list(claimed_staff))
                    )
                )
            ).all()
        )
        if claimed_staff - granted:
            security_log.event(
                "auth.stale_role_claim",
                user_id=str(user_id),
                claimed=sorted(r.value for r in claimed_staff - granted),
            )
        roles = [r for r in roles if r in PARTICIPANT_ROLES or r in granted]
        if role not in PARTICIPANT_ROLES and role not in granted:
            role = next((r for r in roles if r in granted), Role.USER)

    return CurrentUser(id=str(user_id), role=role, roles=roles)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> CurrentUser:
    if credentials is None:
        raise _unauthorised("Authentication required")
    try:
        payload = decode_token(credentials.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise _unauthorised("Token expired") from None
    except jwt.PyJWTError:
        raise _unauthorised("Invalid token") from None

    user = await _verified(session, payload)
    user_id_ctx.set(user.id)
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> CurrentUser | None:
    """The caller when signed in, `None` when browsing anonymously.

    For endpoints that are open to visitors but behave differently once she has
    an account. A token that is present but broken is still an error: silently
    demoting an expired session to anonymous hides the real problem from her.
    """
    if credentials is None:
        return None
    return await get_current_user(credentials, session)


OptionalUserDep = Annotated[CurrentUser | None, Depends(get_optional_user)]


def require_roles(*roles: Role) -> Callable[[CurrentUser], CurrentUser]:
    """Restrict an endpoint to the given roles."""

    def dependency(user: CurrentUserDep) -> CurrentUser:
        if not user.has_role(*roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role for this operation",
            )
        return user

    return dependency


def require_permission(permission: Permission) -> Callable[[CurrentUser], CurrentUser]:
    """Restrict an endpoint to roles holding a given permission."""

    def dependency(user: CurrentUserDep) -> CurrentUser:
        granted = set().union(
            *(ROLE_PERMISSIONS.get(r, frozenset()) for r in {user.role, *user.roles})
        )
        if permission not in granted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission.value}' required",
            )
        return user

    return dependency


AdminDep = Annotated[CurrentUser, Depends(require_roles(Role.ADMIN))]
StaffDep = Annotated[
    CurrentUser,
    Depends(require_roles(Role.ADMIN, Role.REGIONAL_COORDINATOR, Role.MODERATOR)),
]
ContentDep = Annotated[
    CurrentUser, Depends(require_roles(Role.ADMIN, Role.TRAINER, Role.MODERATOR))
]


async def coordinator_region(session: AsyncSession, user: CurrentUser) -> Region | None:
    """The region a regional coordinator is limited to, read from the database.

    The access token carries roles, not regions, so `CurrentUser.region` is
    never set from it; the scope lives on the role assignment
    (`user_roles.scope_region`) and nowhere else. It used to fall back to the
    region on her own account — which she can set herself, to any region or to
    none, through the profile endpoints; a coordinator could widen her view to
    the whole country in one request. Administrators and moderators work
    nationally and get `None`. A coordinator nobody has scoped is refused:
    a desk with no district is a configuration error, not national access.
    """
    if user.has_role(Role.ADMIN, Role.MODERATOR) or not user.has_role(Role.REGIONAL_COORDINATOR):
        return None
    scoped = await session.scalar(
        select(UserRole.scope_region).where(
            UserRole.user_id == user.id,
            UserRole.role == Role.REGIONAL_COORDINATOR,
            UserRole.scope_region.is_not(None),
        )
    )
    if scoped is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"reason": "no_region_scope"}
        )
    return scoped


def client_ip(request: Request) -> str | None:
    """Client IP, honouring the proxy header set by the ingress.

    The *last* address in `X-Forwarded-For`, not the first. The ingress
    appends (or sets) the address it saw; anything before that is whatever the
    client chose to send. Reading the first entry let a client pick its own
    identity for every per-address limit on the API with one header.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else None


# --- Analytics -------------------------------------------------------------
#
# What a request says about who is acting, for the events the server records
# beside the records it writes. Imported here so an endpoint declares one
# dependency and never reads a cookie or a header itself.

from app.services.telemetry import Actor, actor_of  # noqa: E402


async def track_actor(request: Request, user: OptionalUserDep) -> Actor:
    return actor_of(request, user)


TrackerDep = Annotated[Actor, Depends(track_actor)]

# National analytics — traffic, funnels, errors — are for the roles that work
# nationally. A regional coordinator reads her region's figures on the Results
# dashboard, which scopes them; these screens have no region to scope to.
NationalStaffDep = Annotated[CurrentUser, Depends(require_roles(Role.ADMIN, Role.MODERATOR))]
