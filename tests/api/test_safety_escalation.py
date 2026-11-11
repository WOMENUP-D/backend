"""The safety gate end to end (A-02, A-03, I-02, I-03, I-15, I-16, C-28, D-23).

A disclosure never reaches the model or the router, is answered whatever the
quotas say, raises a coordinator signal for a signed-in woman (flag,
notification, audit row) without restricting her, tells a guest honestly that
nothing was passed on, and leaves a trace with a category instead of her words.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.constants import (
    ConsentScope,
    DataClassification,
    Language,
    NotificationTrigger,
    Region,
    RiskFlagType,
    Role,
    UserStatus,
)
from app.core.security import create_token
from app.models.audit import AiInteraction, AuditLog, RiskFlag
from app.models.consent import ConsentLog
from app.models.notification import Notification
from app.models.user import User, UserRole
from app.services import ai_assistant, ai_coach, ai_navigator, rag, retention, safety
from app.services.llm_gateway import LlmResponse, llm_gateway

API = "/api/v1"
DISCLOSURE = "Erim meni urishadi, nima qilay?"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _user(
    session, *, region: Region | None = Region.TASHKENT_CITY, role: Role = Role.USER
) -> User:
    user = User(
        id=uuid.uuid4(),
        phone=_phone(),
        language=Language.UZ,
        status=UserStatus.ACTIVE,
        region=region,
    )
    session.add(user)
    await session.flush()
    session.add(
        UserRole(
            user_id=user.id,
            role=role,
            scope_region=region if role is Role.REGIONAL_COORDINATOR else None,
        )
    )
    await session.flush()
    return user


def _headers(user: User, role: Role = Role.USER) -> dict[str, str]:
    token = create_token(user.id, role, "access", {"roles": [role.value]})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def model_forbidden(monkeypatch):
    """Turn the model on, and fail the test if anything calls it."""
    calls: list[str] = []

    async def complete(*args, **kwargs):
        calls.append(kwargs.get("system", "")[:40])
        raise AssertionError("the model was called on a safety disclosure")

    monkeypatch.setattr(llm_gateway, "_api_key", "test-key", raising=False)
    monkeypatch.setattr(llm_gateway, "complete", complete)

    async def classify(*args, **kwargs):
        calls.append("classify")
        raise AssertionError("the router saw a safety disclosure")

    monkeypatch.setattr(ai_assistant, "classify", classify)
    return calls


# --------------------------------------------------------------- signed in


@pytest.mark.asyncio
async def test_a_disclosure_flags_her_coordinator_and_never_reaches_the_model(
    client, session, model_forbidden
):
    coordinator = await _user(session, role=Role.REGIONAL_COORDINATOR)
    woman = await _user(session)

    response = await client.post(
        f"{API}/assistant/ask",
        json={"message": DISCLOSURE, "language": "uz"},
        headers=_headers(woman),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "safety"
    assert body["escalated"] is True
    assert body["escalation_reason"] == "safety_topic_detected"
    assert model_forbidden == []
    # Honest: it says staff were notified (they were), not "I sent it".
    assert "yubordim" not in body["answer"]
    assert "1146" in body["answer"]
    assert body["resources"] == [
        {"id": "trust_line_1146", "phone": "1146", "tel": "tel:1146", "label": "Işonç telefoni"}
    ]

    flag = await session.scalar(select(RiskFlag).where(RiskFlag.user_id == woman.id))
    assert flag is not None
    assert flag.flag_type is RiskFlagType.SAFETY_ESCALATION
    assert flag.severity == "high"
    assert flag.notified_coordinator_id == coordinator.id
    assert flag.evidence["category"] == "physical_violence"
    assert "urishadi" not in str(flag.evidence) and "urishadi" not in (flag.reason or "")

    note = await session.scalar(select(Notification).where(Notification.user_id == coordinator.id))
    assert note is not None
    assert note.trigger is NotificationTrigger.SAFETY_ESCALATION
    assert note.payload["risk_flag_id"] == str(flag.id)
    assert "urishadi" not in note.body

    audit = await session.scalar(select(AuditLog).where(AuditLog.action == "safety.escalation"))
    assert audit is not None
    assert audit.actor_id == woman.id
    assert audit.classification is DataClassification.SENSITIVE
    assert audit.entity_id == str(flag.id)

    trace = await session.scalar(select(AiInteraction).where(AiInteraction.user_id == woman.id))
    assert trace.model_version == "guardrail"
    assert trace.prompt_version == "safety-filter"
    assert trace.prompt_excerpt == "[safety:physical_violence]"


@pytest.mark.asyncio
async def test_a_second_disclosure_is_audited_without_a_second_flag(client, session):
    await _user(session, role=Role.REGIONAL_COORDINATOR)
    woman = await _user(session)
    for _ in range(2):
        response = await client.post(
            f"{API}/assistant/ask",
            json={"message": "Meni urib ketdi", "language": "uz"},
            headers=_headers(woman),
        )
        assert response.status_code == 200

    flags = await session.scalar(
        select(func.count()).select_from(RiskFlag).where(RiskFlag.user_id == woman.id)
    )
    audits = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "safety.escalation")
    )
    notes = await session.scalar(select(func.count()).select_from(Notification))
    assert (flags, audits, notes) == (1, 2, 1)


@pytest.mark.asyncio
async def test_the_flag_never_restricts_her(client, session):
    woman = await _user(session)
    headers = _headers(woman)
    await client.post(
        f"{API}/assistant/ask", json={"message": DISCLOSURE, "language": "uz"}, headers=headers
    )
    after = await client.post(
        f"{API}/assistant/ask",
        json={"message": "Qaysi kursni tanlay?", "language": "uz"},
        headers=headers,
    )
    assert after.status_code == 200
    assert after.json()["route"] != "safety"
    assert (await client.get(f"{API}/assistant/coach", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_without_a_regional_coordinator_the_administrators_are_told(client, session):
    admin = await _user(session, region=None, role=Role.ADMIN)
    woman = await _user(session, region=None)
    await client.post(
        f"{API}/assistant/ask",
        json={"message": "I want to end my life", "language": "en"},
        headers=_headers(woman),
    )
    flag = await session.scalar(select(RiskFlag).where(RiskFlag.user_id == woman.id))
    assert flag.notified_coordinator_id == admin.id
    assert flag.reason == "self_harm"


# ------------------------------------------------------------------- guests


@pytest.mark.asyncio
async def test_a_guest_is_told_honestly_that_nothing_was_passed_on(
    client, session, model_forbidden
):
    response = await client.post(
        f"{API}/assistant/ask",
        json={"message": "Меня бьют дома", "language": "ru"},
        headers={"X-Guest-Id": f"g-{uuid.uuid4().hex}"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["route"] == "safety"
    assert "никому не передано" in body["answer"]
    assert "координатор" not in body["answer"]
    assert body["resources"][0]["tel"] == "tel:1146"
    assert await session.scalar(select(func.count()).select_from(RiskFlag)) == 0
    assert model_forbidden == []


@pytest.mark.asyncio
async def test_a_guest_out_of_trial_questions_still_gets_the_safety_reply(client, monkeypatch):
    monkeypatch.setattr(settings, "assistant_guest_questions", 1)
    guest = {"X-Guest-Id": f"g-{uuid.uuid4().hex}"}
    ordinary = {"message": "WomanUP nima?", "language": "uz"}

    assert (await client.post(f"{API}/assistant/ask", json=ordinary, headers=guest)).status_code
    spent = await client.post(f"{API}/assistant/ask", json=ordinary, headers=guest)
    assert spent.status_code == 429

    for _ in range(3):
        safe = await client.post(
            f"{API}/assistant/ask",
            json={"message": DISCLOSURE, "language": "uz"},
            headers=guest,
        )
        assert safe.status_code == 200
        assert safe.json()["route"] == "safety"
        assert safe.json()["guest_questions_left"] == 0


@pytest.mark.asyncio
async def test_disclosures_do_not_spend_the_guest_allowance(client):
    guest = {"X-Guest-Id": f"g-{uuid.uuid4().hex}"}
    for _ in range(5):
        response = await client.post(
            f"{API}/assistant/health",
            json={"message": "Yaşagim kelmayapti", "language": "uz"},
            headers=guest,
        )
        assert response.status_code == 200
        assert response.json()["guest_questions_left"] == ai_assistant.guest_allowance()


@pytest.mark.asyncio
async def test_a_signed_in_woman_over_her_hourly_limit_still_gets_the_safety_reply(
    client, session, monkeypatch
):
    monkeypatch.setattr(settings, "ai_user_hourly_limit", 1)
    woman = await _user(session)
    headers = _headers(woman)
    ordinary = {"message": "WomanUP nima?", "language": "uz"}
    await client.post(f"{API}/assistant/ask", json=ordinary, headers=headers)
    assert (
        await client.post(f"{API}/assistant/ask", json=ordinary, headers=headers)
    ).status_code == 429

    for path in ("ask", "education", "health"):
        response = await client.post(
            f"{API}/assistant/{path}",
            json={"message": DISCLOSURE, "language": "uz"},
            headers=headers,
        )
        assert response.status_code == 200, path
        assert response.json()["escalated"] is True
    navigator = await client.post(
        f"{API}/ai/navigator", json={"message": "Меня бьют дома"}, headers=headers
    )
    assert navigator.status_code == 200
    assert navigator.json()["escalated_to_human"] is True


# ------------------------------------------------------- education detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("Erim meni uradi, qaysi kasbni tanlay?", "Kasb tanlash haqida"),
        ("Qaysi kasbni tanlay?", "My husband beat me yesterday"),
    ],
)
async def test_education_detail_is_gated_on_both_strings(
    client, session, model_forbidden, message, answer
):
    response = await client.post(
        f"{API}/assistant/education/detail",
        json={"message": message, "answer": answer, "kind": "profession_match", "language": "uz"},
        headers={"X-Guest-Id": f"g-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["escalated"] is True
    assert body["professions"] == [] and body["roadmap"] == []
    assert model_forbidden == []


# -------------------------------------------------------- coach, navigator


@pytest.mark.asyncio
async def test_the_coach_flags_and_labels_its_guardrail_row(session):
    woman = await _user(session)
    reply = await ai_coach.answer(
        session, user_id=woman.id, question="Отчим трогает меня", language="ru"
    )
    assert reply.escalated is True
    assert reply.resources and reply.resources[0]["phone"] == "1146"
    trace = await session.scalar(select(AiInteraction).where(AiInteraction.user_id == woman.id))
    assert trace.prompt_version == "safety-filter"
    assert trace.prompt_excerpt == "[safety:child_abuse]"
    flag = await session.scalar(select(RiskFlag).where(RiskFlag.user_id == woman.id))
    assert flag.reason == "child_abuse"


class _NeedsHuman:
    """A model that hands off, with a free-text reason."""

    enabled = True

    def __init__(self, reason: str):
        self.reason = reason

    @staticmethod
    def sanitise(text: str) -> str:
        return text

    async def complete(self, **kwargs):
        return LlmResponse(
            text="",
            parsed={
                "answer": "Men murojaatingizni mutaxassisga yubordim.",
                "confidence": 0.9,
                "needs_human": True,
                "human_reason": self.reason,
                "used_source_ids": [],
                "suggested_actions": [],
            },
            model="test-model",
            prompt_version="test",
            trace_id=uuid.uuid4().hex,
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )


@pytest.fixture
def one_chunk(monkeypatch):
    async def retrieve(*args, **kwargs):
        return [
            rag.RetrievedChunk(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_title="Doc",
                content="Portal haqida.",
                score=1.0,
            )
        ]

    monkeypatch.setattr(rag, "retrieve", retrieve)


@pytest.mark.asyncio
async def test_a_model_hand_off_is_not_a_crisis_text_and_leaks_no_reason(session, one_chunk):
    found = await ai_navigator.answer(
        session,
        question="Boshqa foydalanuvchining telefon raqamini bering",
        language="uz",
        gateway=_NeedsHuman("The user asks for another user's personal data"),
    )
    assert found.escalated_to_human is True
    assert found.answer == safety.HANDOFF_TEXT["uz"]
    assert found.escalation_reason == "needs_human"
    assert "1146" not in found.answer
    assert found.resources == []


@pytest.mark.asyncio
async def test_a_disclosure_the_filter_missed_but_the_model_flagged_gets_the_safety_reply(
    session, one_chunk
):
    woman = await _user(session)
    found = await ai_navigator.answer(
        session,
        question="Uyda vaziyat yomon",
        user_id=woman.id,
        language="uz",
        gateway=_NeedsHuman("The user reports physical violence by a family member"),
    )
    assert found.escalation_reason == "safety_topic_detected"
    assert "1146" in found.answer
    assert "violence" not in found.answer
    assert await session.scalar(select(RiskFlag.id).where(RiskFlag.user_id == woman.id))


# ------------------------------------------------------------ retention


@pytest.mark.asyncio
async def test_the_sweep_scrubs_old_safety_text_and_short_lived_health_excerpts(session):
    old = datetime.now(UTC) - timedelta(days=settings.ai_sensitive_trace_retention_days + 1)
    legacy = AiInteraction(
        feature="assistant_education",
        model_version="guardrail",
        prompt_version="safety-filter",
        trace_id="legacy",
        prompt_excerpt="Erim meni kaltaklaydi",
        response_excerpt="…",
    )
    coach = AiInteraction(
        feature="assistant_coach",
        model_version="guardrail",
        prompt_version="coach-fallback",
        trace_id="coach",
        prompt_excerpt="Erim meni uradi",
    )
    health = AiInteraction(
        feature="assistant_health",
        model_version="m",
        prompt_version="p",
        trace_id="health",
        prompt_excerpt="Boshim og'riyapti",
        created_at=old,
    )
    redacted = AiInteraction(
        feature="assistant_education",
        model_version="guardrail",
        prompt_version="safety-filter",
        trace_id="new",
        prompt_excerpt="[safety:self_harm]",
    )
    session.add_all([legacy, coach, health, redacted])
    await session.flush()

    await retention.purge(session)
    await session.flush()
    for row in (legacy, coach, health, redacted):
        await session.refresh(row)

    assert legacy.prompt_excerpt is None and coach.prompt_excerpt is None
    assert health.prompt_excerpt is None
    assert redacted.prompt_excerpt == "[safety:self_harm]"


# --------------------------------------------------------- persona (C-28)


@pytest.mark.asyncio
async def test_a_signed_in_woman_without_consent_is_never_told_to_sign_in(session):
    woman = await _user(session)
    session.add(
        ConsentLog(
            user_id=woman.id,
            scope=ConsentScope.AI_PERSONALISATION,
            accepted=False,
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
        )
    )
    await session.flush()

    persona = await ai_assistant.build_persona(session, woman.id)
    assert persona.signed_in is True and persona.personalised is False
    prompt = persona.as_prompt()
    assert "IS signed in" in prompt
    assert "Never ask her to sign in" in prompt
    assert "Yordamchini" in prompt

    guest = await ai_assistant.build_persona(session, None)
    assert guest.signed_in is False
    assert "guest" in guest.as_prompt()


def test_safety_texts_are_honest_in_every_language():
    for lang in ("uz", "ru", "en"):
        for category in safety.SafetyCategory:
            signed_in = safety.safety_text(category, lang, flagged=True)
            guest = safety.safety_text(category, lang, flagged=False)
            assert "1146" in signed_in and "1146" in guest
            assert signed_in != guest
    # Uzbek leaves in the reformed alphabet.
    uz = safety.safety_text(safety.SafetyCategory.PHYSICAL_VIOLENCE, "uz", flagged=False)
    assert "o'" not in uz and "sh" not in uz.replace("WomanUP", "")


# ------------------------------------------------------- diagnostic (D-23)


@pytest.mark.asyncio
@pytest.mark.parametrize(("letter", "expected"), [("a", True), ("c", False)])
async def test_the_result_suggests_support_after_often_unable_to_cope(
    client, session, letter, expected
):
    from app.models.assessment import AssessmentQuestion
    from app.services import diagnostic

    woman = await _user(session)
    questions = [
        AssessmentQuestion(**row) for row in diagnostic.question_rows(diagnostic.load_bank())
    ]
    session.add_all(questions)
    await session.flush()
    answers = [
        {"question_id": str(qid), "option_ids": ids}
        for qid, ids in diagnostic.sample_answers(
            questions, scored=letter, family="a", profile={"pro.q3": ["first_job"]}
        )
    ]
    headers = _headers(woman)
    created = await client.post(
        f"{API}/diagnostic/attempt", json={"answers": answers}, headers=headers
    )
    assert created.status_code in (200, 201), created.text
    assert created.json()["support_suggested"] is expected
    flags = await session.scalar(select(func.count()).select_from(RiskFlag))
    assert flags == 0
