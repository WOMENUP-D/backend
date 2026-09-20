"""Whose attempts the sign-in throttles are counting.

Behind the ingress every request carries the proxy's address, so a throttle
keyed on it counts the whole country as one visitor: a script with eight wrong
passwords a minute locked everybody out of password sign-in, and the staff
endpoint needed five. The throttles key on the address the proxy reports
instead.
"""

from __future__ import annotations

import uuid

import pytest

from app.api import auth as auth_api

LOGIN = "/api/v1/auth/login"
STAFF = "/api/v1/auth/staff/login"


def wrong_password(email: str = "nobody@example.uz") -> dict:
    return {"email": email, "password": "not-the-password"}


async def attempt(client, ip: str, path: str = LOGIN) -> int:
    body = wrong_password() if path == LOGIN else {"login": "nobody", "password": "wrong-one"}
    response = await client.post(path, json=body, headers={"X-Forwarded-For": ip})
    return response.status_code


@pytest.fixture(autouse=True)
def fresh_throttles():
    """Each test starts with empty counters, whatever ran before it."""
    for throttle in (auth_api._login_throttle, auth_api._staff_throttle):
        throttle._failures.clear()
    yield
    for throttle in (auth_api._login_throttle, auth_api._staff_throttle):
        throttle._failures.clear()


@pytest.mark.asyncio
async def test_one_caller_cannot_lock_everybody_out(client):
    attacker = "203.0.113.9"
    for _ in range(12):
        await attempt(client, attacker)

    # The attacker is refused...
    assert await attempt(client, attacker) == 429
    # ...and a woman signing in from anywhere else is not.
    assert await attempt(client, "198.51.100.4") == 401


@pytest.mark.asyncio
async def test_the_allowance_follows_one_address(client):
    caller = f"198.51.100.{uuid.uuid4().int % 200}"
    refused = False
    for _ in range(12):
        if await attempt(client, caller) == 429:
            refused = True
            break
    assert refused, "the throttle never refused a caller who kept guessing"


@pytest.mark.asyncio
async def test_the_staff_endpoint_counts_per_address_too(client):
    attacker = "203.0.113.77"
    for _ in range(8):
        await attempt(client, attacker, STAFF)

    assert await attempt(client, attacker, STAFF) == 429
    assert await attempt(client, "198.51.100.90", STAFF) == 401
