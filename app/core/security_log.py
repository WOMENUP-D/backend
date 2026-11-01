"""Security events, written where an operator can alert on them.

One logger, one shape: `event` is the name an alert rule matches on,
everything else is context. Never a password, a code, a token or a full
identifier — addresses are masked before they get here, and the request id
ties the line to the rest of the request's log. Operators forward the
`security` logger to their alerting (see SECURITY.md for the rules worth
having).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.security import mask_email, mask_phone

logger = logging.getLogger("security")


def masked(identifier: str | None) -> str | None:
    if not identifier:
        return None
    if "@" in identifier:
        return mask_email(identifier)
    if identifier.startswith("+"):
        return mask_phone(identifier)
    return identifier[:2] + "…"


def event(name: str, *, level: int = logging.WARNING, **fields: Any) -> None:
    """Record one security-relevant event.

    `name` is stable and dotted (`auth.login_failed`, `auth.token_revoked_use`,
    `ai.limit_reached`, `api.rate_limited`, `admin.role_changed`); the fields
    are small scalars.
    """
    logger.log(
        level,
        name,
        extra={
            "extra_fields": {"event": name, **{k: v for k, v in fields.items() if v is not None}}
        },
    )
