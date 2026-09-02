"""Shared FastAPI dependencies: authentication and RBAC."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ROLE_PERMISSIONS, Permission, Role
from app.core.logging import user_id_ctx
from app.core.security import decode_token
from app.db import get_session
from app.schemas.auth import CurrentUser

bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from None
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from None

    user = CurrentUser(
        id=payload["sub"],
        role=Role(payload["role"]),
        roles=[Role(r) for r in payload.get("roles", [])],
    )
    user_id_ctx.set(user.id)
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> CurrentUser | None:
    """The caller when signed in, `None` when browsing anonymously.

    For endpoints that are open to visitors but behave differently once she has
    an account. A token that is present but broken is still an error: silently
    demoting an expired session to anonymous hides the real problem from her.
    """
    if credentials is None:
        return None
    return await get_current_user(credentials)


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


def client_ip(request: Request) -> str | None:
    """Client IP, honouring the proxy header set by the ingress."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
