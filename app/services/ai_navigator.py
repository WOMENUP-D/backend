"""AI WomanUP Navigator — RAG question answering with guardrails."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AiInteraction
from app.schemas.ai import NavigatorAnswer, SourceRef
from app.services import rag, safety
from app.services.llm_gateway import (
    LlmGateway,
    LlmResponse,
    LlmUnavailableError,
    llm_gateway,
)
from app.services.prompts import NAVIGATOR_SCHEMA, NAVIGATOR_SYSTEM

logger = logging.getLogger(__name__)

#: The model's own "this needs a person" — not the safety filter. Honest: it
#: claims nothing was forwarded, and it is not a crisis text (I-15).
HUMAN_HANDOFF_TEXT = safety.HANDOFF_TEXT

EXTRACTIVE_PREFIX = {
    "uz": (
        "AI generatsiya rejimi yoqilmagan, şuning uçun tasdiqlangan manbadan "
        "töğridan-töğri parça keltiraman:"
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

    category = safety.detect(question)
    if category is not None:
        return await _safety_reply(
            session, user_id=user_id, category=category, lang=lang, conversation_id=conversation_id
        )

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
    except TypeError, ValueError:
        confidence = 0.0
    needs_human = bool(payload.get("needs_human")) or response.refused
    # The model's free-text reason never reaches the client (it once echoed
    # "the user reports physical violence…" back to her). It is only read here,
    # model-free, to tell a missed disclosure from an ordinary hand-off.
    human_reason = str(payload.get("human_reason") or "")
    if needs_human and safety.mentions_safety_topic(human_reason):
        return await _safety_reply(
            session,
            user_id=user_id,
            category=safety.detect(human_reason) or safety.SafetyCategory.PHYSICAL_VIOLENCE,
            lang=lang,
            conversation_id=conversation_id,
            response=response,
        )
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
        escalation_reason="needs_human" if needs_human else None,
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


async def _safety_reply(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    category: safety.SafetyCategory,
    lang: str,
    conversation_id: uuid.UUID | None,
    response: LlmResponse | None = None,
) -> NavigatorAnswer:
    """Fixed safety text, a coordinator signal for a signed-in user, and a
    trace that keeps the category rather than her words."""
    trace_id = response.trace_id if response else uuid.uuid4().hex
    outcome = await safety.escalate(
        session,
        user_id=user_id,
        category=category,
        surface="navigator",
        language=lang,
        trace_id=trace_id,
    )
    result = NavigatorAnswer(
        answer=outcome.text,
        confidence=1.0,
        escalated_to_human=True,
        escalation_reason="safety_topic_detected",
        trace_id=trace_id,
        conversation_id=conversation_id,
        resources=outcome.resources,
    )
    await _record(
        session,
        user_id=user_id,
        trace_id=trace_id,
        model=response.model if response else "guardrail",
        prompt_version=response.prompt_version if response else "safety-filter",
        question=safety.redacted(category),
        answer=result.answer,
        sources=[],
        confidence=1.0,
        escalated=True,
        latency_ms=response.latency_ms if response else None,
        input_tokens=response.input_tokens if response else None,
        output_tokens=response.output_tokens if response else None,
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
