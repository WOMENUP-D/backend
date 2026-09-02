"""AI WomanUP Navigator — RAG question answering with guardrails."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AiInteraction
from app.schemas.ai import NavigatorAnswer, SourceRef
from app.services import rag
from app.services.llm_gateway import (
    LlmGateway,
    LlmResponse,
    LlmUnavailableError,
    llm_gateway,
)
from app.services.prompts import NAVIGATOR_SCHEMA, NAVIGATOR_SYSTEM

logger = logging.getLogger(__name__)

HUMAN_HANDOFF_TEXT = {
    "uz": (
        "Bu savol bo‘yicha sizga tirik mutaxassis yordam berishi kerak. "
        "Murojaatingizni koordinatorga yubordim — u siz bilan bog‘lanadi. "
        "Agar xavf ostida bo‘lsangiz, zudlik bilan 1146 ishonch telefoniga murojaat qiling."
    ),
    "ru": (
        "По этому вопросу вам должен помочь живой специалист. "
        "Я передала обращение координатору — он свяжется с вами. "
        "Если вы в опасности, немедленно позвоните на телефон доверия 1146."
    ),
    "en": (
        "This question needs a human specialist. I have passed your request to a "
        "coordinator who will contact you. If you are in danger, call the 1146 "
        "helpline immediately."
    ),
}

EXTRACTIVE_PREFIX = {
    "uz": (
        "AI generatsiya rejimi yoqilmagan, shuning uchun tasdiqlangan manbadan "
        "to'g'ridan-to'g'ri parcha keltiraman:"
    ),
    "ru": (
        "Режим генерации ИИ не подключён, поэтому привожу фрагмент из "
        "проверённого источника напрямую:"
    ),
    "en": (
        "AI generation is not enabled, so here is the relevant passage from an "
        "approved source, quoted directly:"
    ),
}

UNCERTAIN_TEXT = {
    "uz": (
        "Bu savolga tasdiqlangan manbalarda javob topa olmadim. Koordinatoringizga murojaat qiling."
    ),
    "ru": "Я не нашла ответа в проверённых источниках. Обратитесь к вашему координатору.",
    "en": "I could not find this in the approved sources. Please ask your coordinator.",
}


async def answer(
    session: AsyncSession,
    *,
    question: str,
    user_id: uuid.UUID | None = None,
    language: str = "uz",
    conversation_id: uuid.UUID | None = None,
    gateway: LlmGateway | None = None,
) -> NavigatorAnswer:
    """Answer a portal question from approved sources only.

    Order matters: the safety check runs before retrieval and before the model,
    so a disclosure of violence never becomes a generative prompt.
    """
    gateway = gateway or llm_gateway
    lang = language if language in HUMAN_HANDOFF_TEXT else "uz"

    if rag.needs_human_escalation(question):
        result = NavigatorAnswer(
            answer=HUMAN_HANDOFF_TEXT[lang],
            confidence=1.0,
            escalated_to_human=True,
            escalation_reason="safety_topic_detected",
            trace_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
        )
        await _record(
            session,
            user_id=user_id,
            trace_id=result.trace_id,
            model="guardrail",
            prompt_version="safety-filter",
            question=question,
            answer=result.answer,
            sources=[],
            confidence=1.0,
            escalated=True,
        )
        return result

    chunks = await rag.retrieve(session, question, language=lang)

    if not chunks and lang != "uz":
        # The knowledge base is authored in Uzbek, the primary locale. Rather
        # than tell a Russian- or English-speaking user that nothing exists,
        # fall back to the sources that do — the model is instructed to answer
        # in her language regardless of the language of the source. Degrade,
        # do not fail.
        chunks = await rag.retrieve(session, question, language=None)

    if not chunks:
        return NavigatorAnswer(
            answer=UNCERTAIN_TEXT[lang],
            confidence=0.0,
            is_uncertain=True,
            escalation_reason="no_matching_source",
            trace_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
        )

    if not gateway.enabled:
        # Extractive degradation: quote the best-matching approved source
        # verbatim instead of generating. Nothing is invented, and the user
        # still gets the information.
        best = chunks[0]
        return NavigatorAnswer(
            answer=f"{EXTRACTIVE_PREFIX[lang]}\n\n{best.content.strip()}",
            sources=[
                SourceRef(
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    chunk_id=chunk.chunk_id,
                    score=chunk.score,
                    excerpt=chunk.excerpt,
                )
                for chunk in chunks[:3]
            ],
            confidence=0.0,
            is_uncertain=True,
            escalation_reason="ai_disabled",
            trace_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
        )

    context = rag.build_context(chunks)
    user_content = f"{context}\n\nUSER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"

    try:
        response: LlmResponse = await gateway.complete(
            system=NAVIGATOR_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            json_schema=NAVIGATOR_SCHEMA,
            max_tokens=4000,
        )
    except LlmUnavailableError as exc:
        logger.warning("navigator fell back to uncertain: %s", exc)
        return NavigatorAnswer(
            answer=UNCERTAIN_TEXT[lang],
            confidence=0.0,
            is_uncertain=True,
            escalation_reason="model_unavailable",
            trace_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
        )

    payload = response.parsed or {}
    # Structured outputs guarantee the shape, not the range — the schema cannot
    # express numeric bounds, so clamp here rather than trust the value.
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    needs_human = bool(payload.get("needs_human")) or response.refused
    text = payload.get("answer") or UNCERTAIN_TEXT[lang]

    # Below the confidence floor the assistant says it does not know rather
    # than presenting a weakly-grounded answer as fact.
    is_uncertain = confidence < settings.ai_min_confidence
    if is_uncertain and not needs_human:
        text = UNCERTAIN_TEXT[lang]

    used_ids = {str(i) for i in payload.get("used_source_ids", [])}
    sources = [
        SourceRef(
            document_id=chunk.document_id,
            document_title=chunk.document_title,
            chunk_id=chunk.chunk_id,
            score=chunk.score,
            excerpt=chunk.excerpt,
        )
        for chunk in chunks
        if not used_ids or str(chunk.chunk_id) in used_ids
    ]

    result = NavigatorAnswer(
        answer=HUMAN_HANDOFF_TEXT[lang] if needs_human else text,
        sources=[] if needs_human else sources,
        confidence=confidence,
        is_uncertain=is_uncertain,
        escalated_to_human=needs_human,
        escalation_reason=payload.get("human_reason") if needs_human else None,
        suggested_actions=list(payload.get("suggested_actions") or [])[:3],
        trace_id=response.trace_id,
        conversation_id=conversation_id,
    )

    await _record(
        session,
        user_id=user_id,
        trace_id=response.trace_id,
        model=response.model,
        prompt_version=response.prompt_version,
        question=question,
        answer=result.answer,
        sources=[str(s.chunk_id) for s in sources],
        confidence=confidence,
        escalated=needs_human,
        refused=response.refused,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    return result


async def _record(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    trace_id: str,
    model: str,
    prompt_version: str,
    question: str,
    answer: str,
    sources: list[str],
    confidence: float,
    escalated: bool = False,
    refused: bool = False,
    latency_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Persist the interaction trace. Excerpts are PII-scrubbed and truncated."""
    session.add(
        AiInteraction(
            user_id=user_id,
            feature="navigator",
            model_version=model,
            prompt_version=prompt_version,
            trace_id=trace_id,
            prompt_excerpt=LlmGateway.sanitise(question)[:1000],
            response_excerpt=LlmGateway.sanitise(answer)[:1000],
            retrieved_sources=sources,
            confidence=confidence,
            escalated=escalated,
            refused=refused,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
