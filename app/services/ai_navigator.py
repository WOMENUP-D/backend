"""AI WomanUP Navigator — RAG question answering with guardrails."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AiInteraction
from app.schemas.ai import NavigatorAnswer, SourceRef
from app.services import conversation, rag
from app.services.llm_gateway import (
    LlmGateway,
    LlmResponse,
    LlmUnavailableError,
    llm_gateway,
)
from app.services.prompts import NAVIGATOR_SCHEMA, NAVIGATOR_SYSTEM
from app.services.uzbek import question_language

logger = logging.getLogger(__name__)

HUMAN_HANDOFF_TEXT = {
    "uz": (
        "Bu savol böyiça sizga tirik mutaxassis yordam berişi kerak. "
        "Murojaatingizni koordinatorga yubordim — u siz bilan boğlanadi. "
        "Agar xavf ostida bölsangiz, zudlik bilan 1146 işonç telefoniga murojaat qiling."
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

#: Said whenever the answer is a quotation rather than a generated reply —
#: because the key is off, the provider failed, or its reply was unusable.
EXTRACTIVE_PREFIX = {
    "uz": (
        "AI yordamçi hozir cheklangan rejimda işlayapti, şuning uçun tasdiqlangan "
        "manbadan töğridan-töğri parça keltiraman:"
    ),
    "ru": (
        "AI-помощник сейчас работает в ограниченном режиме, поэтому привожу "
        "фрагмент из проверенного источника напрямую:"
    ),
    "en": (
        "The AI assistant is in limited mode right now, so here is the relevant "
        "passage from an approved source, quoted directly:"
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
    history: list | None = None,
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

    # The question's own language, not the interface's: a Russian question
    # typed on the Uzbek site is answered in Russian, from Russian sources.
    lang = question_language(question) or lang
    turns = conversation.clean(history)

    chunks = await rag.retrieve(session, question, language=lang)
    if not chunks:
        # Every edition of the knowledge base, in any language — the model is
        # instructed to answer in her language regardless of the source's.
        # This used to apply to ru/en only, so an Uzbek question whose answer
        # existed only in another edition was told nothing exists. Degrade,
        # do not fail.
        chunks = await rag.retrieve(session, question, language=None)
    if not chunks and turns:
        # A bare follow-up ("and the certificate?") carries few words of its
        # own; her previous question supplies the topic.
        chunks = await rag.retrieve(
            session, f"{conversation.last_user_text(turns)} {question}", language=None
        )

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
        return await _extractive(
            session, chunks, lang, question, user_id, conversation_id, reason="ai_disabled"
        )

    context = rag.build_context(chunks)
    user_content = (
        f"{context}\n\n{conversation.as_prompt(turns)}"
        f"USER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"
    )

    try:
        response: LlmResponse = await gateway.complete(
            system=NAVIGATOR_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            json_schema=NAVIGATOR_SCHEMA,
            max_tokens=4000,
            timeout=settings.ai_interactive_timeout_seconds,
        )
    except LlmUnavailableError as exc:
        # The sources were found; only the writer is missing. Quote them
        # rather than claim nothing exists (which is what this used to say).
        logger.warning("navigator fell back to extractive: %s", exc)
        return await _extractive(
            session, chunks, lang, question, user_id, conversation_id, reason="model_unavailable"
        )

    if response.parsed is None and not response.refused:
        logger.warning("navigator produced no usable payload; quoting the source")
        return await _extractive(
            session,
            chunks,
            lang,
            question,
            user_id,
            conversation_id,
            reason="model_unavailable",
            response=response,
        )

    payload = response.parsed or {}
    # Structured outputs guarantee the shape, not the range — the schema cannot
    # express numeric bounds, so clamp here rather than trust the value.
    try:
        confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.0))))
    except TypeError, ValueError:
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


async def _extractive(
    session: AsyncSession,
    chunks: list[rag.RetrievedChunk],
    lang: str,
    question: str,
    user_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    *,
    reason: str,
    response: LlmResponse | None = None,
) -> NavigatorAnswer:
    """Extractive degradation: quote the best-matching approved source verbatim
    instead of generating. Nothing is invented, and the user still gets the
    information."""
    best = chunks[0]
    result = NavigatorAnswer(
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
        escalation_reason=reason,
        trace_id=response.trace_id if response else uuid.uuid4().hex,
        conversation_id=conversation_id,
    )
    await _record(
        session,
        user_id=user_id,
        trace_id=result.trace_id,
        model=response.model if response else "extractive",
        prompt_version="navigator-extractive",
        question=question,
        answer=result.answer,
        sources=[str(chunk.chunk_id) for chunk in chunks[:3]],
        confidence=0.0,
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
