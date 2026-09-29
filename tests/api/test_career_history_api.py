"""Work and study history — the profile's CV sections.

Pinned here: her entries are hers alone (another woman's id is a plain 404),
they come back in the order a CV is read, dates are month-precise and cannot
run backwards, a retried save is one entry, and none of it reaches a partner.
"""

import uuid

from app.core.constants import Language, Role
from app.core.security import create_token
from app.models.profile import Profile
from app.models.user import User
from app.services.integration_gateway import minimal_profile_payload

WORK = "/api/v1/users/me/experience"
STUDY = "/api/v1/users/me/education"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _woman(session, user_id: uuid.UUID) -> None:
    session.add(
        User(id=user_id, phone=f"+9989{uuid.uuid4().int % 10**8:08d}", language=Language.UZ)
    )
    await session.flush()


async def test_a_job_is_added_edited_and_removed(client, session, user_id):
    await _woman(session, user_id)
    created = await client.post(
        WORK,
        json={
            "organization": "  45-maktab ",
            "position": "Oʻqituvchi",
            "start_date": "2018-09-14",
            "end_date": "2024-06-30",
        },
        headers=_headers(user_id),
    )
    assert created.status_code == 201
    job = created.json()
    assert job["organization"] == "45-maktab"
    # Month-precise: the day she typed is not kept.
    assert (job["start_date"], job["end_date"]) == ("2018-09-01", "2024-06-01")

    # The form sends the whole entry back, so clearing the end date means
    # "I still work there".
    edited = await client.put(
        f"{WORK}/{job['id']}",
        json={
            "organization": "45-maktab",
            "position": "Direktor oʻrinbosari",
            "start_date": "2018-09-01",
        },
        headers=_headers(user_id),
    )
    assert edited.status_code == 200
    assert edited.json()["position"] == "Direktor oʻrinbosari"
    assert edited.json()["end_date"] is None

    removed = await client.delete(f"{WORK}/{job['id']}", headers=_headers(user_id))
    assert removed.status_code == 200
    assert (await client.get(WORK, headers=_headers(user_id))).json() == []


async def test_the_list_reads_like_a_cv(client, session, user_id):
    """What she is doing now first, then the rest, newest first."""
    await _woman(session, user_id)
    for org, start, end in (
        ("Birinchi ish", "2012-01-01", "2015-01-01"),
        ("Hozirgi ish", "2020-03-01", None),
        ("Ikkinchi ish", "2015-02-01", "2020-02-01"),
    ):
        await client.post(
            WORK,
            json={
                "organization": org,
                "position": "Buxgalter",
                "start_date": start,
                "end_date": end,
            },
            headers=_headers(user_id),
        )
    listed = (await client.get(WORK, headers=_headers(user_id))).json()
    assert [row["organization"] for row in listed] == [
        "Hozirgi ish",
        "Ikkinchi ish",
        "Birinchi ish",
    ]


async def test_an_end_before_the_start_is_refused(client, session, user_id):
    await _woman(session, user_id)
    response = await client.post(
        STUDY,
        json={"institution": "TDIU", "start_date": "2020-09-01", "end_date": "2019-06-01"},
        headers=_headers(user_id),
    )
    assert response.status_code == 422


async def test_a_blank_name_is_refused(client, session, user_id):
    await _woman(session, user_id)
    response = await client.post(
        WORK, json={"organization": "   ", "position": "x"}, headers=_headers(user_id)
    )
    assert response.status_code == 422


async def test_a_retried_save_is_one_entry(client, session, user_id):
    await _woman(session, user_id)
    body = {"institution": "TDIU", "degree": "bachelor", "client_ref": str(uuid.uuid4())}
    first = await client.post(STUDY, json=body, headers=_headers(user_id))
    second = await client.post(STUDY, json=body, headers=_headers(user_id))
    assert first.json()["id"] == second.json()["id"]
    assert len((await client.get(STUDY, headers=_headers(user_id))).json()) == 1


async def test_another_womans_entry_is_a_plain_404(client, session, user_id):
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other)
    theirs = (
        await client.post(STUDY, json={"institution": "Uning maktabi"}, headers=_headers(other))
    ).json()

    edit = await client.put(
        f"{STUDY}/{theirs['id']}", json={"institution": "Buzildi"}, headers=_headers(user_id)
    )
    delete = await client.delete(f"{STUDY}/{theirs['id']}", headers=_headers(user_id))
    listed = (await client.get(STUDY, headers=_headers(user_id))).json()

    assert edit.status_code == delete.status_code == 404
    assert listed == []


async def test_history_never_reaches_a_partner(client, session, user_id):
    """Where she worked names her more precisely than any field partners get."""
    await _woman(session, user_id)
    profile = Profile(user_id=user_id, full_name="Dilnoza")
    session.add(profile)
    await session.flush()
    await client.post(
        WORK,
        json={"organization": "«Anor» doʻkoni", "position": "Sotuvchi"},
        headers=_headers(user_id),
    )
    payload = minimal_profile_payload(await session.get(User, user_id), profile)
    assert "Anor" not in repr(payload)
