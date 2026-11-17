"""`POST /assistant/ask/stream` — the same door as `/ask`, streamed.

The fake gateway below writes its JSON a few characters at a time through
`on_text`, the way the provider's stream does, so these tests see the deltas
an actual answer would produce without calling a model.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.api import assistant as assistant_api
from app.core.config import settings
from app.models.audit import AiInteraction
from app.services import ai_assistant
from app.services.llm_gateway import LlmResponse, LlmUnavailableError, llm_gateway

API = "/api/v1/assistant"


def _events(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body: `(event, data)` pairs, comments dropped."""
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def _streamed_text(events: list[tuple[str, dict]]) -> str:
    return "".join(data["text"] for event, data in events if event == "delta")


class _Gateway:
    """Writes `payload` as JSON, in `chunk`-sized pieces when streamed."""

    enabled = True

    def __init__(self, payload: dict, *, chunk: int = 4, fail_after: int | None = None):
        self.payload = payload
        self.chunk = chunk
        self.fail_after = fail_after
        self.calls: list[dict] = []

    @staticmethod
    def sanitise(text: str) -> str:
        return text

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        raw = json.dumps(self.payload, ensure_ascii=False)
        on_text = kwargs.get("on_text")
        if on_text is not None:
            for index in range(0, len(raw), self.chunk):
                if self.fail_after is not None and index >= self.fail_after:
                    raise LlmUnavailableError("stream broke")
                await on_text(raw[index : index + self.chunk])
        elif self.fail_after is not None:
            raise LlmUnavailableError("model down")
        return LlmResponse(
            text=raw,
            parsed=json.loads(raw),
            model="test-model",
            prompt_version="test-prompt",
            trace_id=uuid.uuid4().hex,
            latency_ms=5,
            input_tokens=10,
            output_tokens=20,
        )


@pytest.fixture
def gateway(monkeypatch):
    def install(payload: dict, **options) -> _Gateway:
        fake = _Gateway(payload, **options)
        monkeypatch.setattr(ai_assistant, "llm_gateway", fake)
        return fake

    return install


EDUCATION = {
    "answer": (
        "Sizga dasturlash yo'nalishi mos keladi. Avval o'qish rejasini tuzing, "
        "keyin shu kurslardan birini tanlang — ishonch bilan boshlang!"
    ),
    "kind": "profession_match",
    "next_actions": ["Kursni ko'rish", "Rejani tuzish"],
}
QUESTION = {"message": "Qaysi kasbni tanlasam bo'ladi?", "language": "uz", "route": "auto"}


def _comparable(reply: dict) -> dict:
    return {key: value for key, value in reply.items() if key != "trace_id"}


@pytest.mark.asyncio
async def test_text_streams_then_one_final_reply_equal_to_ask(
    client, session, auth_headers, gateway
):
    fake = gateway(EDUCATION)
    streamed = await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert streamed.headers["x-accel-buffering"] == "no"
    assert "no-cache" in streamed.headers["cache-control"]
    assert streamed.text.startswith(": open")

    events = _events(streamed.text)
    kinds = [event for event, _ in events]
    assert kinds[-1] == "reply" and kinds.count("reply") == 1
    assert set(kinds[:-1]) == {"delta"} and len(kinds) > 3
    final = events[-1][1]

    plain = await client.post(f"{API}/ask", json=QUESTION, headers=auth_headers)
    assert plain.status_code == 200
    assert _comparable(final) == _comparable(plain.json())
    assert final["route"] == "education"
    assert final["next_actions"] == ["Kursni köriş", "Rejani tuziş"]

    # The deltas only ever append, and they are already the final alphabet.
    text = _streamed_text(events)
    assert final["answer"].startswith(text)
    assert len(text) >= len(final["answer"]) - len("boshlang!")
    assert "yönaliş" in text and "o'q" not in text and "sh" not in text.replace("ş", "")

    # The streamed call was the same model call: same request bar `on_text`.
    stream_call, plain_call = fake.calls
    assert "on_text" in stream_call and "on_text" not in plain_call
    assert {k: v for k, v in stream_call.items() if k != "on_text"} == plain_call

    traces = (
        await session.scalars(
            select(AiInteraction).where(AiInteraction.feature == "assistant_education")
        )
    ).all()
    assert len(traces) == 2
    assert {t.model_version for t in traces} == {"test-model"}
    assert {t.prompt_version for t in traces} == {"test-prompt"}
    assert final["trace_id"] in {t.trace_id for t in traces}


@pytest.mark.asyncio
async def test_a_russian_answer_on_the_uzbek_page_is_not_transliterated(
    client, auth_headers, gateway
):
    gateway(
        {
            "answer": "Вам подойдёт программирование. Начните с курса и плана обучения.",
            "kind": "general",
            "next_actions": [],
        }
    )
    body = {"message": "Какую профессию выбрать?", "language": "uz", "route": "education"}
    events = _events((await client.post(f"{API}/ask/stream", json=body, headers=auth_headers)).text)
    final = events[-1][1]
    assert final["answer"].startswith("Вам подойдёт программирование.")
    assert final["answer"].startswith(_streamed_text(events))


@pytest.mark.asyncio
async def test_health_streams_too(client, auth_headers, gateway):
    gateway(
        {
            "answer": "Yaxshi uyqu uchun har kuni bir vaqtda yoting va ekranni chetga qo'ying.",
            "see_a_doctor": False,
            "out_of_scope": False,
            "next_actions": [],
        }
    )
    body = {"message": "Uyqum yomon, nima qilay?", "language": "uz", "route": "health"}
    events = _events((await client.post(f"{API}/ask/stream", json=body, headers=auth_headers)).text)
    final = events[-1][1]
    assert final["route"] == "health"
    assert final["answer"].startswith("Yaxşi uyqu uçun")
    assert _streamed_text(events) and final["answer"].startswith(_streamed_text(events))


@pytest.mark.asyncio
async def test_a_disclosure_gets_the_safety_reply_and_no_model_call(
    client, auth_headers, monkeypatch
):
    calls: list[str] = []

    class Forbidden(_Gateway):
        async def complete(self, **kwargs):
            calls.append("model")
            raise AssertionError("the model was called on a safety disclosure")

    async def classify(*args, **kwargs):
        calls.append("classify")
        raise AssertionError("the router saw a safety disclosure")

    monkeypatch.setattr(ai_assistant, "llm_gateway", Forbidden({}))
    monkeypatch.setattr(ai_assistant, "classify", classify)
    body = {"message": "Erim meni har kuni urishadi", "language": "uz"}
    response = await client.post(f"{API}/ask/stream", json=body, headers=auth_headers)
    assert response.status_code == 200
    events = _events(response.text)
    assert [event for event, _ in events] == ["reply"]
    final = events[0][1]
    assert final["route"] == "safety"
    assert final["escalation_reason"] == "safety_topic_detected"
    assert final["resources"][0]["tel"] == "tel:1146"
    assert calls == []


@pytest.mark.asyncio
async def test_a_guest_out_of_questions_still_gets_the_safety_reply(client, monkeypatch, gateway):
    gateway(EDUCATION)
    monkeypatch.setattr(settings, "assistant_guest_questions", 1)
    guest = {"X-Guest-Id": f"g-{uuid.uuid4().hex}"}
    first = await client.post(f"{API}/ask/stream", json=QUESTION, headers=guest)
    assert first.status_code == 200
    assert _events(first.text)[-1][1]["guest_questions_left"] == 0

    spent = await client.post(f"{API}/ask/stream", json=QUESTION, headers=guest)
    assert spent.status_code == 429
    assert spent.json()["detail"] == "guest_limit_reached"

    disclosure = {"message": "Меня бьют дома", "language": "ru"}
    safe = await client.post(f"{API}/ask/stream", json=disclosure, headers=guest)
    assert safe.status_code == 200
    assert _events(safe.text)[-1][1]["route"] == "safety"


@pytest.mark.asyncio
async def test_the_ai_limit_is_the_same_429(client, auth_headers, monkeypatch, gateway):
    gateway(EDUCATION)
    monkeypatch.setattr(settings, "ai_user_hourly_limit", 1)
    assert (await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)).is_success
    second = await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)
    assert second.status_code == 429
    assert second.json()["detail"] == "ai_limit_reached"


@pytest.mark.asyncio
async def test_model_unavailable_is_the_degraded_answer_as_one_reply(client, auth_headers):
    # The suite's gateway has no key: the documented fallback, over the stream.
    events = _events(
        (await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)).text
    )
    assert [event for event, _ in events] == ["reply"]
    final = events[0][1]
    assert final["degraded"] is True
    assert final["escalation_reason"] == "model_unavailable"


@pytest.mark.asyncio
async def test_a_stream_broken_mid_answer_ends_in_the_degraded_reply(
    client, session, auth_headers, gateway
):
    gateway(EDUCATION, fail_after=60)
    events = _events(
        (await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)).text
    )
    kinds = [event for event, _ in events]
    assert kinds[0] == "delta" and kinds[-1] == "reply"
    final = events[-1][1]
    assert final["degraded"] is True
    # The fallback is traced as a fallback, as on the plain path.
    trace = await session.scalar(
        select(AiInteraction).where(AiInteraction.trace_id == final["trace_id"])
    )
    assert trace is not None and trace.prompt_version == "education-fallback"


@pytest.mark.asyncio
async def test_a_portal_answer_is_delivered_whole(client, auth_headers, monkeypatch, gateway):
    gateway(EDUCATION)
    seen: list = []

    async def answer(session, **kwargs):
        seen.append(kwargs)
        from app.schemas.ai import NavigatorAnswer

        return NavigatorAnswer(
            answer="Sertifikat kabinetda.", confidence=0.9, trace_id=uuid.uuid4().hex
        )

    monkeypatch.setattr(ai_assistant.ai_navigator, "answer", answer)
    body = {"message": "Sertifikatni qayerdan olaman?", "language": "uz", "route": "portal"}
    events = _events((await client.post(f"{API}/ask/stream", json=body, headers=auth_headers)).text)
    assert [event for event, _ in events] == ["reply"]
    assert events[0][1]["route"] == "portal"
    assert "on_text" not in seen[0] and "on_answer" not in seen[0]


@pytest.mark.asyncio
async def test_an_unexpected_failure_ends_with_an_error_event(
    client, session, auth_headers, monkeypatch
):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_assistant, "ask", broken)
    response = await client.post(f"{API}/ask/stream", json=QUESTION, headers=auth_headers)
    assert response.status_code == 200
    assert _events(response.text) == [("error", {"detail": "assistant_failed"})]
    assert await session.scalar(select(func.count()).select_from(AiInteraction)) == 0


@pytest.mark.asyncio
async def test_expired_token_is_a_401_before_the_stream(client):
    response = await client.post(
        f"{API}/ask/stream", json=QUESTION, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


def test_the_stream_is_an_expensive_path():
    from app.core.throttle import EXPENSIVE_PREFIXES

    assert any("/assistant/ask/stream".startswith(prefix) for prefix in EXPENSIVE_PREFIXES)


def test_live_text_releases_whole_words_in_the_final_alphabet():
    live = assistant_api._LiveText("uz")
    pieces = ["Bosh", "lang", "ich ", "o'q", "ish ", "uchun"]
    out = "".join(live.push(piece) for piece in pieces)
    assert out == "Boşlangiç öqiş "
    assert assistant_api._uz(live.raw, "uz").startswith(out)


def test_live_text_leaves_english_alone():
    live = assistant_api._LiveText("uz")
    out = "".join(live.push(p) for p in ["Choose the ", "best course for ", "your goals. "])
    assert out == "Choose the best course for your goals. "


def test_llm_gateway_is_not_called_by_the_suite():
    assert llm_gateway.enabled is False
