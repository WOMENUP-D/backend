"""Individual development plan generation.

The AI proposes; nothing activates until the user accepts (section 06).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    GoalHorizon,
    PlanItemStatus,
    Priority,
    ScoreDimension,
    normalise_language,
)
from app.models.assessment import DevelopmentScore
from app.models.audit import AiInteraction
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.profile import Goal, Profile
from app.models.program import Program
from app.models.user import User
from app.services import skills as skill_service
from app.services.llm_gateway import (
    LlmGateway,
    LlmUnavailableError,
    llm_gateway,
)
from app.services.prompts import ROADMAP_SCHEMA, ROADMAP_SYSTEM
from app.services.score_insights import DIMENSION_PROGRAM_CATEGORIES
from app.services.scoring import weakest_dimensions

logger = logging.getLogger(__name__)

HORIZON_MONTHS: dict[GoalHorizon, int] = {
    GoalHorizon.M3: 3,
    GoalHorizon.M6: 6,
    GoalHorizon.M12: 12,
    GoalHorizon.M36: 36,
}


# Static text for the rule-based plan. Localised because this path runs exactly
# when the model is unavailable — the user should not be handed Uzbek because
# the AI happened to be down.
FALLBACK_TITLE = {
    "uz": "Boshlang‘ich rivojlanish rejasi",
    "ru": "Начальный план развития",
    "en": "Starter development plan",
}
FALLBACK_SUMMARY = {
    "uz": "Diagnostika natijasi asosida tuzilgan bazaviy reja.",
    "ru": "Базовый план, составленный по результатам диагностики.",
    "en": "A baseline plan built from your diagnostic results.",
}
FALLBACK_START = {
    "uz": "«{program}» dasturini boshlang",
    "ru": "Начните программу «{program}»",
    "en": "Start the \u201c{program}\u201d programme",
}
FALLBACK_PICK = {
    "uz": "{dimension} yo‘nalishi bo‘yicha dastur tanlang",
    "ru": "Выберите программу по направлению {dimension}",
    "en": "Choose a programme in {dimension}",
}
FALLBACK_NOTE = {
    "uz": "Bazaviy tavsiya — AI rejasi tayyor bo‘lganda yangilanadi.",
    "ru": "Базовая рекомендация — обновится, когда план от ИИ будет готов.",
    "en": "A baseline suggestion — it updates once the AI plan is ready.",
}


async def generate_plan(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    horizon: GoalHorizon = GoalHorizon.M6,
    focus_dimensions: list[ScoreDimension] | None = None,
    language: str | None = None,
    gateway: LlmGateway | None = None,
) -> DevelopmentPlan:
    """Build a draft roadmap. Falls back to a rule-based plan if AI is down."""
    gateway = gateway or llm_gateway

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    # Which language to write the roadmap in. The request wins: it carries the
    # locale she is actually reading the site in, and that is a live fact.
    # `User.language` is the fallback — it is only as fresh as the last time she
    # changed it, and for accounts created before that was persisted it is the
    # column default, which is why every plan used to come back in Uzbek.
    stored = await session.scalar(select(User.language).where(User.id == user_id))
    lang = (
        normalise_language(language) or normalise_language(stored.value if stored else None) or "uz"
    )
    scores = list(
        (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == user_id)
            )
        ).scalars()
    )
    goals = list(
        (
            await session.execute(
                select(Goal).where(Goal.user_id == user_id, Goal.achieved.is_(False))
            )
        ).scalars()
    )

    score_map = {s.dimension: s.current for s in scores}
    focus = focus_dimensions or weakest_dimensions(score_map, limit=3)

    programs = list(
        (
            await session.execute(select(Program).where(Program.is_published.is_(True)).limit(60))
        ).scalars()
    )

    if not gateway.enabled:
        logger.info("AI disabled — generating rule-based plan for %s", user_id)
        return await _fallback_plan(session, user_id, horizon, focus, programs, lang)

    # Canonical skills, not the labels she once typed: the roadmap should
    # build on what the platform can actually evidence, and should know the
    # difference between a skill a course taught her and one someone verified.
    notes = await skill_service.notes_for(session, user_id, language=lang)
    prompt = _build_prompt(profile, score_map, goals, focus, horizon, programs, lang, notes)

    try:
        response = await gateway.complete(
            system=ROADMAP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            json_schema=ROADMAP_SCHEMA,
        )
    except LlmUnavailableError as exc:
        logger.warning("roadmap generation unavailable (%s) — using fallback", exc)
        return await _fallback_plan(session, user_id, horizon, focus, programs, lang)

    payload = response.parsed
    if not payload:
        return await _fallback_plan(session, user_id, horizon, focus, programs, lang)

    plan = DevelopmentPlan(
        user_id=user_id,
        horizon=horizon,
        # A missing title fell back to a hardcoded Uzbek string, which put the
        # one language bug back into a plan the model had written in Russian.
        title=payload.get("title") or FALLBACK_TITLE.get(lang, FALLBACK_TITLE["uz"]),
        summary=payload.get("summary"),
        generated_by_ai=True,
        model_version=response.model,
        prompt_version=response.prompt_version,
        trace_id=response.trace_id,
        # Recorded so the reader can be told a plan was written in a language
        # she is no longer browsing in, and offered a regenerate.
        rationale={"focus_dimensions": [d.value for d in focus], "language": lang},
    )
    session.add(plan)
    await session.flush()

    valid_program_ids = {str(p.id) for p in programs}
    today = date.today()

    # Same reason as the navigator: the schema carries shape only, so the
    # roadmap length and the month offsets are bounded here.
    for index, item in enumerate(list(payload.get("items") or [])[:24]):
        raw_program_id = item.get("program_id")
        # Guard against a hallucinated id pointing at nothing.
        program_id = (
            uuid.UUID(raw_program_id)
            if raw_program_id and str(raw_program_id) in valid_program_ids
            else None
        )
        session.add(
            PlanItem(
                plan_id=plan.id,
                order_index=index,
                action=item["action"][:500],
                description=item.get("description"),
                dimension=ScoreDimension(item["dimension"]),
                priority=Priority(item.get("priority", "medium")),
                due_date=today
                + timedelta(days=30 * min(36, max(0, int(item.get("month_offset", 0) or 0)))),
                program_id=program_id,
            )
        )

    session.add(
        AiInteraction(
            user_id=user_id,
            feature="roadmap",
            model_version=response.model,
            prompt_version=response.prompt_version,
            trace_id=response.trace_id,
            prompt_excerpt=LlmGateway.sanitise(prompt)[:1000],
            response_excerpt=LlmGateway.sanitise(response.text)[:1000],
            confidence=None,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
    )

    await session.flush()
    await session.refresh(plan)
    return plan


def _program_title(program: Program, lang: str = "uz") -> str:
    """Uzbek title, falling back to whichever translation exists."""
    return (
        program.title_i18n.get(lang)
        or program.title_i18n.get("uz")
        or next(iter(program.title_i18n.values()), "")
    )


def _build_prompt(
    profile: Profile | None,
    score_map: dict[ScoreDimension, float],
    goals: list[Goal],
    focus: list[ScoreDimension],
    horizon: GoalHorizon,
    programs: list[Program],
    lang: str = "uz",
    skill_notes: list[skill_service.SkillNote] | None = None,
) -> str:
    """Assemble the user-turn payload. No direct identifiers are included."""
    lines = [
        f"USER LANGUAGE: {lang}",
        f"HORIZON: {HORIZON_MONTHS[horizon]} months",
        "",
        "DEVELOPMENT SCORE:",
    ]
    lines += [f"- {d.value}: {v}/100" for d, v in sorted(score_map.items())] or [
        "- (not assessed yet)"
    ]

    lines += ["", "FOCUS DIMENSIONS: " + ", ".join(d.value for d in focus)]

    lines += ["", "USER GOALS:"]
    lines += [f"- [{g.horizon.value}] {g.title}" for g in goals] or ["- (none stated)"]

    if profile:
        children = profile.children_count if profile.children_count is not None else "unspecified"
        lines += [
            "",
            "PROFILE:",
            f"- region/district: {profile.district or 'unspecified'}",
            f"- education: {profile.education_level or 'unspecified'}",
            f"- employment: {profile.employment_status or 'unspecified'}",
            f"- profession: {profile.profession or 'unspecified'}",
            f"- children: {children}",
        ]

    # Skills as the platform records them, each with how well it is known.
    # "learned" is a course; only a mentor, an employer or a placement makes a
    # skill "verified", and a roadmap that confuses the two plans the wrong
    # next step.
    lines += ["", "RECORDED SKILLS (name — how well it is known — level):"]
    lines += [f"- {note.as_line()}" for note in (skill_notes or [])] or ["- (none recorded yet)"]

    lines += ["", "AVAILABLE PROGRAMMES (id | category | title):"]
    lines += [f"- {p.id} | {p.category.value} | {_program_title(p, lang)}" for p in programs] or [
        "- (catalogue empty)"
    ]

    return "\n".join(lines)


async def _fallback_plan(
    session: AsyncSession,
    user_id: uuid.UUID,
    horizon: GoalHorizon,
    focus: list[ScoreDimension],
    programs: list[Program],
    lang: str = "uz",
) -> DevelopmentPlan:
    """Deterministic plan used when the model is unreachable.

    Suggests one published programme per weak dimension. It is intentionally
    plain: a working plan beats an error screen, and the user can regenerate.
    """
    plan = DevelopmentPlan(
        user_id=user_id,
        horizon=horizon,
        title=FALLBACK_TITLE.get(lang, FALLBACK_TITLE["uz"]),
        summary=FALLBACK_SUMMARY.get(lang, FALLBACK_SUMMARY["uz"]),
        generated_by_ai=False,
        rationale={
            "strategy": "rule_based_fallback",
            "focus_dimensions": [d.value for d in focus],
            "language": lang,
        },
    )
    session.add(plan)
    await session.flush()

    today = date.today()
    for index, dimension in enumerate(focus or list(ScoreDimension)[:3]):
        # The first category that builds the dimension and has a programme.
        match = next(
            (
                program
                for category in DIMENSION_PROGRAM_CATEGORIES[dimension]
                for program in programs
                if program.category == category
            ),
            None,
        )
        session.add(
            PlanItem(
                plan_id=plan.id,
                order_index=index,
                action=(
                    FALLBACK_START.get(lang, FALLBACK_START["uz"]).format(
                        program=_program_title(match, lang)
                    )
                    if match
                    else FALLBACK_PICK.get(lang, FALLBACK_PICK["uz"]).format(
                        dimension=dimension.value
                    )
                ),
                description=FALLBACK_NOTE.get(lang, FALLBACK_NOTE["uz"]),
                dimension=dimension,
                priority=Priority.HIGH if index == 0 else Priority.MEDIUM,
                due_date=today + timedelta(days=30 * (index + 1)),
                program_id=match.id if match else None,
            )
        )

    await session.flush()
    await session.refresh(plan)
    return plan


async def accept_plan(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    plan_id: uuid.UUID,
    removed_item_ids: list[uuid.UUID] | None = None,
) -> DevelopmentPlan | None:
    """Activate a plan. Exactly one plan is active per user at a time."""
    plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.id == plan_id, DevelopmentPlan.user_id == user_id
        )
    )
    if plan is None:
        return None

    await session.execute(
        update(DevelopmentPlan)
        .where(
            DevelopmentPlan.user_id == user_id,
            DevelopmentPlan.id != plan_id,
            DevelopmentPlan.is_active.is_(True),
        )
        .values(is_active=False)
    )

    for item_id in removed_item_ids or []:
        item = await session.get(PlanItem, item_id)
        if item is not None and item.plan_id == plan.id:
            item.status = PlanItemStatus.SKIPPED

    plan.is_active = True
    plan.accepted_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(plan)
    return plan


async def close_plan_items_for_program(
    session: AsyncSession, user_id: uuid.UUID, program_id: uuid.UUID
) -> list[PlanItem]:
    """Mark the roadmap steps bound to a programme done, because it is finished.

    A plan step that *is* a course carries no information the platform does not
    already hold: the enrollment knows which modules are ticked and closes itself
    at a hundred per cent. Asking her to then go to another screen and confirm it
    a second time is asking her to restate a fact the system just recorded, and
    the step sits there looking outstanding until she does.

    Deliberately narrow. Only steps with a `program_id`, only in the active plan,
    and only forward — a step already done or skipped is left alone, so this can
    never undo a decision she made. Steps with no programme behind them are not
    touched at all: nothing in the system knows whether she updated her CV, so
    those stay hers to mark.

    Returns what it changed, so the caller can log or report it.
    """
    rows = await session.execute(
        select(PlanItem)
        .join(DevelopmentPlan, PlanItem.plan_id == DevelopmentPlan.id)
        .where(
            DevelopmentPlan.user_id == user_id,
            DevelopmentPlan.is_active.is_(True),
            PlanItem.program_id == program_id,
            PlanItem.status.not_in((PlanItemStatus.DONE, PlanItemStatus.SKIPPED)),
        )
    )
    items = list(rows.scalars())
    now = datetime.now(UTC)
    for item in items:
        item.status = PlanItemStatus.DONE
        if item.completed_at is None:
            item.completed_at = now
    if items:
        await session.flush()
    return items
