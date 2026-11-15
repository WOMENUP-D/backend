"""A plan follows the language she reads the site in (translated, not regenerated)."""

from __future__ import annotations

import json
import uuid

from app.core.constants import GoalHorizon
from app.models.audit import AiInteraction
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.user import User
from app.services.llm_gateway import LlmResponse, LlmUnavailableError, llm_gateway

API = "/api/v1/plans"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _russian_plan(session, user_id, *, language_recorded: bool = True) -> DevelopmentPlan:
    plan = DevelopmentPlan(
        user_id=user_id,
        horizon=GoalHorizon.M6,
        title="Стартовый маршрут на 6 месяцев",
        summary="Сначала диагностика, затем базовые навыки.",
        is_active=True,
        rationale={"language": "ru"} if language_recorded else {},
    )
    session.add(plan)
    await session.flush()
    for index, action in enumerate(["Пройти курс по Excel", "Обновить резюме"]):
        session.add(
            PlanItem(plan_id=plan.id, order_index=index, action=action, description="Описание")
        )
    await session.flush()
    await session.refresh(plan, ["items"])
    return plan


def _fake_model(monkeypatch, *, fail: bool = False) -> list[dict]:
    """Stand in for the model: echoes ids, answers in the old Uzbek alphabet."""
    calls: list[dict] = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if fail:
            raise LlmUnavailableError("down")
        source = json.loads(kwargs["messages"][0]["content"].split("PLAN:\n", 1)[1])
        parsed = {
            "title": "6 oylik boshlang‘ich yo‘l",
            "summary": "Avval diagnostika, so‘ng asosiy ko‘nikmalar.",
            "items": [
                {"id": item["id"], "action": f"Qadam: o‘qish {n}", "description": "Tavsif"}
                for n, item in enumerate(source["items"])
            ],
        }
        return LlmResponse(
            text=json.dumps(parsed, ensure_ascii=False),
            trace_id="trace-1",
            model="test-model",
            prompt_version="test",
            latency_ms=5,
            parsed=parsed,
        )

    monkeypatch.setattr(llm_gateway, "_api_key", "test-key", raising=False)
    monkeypatch.setattr(llm_gateway, "complete", complete)
    return calls


async def test_translates_once_and_reads_use_the_cache(
    client, session, auth_headers, user_id, monkeypatch
):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _russian_plan(session, user_id)
    calls = _fake_model(monkeypatch)

    first = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "uz-Cyrl"}, headers=auth_headers
    )
    assert first.status_code == 200
    body = first.json()
    assert body["language"] == "uz" and body["content_language"] == "ru"
    assert body["translation_unavailable"] is False
    # Uzbek passes through the reformed-alphabet converter.
    assert body["title"] == "6 oylik boşlanğiç yöl"
    assert [i["action"] for i in body["items"]] == ["Qadam: öqiş 0", "Qadam: öqiş 1"]
    assert len(calls) == 1

    again = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "uz"}, headers=auth_headers
    )
    assert again.json()["title"] == body["title"]
    assert len(calls) == 1  # cached: no second model call

    active = await client.get(f"{API}/active?language=uz", headers=auth_headers)
    assert active.json()["title"] == body["title"]
    assert active.json()["items"][1]["action"] == "Qadam: öqiş 1"
    listed = await client.get(f"{API}?language=uz", headers=auth_headers)
    assert listed.json()[0]["title"] == body["title"]

    original = await client.get(f"{API}/active?language=ru", headers=auth_headers)
    assert original.json()["title"] == "Стартовый маршрут на 6 месяцев"

    traces = (await session.execute(AiInteraction.__table__.select())).all()
    assert any(row.feature == "plan_translation" for row in traces)


async def test_reading_never_translates(client, session, auth_headers, user_id, monkeypatch):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _russian_plan(session, user_id)
    calls = _fake_model(monkeypatch)

    response = await client.get(f"{API}/active?language=uz", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["title"] == "Стартовый маршрут на 6 месяцев"
    assert response.json()["language"] == "ru"
    assert calls == []
    await session.refresh(plan)
    assert plan.translations == {}


async def test_only_the_owner_can_translate(client, session, auth_headers, user_id, monkeypatch):
    session.add(User(id=user_id, phone=_phone()))
    other = User(phone=_phone())
    session.add(other)
    await session.flush()
    plan = await _russian_plan(session, other.id)
    calls = _fake_model(monkeypatch)

    response = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "uz"}, headers=auth_headers
    )
    assert response.status_code == 404
    assert calls == []


async def test_model_down_returns_the_original(client, session, auth_headers, user_id, monkeypatch):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _russian_plan(session, user_id)
    _fake_model(monkeypatch, fail=True)

    response = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "en"}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["translation_unavailable"] is True
    assert body["language"] == "ru"
    assert body["title"] == "Стартовый маршрут на 6 месяцев"
    await session.refresh(plan)
    assert plan.translations == {}


async def test_same_language_and_unknown_language(
    client, session, auth_headers, user_id, monkeypatch
):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _russian_plan(session, user_id)
    calls = _fake_model(monkeypatch)

    same = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "ru"}, headers=auth_headers
    )
    assert same.status_code == 200 and same.json()["language"] == "ru"
    bad = await client.post(
        f"{API}/{plan.id}/translate", json={"language": "de"}, headers=auth_headers
    )
    assert bad.status_code == 422
    assert calls == []


async def test_an_old_plan_is_recognised_by_its_script(session):
    user = User(phone=_phone())
    session.add(user)
    await session.flush()
    plan = await _russian_plan(session, user.id, language_recorded=False)
    assert plan.content_language == "ru"
    plan.title, plan.summary = "Reja", "Qisqa"
    assert plan.content_language == "uz"
