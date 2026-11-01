"""The session as a cookie: the refresh token leaves the page's reach.

Signing in sets an HttpOnly cookie scoped to the auth endpoints and keeps
the refresh token out of the body; refreshing works from the cookie alone and
rotates it; signing out clears it; a replayed refresh token ends every
session of the account; "sign out everywhere" does the same on request.
"""

from __future__ import annotations

import pytest

from app.core.config import settings

API = "/api/v1"
REGISTER = {
    "password": "long-enough-password",
    "accepted_privacy_policy": True,
}


def _cookie_header(response) -> str:
    return next(
        (value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"),
        "",
    )


@pytest.mark.asyncio
async def test_signing_in_sets_a_cookie_the_page_cannot_read(client):
    response = await client.post(
        f"{API}/auth/register", json={"email": "cookie@example.uz", **REGISTER}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] is None

    cookie = _cookie_header(response)
    assert cookie.startswith(f"{settings.auth_cookie_name}=")
    assert "HttpOnly" in cookie
    assert f"Path={API}/auth" in cookie
    assert "SameSite=lax" in cookie
    # Plain http in local development, Secure everywhere else.
    assert ("Secure" in cookie) is (settings.environment != "local")


@pytest.mark.asyncio
async def test_a_client_that_cannot_hold_cookies_may_ask_for_the_body(client):
    response = await client.post(
        f"{API}/auth/register",
        json={"email": "native@example.uz", **REGISTER},
        headers={"X-Refresh-Transport": "body"},
    )
    assert response.status_code == 201
    assert response.json()["refresh_token"]


@pytest.mark.asyncio
async def test_the_cookie_alone_refreshes_and_rotates(client):
    await client.post(f"{API}/auth/register", json={"email": "rotate@example.uz", **REGISTER})
    first_cookie = client.cookies.get(settings.auth_cookie_name)
    assert first_cookie

    refreshed = await client.post(f"{API}/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]
    assert refreshed.json()["refresh_token"] is None
    second_cookie = client.cookies.get(settings.auth_cookie_name)
    assert second_cookie and second_cookie != first_cookie

    # The spent cookie is refused — and, because a token used twice has been
    # in two hands, so is the new one: every session of the account ends.
    client.cookies.clear()

    def present(value: str) -> dict[str, str]:
        return {"Cookie": f"{settings.auth_cookie_name}={value}"}

    assert (
        await client.post(f"{API}/auth/refresh", headers=present(first_cookie))
    ).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", headers=present(second_cookie))
    ).status_code == 401


@pytest.mark.asyncio
async def test_no_token_anywhere_is_a_401_not_a_500(client):
    client.cookies.clear()
    assert (await client.post(f"{API}/auth/refresh")).status_code == 401
    assert (await client.post(f"{API}/auth/refresh", json={})).status_code == 401


@pytest.mark.asyncio
async def test_signing_out_clears_the_cookie_and_kills_the_session(client):
    registered = await client.post(
        f"{API}/auth/register", json={"email": "bye@example.uz", **REGISTER}
    )
    access = registered.json()["access_token"]
    headers = {"Authorization": f"Bearer {access}"}

    out = await client.post(f"{API}/auth/logout", headers=headers)
    assert out.status_code == 200
    cleared = _cookie_header(out)
    assert cleared.startswith(f"{settings.auth_cookie_name}=")
    assert "Max-Age=0" in cleared or "expires=" in cleared.lower()

    assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 401
    assert (await client.post(f"{API}/auth/refresh")).status_code == 401


@pytest.mark.asyncio
async def test_sign_out_everywhere_ends_the_other_device_too(client):
    phone = await client.post(
        f"{API}/auth/register",
        json={"email": "two@example.uz", **REGISTER},
        headers={"X-Refresh-Transport": "body"},
    )
    phone_tokens = phone.json()
    laptop = await client.post(
        f"{API}/auth/login",
        json={"email": "two@example.uz", "password": REGISTER["password"]},
        headers={"X-Refresh-Transport": "body"},
    )
    laptop_tokens = laptop.json()
    assert laptop.status_code == 200

    out = await client.post(
        f"{API}/auth/logout-all",
        headers={"Authorization": f"Bearer {laptop_tokens['access_token']}"},
    )
    assert out.status_code == 200

    # Both the phone's access token and its refresh token are refused now.
    assert (
        await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {phone_tokens['access_token']}"}
        )
    ).status_code == 401
    assert (
        await client.post(
            f"{API}/auth/refresh", json={"refresh_token": phone_tokens["refresh_token"]}
        )
    ).status_code == 401
    # A fresh sign-in opens a new session as usual.
    again = await client.post(
        f"{API}/auth/login", json={"email": "two@example.uz", "password": REGISTER["password"]}
    )
    assert again.status_code == 200
    assert (
        await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {again.json()['access_token']}"}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_an_account_is_held_after_too_many_wrong_passwords_from_anywhere(client, monkeypatch):
    monkeypatch.setattr(settings, "login_failures_per_hour", 2)
    await client.post(f"{API}/auth/register", json={"email": "held@example.uz", **REGISTER})
    wrong = {"email": "held@example.uz", "password": "not-the-password"}
    # Each attempt from a "different" address, as a distributed guess would be.
    for n in range(2):
        assert (
            await client.post(
                f"{API}/auth/login", json=wrong, headers={"X-Forwarded-For": f"203.0.113.{n}"}
            )
        ).status_code == 401
    # The third attempt — with the *right* password — is held: the account,
    # not the address, has used up its allowance. Same message as a wrong
    # password, so the hold does not confirm the account exists.
    held = await client.post(
        f"{API}/auth/login",
        json={"email": "held@example.uz", "password": REGISTER["password"]},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert held.status_code == 401
    assert held.json()["detail"] == "invalid email or password"


@pytest.mark.asyncio
async def test_a_password_everyone_tries_first_is_refused(client):
    for password in ("password123", "12345678", "womanup2026", "Qwerty123"):
        response = await client.post(
            f"{API}/auth/register",
            json={
                "email": "weak@example.uz",
                "password": password,
                "accepted_privacy_policy": True,
            },
        )
        assert response.status_code == 422, password
    # Her own address is not a password either.
    assert (
        await client.post(
            f"{API}/auth/register",
            json={
                "email": "malika.y@example.uz",
                "password": "malika.y",
                "accepted_privacy_policy": True,
            },
        )
    ).status_code == 422
    assert (
        await client.post(
            f"{API}/auth/register",
            json={
                "email": "strong@example.uz",
                "password": "olma-daraxti-2026",
                "accepted_privacy_policy": True,
            },
        )
    ).status_code == 201


@pytest.mark.asyncio
async def test_a_host_the_api_does_not_serve_is_refused(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    monkeypatch.setattr(settings, "allowed_hosts", ["womanup.uz", "localhost", "test"])
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/health")).status_code == 200
        forged = await http.get("/health", headers={"Host": "evil.example"})
        assert forged.status_code == 400
