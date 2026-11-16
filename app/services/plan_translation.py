"""A development plan shown in the language she is reading the site in.

A plan is written once, in one language. Regenerating it on a language switch
would throw away the steps she accepted and the progress she made, so the
plan is translated instead: the readable text (title, summary, each step's
action and description) is translated once per language and cached on the
plan. Everything else — steps, dates, statuses, courses — is the same plan.

Reads only *look up* the cache (`localise`); the model is called from one
explicit request (`translate`), which spends the user's hourly AI quota.
Degrade, don't fail: when the model is unavailable the original text is
returned with `translation_unavailable` set, never an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.audit import AiInteraction
from app.models.plan import DevelopmentPlan
from app.schemas.plan import PlanItemRead, PlanRead
from app.services.llm_gateway import LlmGateway, LlmUnavailableError, llm_gateway
from app.services.prompts import PLAN_TRANSLATION_SCHEMA, PLAN_TRANSLATION_SYSTEM
from app.services.uzbek import to_new_alphabet

logger = logging.getLogger(__name__)

LANGUAGES = ("uz", "ru", "en")
LANGUAGE_NAMES = {"uz": "Uzbek (reformed Latin)", "ru": "Russian", "en": "English"}


def _cached(plan: DevelopmentPlan, language: str) -> dict | None:
    entry = (plan.translations or {}).get(language)
    return entry if isinstance(entry, dict) else None


def localise(plan: DevelopmentPlan, language: str | None, *, unavailable: bool = False) -> PlanRead:
    """The plan as the API returns it, in `language` when that is cached.

    Pure: nothing is written. Without a cached translation the original text is
    returned and `language` says which one it is, so the client can ask for a
    translation (`translate`) and meanwhile tell her what she is reading.
    """
    read = PlanRead.model_validate(plan)
    wanted = language if language in LANGUAGES else None
    entry = _cached(plan, wanted) if wanted and wanted != plan.content_language else None
    if entry is None:
        return read.model_copy(
            update={"language": plan.content_language, "translation_unavailable": unavailable}
        )

    texts: dict[str, Any] = entry.get("items") or {}
    items: list[PlanItemRead] = []
    for item in read.items:
        text = texts.get(str(item.id)) or {}
        items.append(
            item.model_copy(
                update={
                    "action": text.get("action") or item.action,
                    "description": text.get("description") or item.description,
                }
            )
        )
    return read.model_copy(
        update={
            "title": entry.get("title") or read.title,
            "summary": entry.get("summary") or read.summary,
            "items": items,
            "language": wanted,
        }
    )


def item_action(plan: DevelopmentPlan, item: Any, language: str | None) -> str:
    """One step's action in `language` when the plan has a cached translation."""
    if not language or language == plan.content_language:
        return item.action
    entry = _cached(plan, language) or {}
    text = (entry.get("items") or {}).get(str(item.id)) or {}
    return text.get("action") or item.action


#: Steps per model call. A whole plan in one call took ~40 s for eleven Uzbek
#: steps — a six-month plan of eighteen would pass the interactive timeout and
#: fail on every visit. Chunks run in parallel, so the wait is one chunk's.
CHUNK = 5


def _chunks(plan: DevelopmentPlan) -> list[dict[str, Any]]:
    """The readable text in parallel-sized pieces; the first carries the heading."""
    items = [
        {"id": str(item.id), "action": item.action, "description": item.description or ""}
        for item in plan.items
    ]
    pieces = [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)] or [[]]
    return [
        {
            "title": plan.title if index == 0 else "",
            "summary": (plan.summary or "") if index == 0 else "",
            "items": piece,
        }
        for index, piece in enumerate(pieces)
    ]


def _text(value: Any, language: str) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return to_new_alphabet(value) if language == "uz" and value else value


def _clean(payloads: list[dict[str, Any]], plan: DevelopmentPlan, language: str) -> dict | None:
    """The cache entry, or None when the replies are not a translation of this plan.

    Every step must come back: a half-translated plan would read as two
    languages at once, so a missing step fails the translation as a whole.
    """
    known = {str(item.id) for item in plan.items}
    items: dict[str, dict[str, str]] = {}
    for payload in payloads:
        for raw in payload.get("items") or []:
            if not isinstance(raw, dict) or raw.get("id") not in known:
                continue
            action = _text(raw.get("action"), language)
            if action:
                items[raw["id"]] = {
                    "action": action[:500],
                    "description": _text(raw.get("description"), language),
                }
    head = payloads[0] if payloads else {}
    title = _text(head.get("title"), language)
    if not title or set(items) != known:
        return None
    return {"title": title[:255], "summary": _text(head.get("summary"), language), "items": items}


async def translate(
    session: AsyncSession,
    plan: DevelopmentPlan,
    language: str,
    *,
    user_id: uuid.UUID,
    gateway: LlmGateway | None = None,
) -> PlanRead:
    """Translate the plan into `language`, cache it, and return it localised.

    A plan already in that language, or already translated, costs nothing.
    The caller has checked ownership and spent the AI quota.
    """
    gateway = gateway or llm_gateway
    if language == plan.content_language or _cached(plan, language) is not None:
        return localise(plan, language)
    if not gateway.enabled:
        return localise(plan, language, unavailable=True)

    prompts = [
        f"TARGET LANGUAGE: {LANGUAGE_NAMES[language]}\n\n"
        f"PLAN:\n{json.dumps(chunk, ensure_ascii=False)}"
        for chunk in _chunks(plan)
    ]

    async def one(prompt: str):
        return await gateway.complete(
            system=PLAN_TRANSLATION_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            json_schema=PLAN_TRANSLATION_SCHEMA,
            # A translation needs no reasoning, and she is waiting on the page:
            # low effort, and bounded so a slow call shows the original rather
            # than running into the edge proxy's limit.
            effort="low",
            timeout=settings.ai_interactive_timeout_seconds,
        )

    try:
        responses = await asyncio.gather(*(one(prompt) for prompt in prompts))
    except LlmUnavailableError as exc:
        logger.warning("plan translation unavailable (%s) — showing the original", exc)
        return localise(plan, language, unavailable=True)

    for prompt, response in zip(prompts, responses, strict=True):
        session.add(
            AiInteraction(
                user_id=user_id,
                feature="plan_translation",
                model_version=response.model,
                prompt_version=response.prompt_version,
                trace_id=response.trace_id,
                prompt_excerpt=LlmGateway.sanitise(prompt)[:1000],
                response_excerpt=LlmGateway.sanitise(response.text)[:1000],
                confidence=None,
                refused=response.refused,
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        )
    entry = _clean([response.parsed or {} for response in responses], plan, language)
    if entry is None:
        logger.warning("plan translation reply unusable for plan %s", plan.id)
        await session.flush()
        return localise(plan, language, unavailable=True)

    entry.update(
        model_version=responses[0].model,
        prompt_version=responses[0].prompt_version,
        trace_ids=[response.trace_id for response in responses],
    )
    plan.translations = {**(plan.translations or {}), language: entry}
    flag_modified(plan, "translations")
    await session.flush()
    return localise(plan, language)
