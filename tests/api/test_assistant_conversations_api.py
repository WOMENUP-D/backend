"""Her saved assistant conversations: hers alone, bounded, and gone with her.

Pinned here, in order of how badly it would hurt to get wrong:

* Another woman's conversation can be neither read, overwritten nor deleted —
  every attempt is the same 404 as an id that does not exist.
* Erasure and the retention sweep remove them.
* The limits hold: a conversation's size, and how many an account keeps.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.constants import Language, Role
from app.core.security import create_token
from app.models.conversation import AssistantConversation
from app.models.user import User
from app.services import assistant_history, retention

BASE = "/api/v1/assistant/conversations"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _woman(session, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    await session.flush()


def _body(title: str = "Qaysi kasb menga mos?", turns: int = 2) -> dict:
    return {
        "tab": "coach",
        "title": title,
        "turns": [
            {"id": str(i), "role": "user" if i % 2 == 0 else "assistant", "text": f"turn {i}"}
            for i in range(turns)
        ],
    }


@pytest.mark.asyncio
async def test_the_list_needs_an_account(client):
    assert (await client.get(BASE)).status_code == 401
    assert (await client.put(f"{BASE}/{uuid.uuid4()}", json=_body())).status_code == 401


@pytest.mark.asyncio
async def test_a_conversation_round_trips_and_lists_newest_first(client, session, user_id):
    await _woman(session, user_id)
    headers = _headers(user_id)
    first, second = uuid.uuid4(), uuid.uuid4()
    assert (
        await client.put(f"{BASE}/{first}", json=_body("Birinçi"), headers=headers)
    ).status_code == 200
    assert (
        await client.put(f"{BASE}/{second}", json=_body("Ikkinçi", 4), headers=headers)
    ).status_code == 200

    listed = (await client.get(BASE, headers=headers)).json()
    assert [row["title"] for row in listed] == ["Ikkinçi", "Birinçi"]
    assert listed[0]["turn_count"] == 4
    assert "turns" not in listed[0]

    read = (await client.get(f"{BASE}/{first}", headers=headers)).json()
    assert read["tab"] == "coach"
    assert [turn["text"] for turn in read["turns"]] == ["turn 0", "turn 1"]


@pytest.mark.asyncio
async def test_saving_again_replaces_the_turns(client, session, user_id):
    await _woman(session, user_id)
    headers = _headers(user_id)
    chat = uuid.uuid4()
    await client.put(f"{BASE}/{chat}", json=_body(turns=2), headers=headers)
    await client.put(f"{BASE}/{chat}", json=_body(turns=6), headers=headers)

    read = (await client.get(f"{BASE}/{chat}", headers=headers)).json()
    assert len(read["turns"]) == 6
    assert len((await client.get(BASE, headers=headers)).json()) == 1


@pytest.mark.asyncio
async def test_another_womans_conversation_is_a_plain_404(client, session, user_id):
    """Not readable, not writable over, not deletable — and not listed."""
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other)
    theirs = uuid.uuid4()
    await client.put(f"{BASE}/{theirs}", json=_body("Uning savoli"), headers=_headers(other))

    mine = _headers(user_id)
    assert (await client.get(f"{BASE}/{theirs}", headers=mine)).status_code == 404
    assert (
        await client.put(f"{BASE}/{theirs}", json=_body("Hijack"), headers=mine)
    ).status_code == 404
    assert (await client.delete(f"{BASE}/{theirs}", headers=mine)).status_code == 404
    assert (await client.get(BASE, headers=mine)).json() == []

    still = (await client.get(f"{BASE}/{theirs}", headers=_headers(other))).json()
    assert still["title"] == "Uning savoli"


@pytest.mark.asyncio
async def test_deleting_removes_it(client, session, user_id):
    await _woman(session, user_id)
    headers = _headers(user_id)
    chat = uuid.uuid4()
    await client.put(f"{BASE}/{chat}", json=_body(), headers=headers)

    assert (await client.delete(f"{BASE}/{chat}", headers=headers)).status_code == 204
    assert (await client.get(f"{BASE}/{chat}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_turns_must_say_who_spoke_and_stay_within_size(client, session, user_id):
    await _woman(session, user_id)
    headers = _headers(user_id)
    bad = _body()
    bad["turns"][0]["role"] = "system"
    assert (
        await client.put(f"{BASE}/{uuid.uuid4()}", json=bad, headers=headers)
    ).status_code == 422

    huge = _body()
    huge["turns"] = [
        {"id": str(i), "role": "user", "text": "x" * 19_000}
        for i in range(assistant_history.MAX_TURNS)
    ]
    assert (
        await client.put(f"{BASE}/{uuid.uuid4()}", json=huge, headers=headers)
    ).status_code == 413

    too_many = _body(turns=assistant_history.MAX_TURNS + 1)
    assert (
        await client.put(f"{BASE}/{uuid.uuid4()}", json=too_many, headers=headers)
    ).status_code == 422

    unknown_tab = {**_body(), "tab": "admin"}
    assert (
        await client.put(f"{BASE}/{uuid.uuid4()}", json=unknown_tab, headers=headers)
    ).status_code == 422


@pytest.mark.asyncio
async def test_over_the_cap_the_oldest_conversation_goes(client, session, user_id, monkeypatch):
    monkeypatch.setattr(assistant_history, "MAX_CONVERSATIONS", 3)
    await _woman(session, user_id)
    headers = _headers(user_id)
    for title in ["bir", "ikki", "uç", "tört"]:
        await client.put(f"{BASE}/{uuid.uuid4()}", json=_body(title), headers=headers)

    titles = [row["title"] for row in (await client.get(BASE, headers=headers)).json()]
    assert titles == ["tört", "uç", "ikki"]


@pytest.mark.asyncio
async def test_erasure_removes_her_conversations(session, user_id):
    await _woman(session, user_id)
    session.add(AssistantConversation(user_id=user_id, tab="health", title="Savol", turns=[]))
    await session.flush()

    await retention.erase_user(session, user_id)

    left = await session.scalars(
        select(AssistantConversation).where(AssistantConversation.user_id == user_id)
    )
    assert list(left) == []


@pytest.mark.asyncio
async def test_the_sweep_removes_conversations_left_untouched(session, user_id):
    await _woman(session, user_id)
    now = datetime.now(UTC)
    old = AssistantConversation(user_id=user_id, tab="auto", title="Eski", turns=[])
    fresh = AssistantConversation(user_id=user_id, tab="auto", title="Yangi", turns=[])
    session.add_all([old, fresh])
    await session.flush()
    old.updated_at = now - timedelta(days=400)
    await session.flush()

    purged = await retention.purge(session, now=now)

    assert purged.conversations == 1
    titles = [
        row.title
        for row in await session.scalars(
            select(AssistantConversation).where(AssistantConversation.user_id == user_id)
        )
    ]
    assert titles == ["Yangi"]
