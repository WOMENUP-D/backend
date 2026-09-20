"""Lessons for the seeded catalogue, and the level each programme teaches at.

This writes no new curriculum. Every module already carries a title and a
paragraph saying what it covers — written by a person, translated three ways —
and that is what a lesson is. Each module becomes one readable lesson built
from its own text, so the learning section has real content to open instead of
a syllabus nobody authored.

Levels are set here rather than guessed at read time. "English for work, 60
hours" and "Family budget, 16 hours" are not the same ask, and the level is
what a completion writes into her skill evidence.

Idempotent: a module that already has a lesson is left alone.

    python -m app.seed_lessons
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EnrollmentStatus, LessonKind, ProficiencyLevel
from app.models.program import Enrollment, Program, ProgramLesson, ProgramModule
from app.services import learning

logger = logging.getLogger(__name__)

L = ProficiencyLevel

#: How demanding each seeded course is. Judged on what it asks of her — hours,
#: prerequisites and how much of the subject it covers — not on a formula.
LEVELS: dict[str, ProficiencyLevel] = {
    # Foundations: no prior knowledge assumed.
    "ai-va-raqamli-savodxonlik": L.BEGINNER,
    "ayollar-salomatligi": L.BEGINNER,
    "bolaning-maktabga-tayyorgarligi": L.BEGINNER,
    "emotsional-barqarorlik": L.BEGINNER,
    "huquqiy-savodxonlik": L.BEGINNER,
    "moliyaviy-savodxonlik": L.BEGINNER,
    "oiladagi-muloqot": L.BEGINNER,
    "oilaviy-byudjet": L.BEGINNER,
    "onalar-maktabi": L.BEGINNER,
    "raqamli-xavfsizlik": L.BEGINNER,
    "soglom-turmush": L.BEGINNER,
    "tadbirkorlik-asoslari": L.BEGINNER,
    # Builds on the basics, or asks her to produce something.
    "buxgalteriya-asoslari": L.ELEMENTARY,
    "ish-suhbatiga-tayyorgarlik": L.ELEMENTARY,
    "jamoatchilik-nutqi": L.ELEMENTARY,
    "liderlik-va-notiqlik": L.ELEMENTARY,
    "mahalla-tashabbusi": L.ELEMENTARY,
    "mikrokredit-va-bank": L.ELEMENTARY,
    "onlayn-savdo-boshlash": L.ELEMENTARY,
    "smm-va-raqamli-marketing": L.ELEMENTARY,
    # Long courses that assume experience or a business already running.
    "ayol-rahbar": L.INTERMEDIATE,
    "hunarmandchilik-biznesi": L.INTERMEDIATE,
    "ingliz-tili-b1": L.INTERMEDIATE,
    "ingliz-tili-ish-uchun": L.INTERMEDIATE,
    "mentorlik-asoslari": L.INTERMEDIATE,
    "tikuvchilik-va-dizayn": L.INTERMEDIATE,
}

_TRANSLIT = {
    "ʻ": "",
    "ʼ": "",
    "‘": "",
    "’": "",
    "'": "",
    "ў": "o",
    "қ": "q",
    "ғ": "g",
    "ҳ": "h",
}


def _slug(title: str, index: int) -> str:
    """A URL-safe name for a lesson, prefixed so two modules cannot collide."""
    folded = title.strip().casefold()
    for source, target in _TRANSLIT.items():
        folded = folded.replace(source, target)
    base = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return f"{index + 1}-{base}"[:160] if base else f"{index + 1}-dars"


def _blocks(content_i18n: dict) -> list[dict]:
    """The module's own text, as the paragraph blocks the reader renders.

    Paragraphs are matched across languages by position, which is how the text
    was written: the same paragraphs, three times. A language that runs short
    simply drops out of the later blocks, and the reader falls back the way she
    does everywhere else.
    """
    split = {
        language: [part.strip() for part in (text or "").split("\n\n") if part.strip()]
        for language, text in (content_i18n or {}).items()
    }
    if not any(split.values()):
        return []

    blocks: list[dict] = []
    for position in range(max(len(parts) for parts in split.values())):
        text = {
            language: parts[position] for language, parts in split.items() if position < len(parts)
        }
        if text:
            blocks.append({"type": "paragraph", "text": text})
    return blocks


async def carry_progress_over(session: AsyncSession) -> int:
    """Move what she already finished onto the lessons those modules became.

    A woman who completed a module before lessons existed has finished its
    content — the lesson is that same text. Without this, her course reads
    "0 of 3 lessons" beside a progress bar at 84%, and the honest reading of a
    module she has closed is that its lesson is closed too.

    Only ever adds ticks, and only for modules she had already completed.
    """
    lessons_by_module: dict[uuid.UUID, list[str]] = {}
    lessons_by_program: dict[uuid.UUID, list[str]] = {}
    for lesson_id, module_id, program_id in (
        await session.execute(
            select(ProgramLesson.id, ProgramLesson.module_id, ProgramModule.program_id).join(
                ProgramModule, ProgramModule.id == ProgramLesson.module_id
            )
        )
    ).all():
        lessons_by_module.setdefault(module_id, []).append(str(lesson_id))
        lessons_by_program.setdefault(program_id, []).append(str(lesson_id))
    if not lessons_by_module:
        return 0

    moved = 0
    for enrollment in (await session.execute(select(Enrollment))).scalars():
        ticks = set(enrollment.completed_lessons or [])
        before = len(ticks)

        for module_id in enrollment.completed_modules or []:
            try:
                ticks.update(lessons_by_module.get(uuid.UUID(str(module_id)), []))
            except ValueError:
                # A tick that is not an id at all: leave it where it is.
                continue

        # A course recorded as finished before any of this existed has its
        # lessons finished too — that is what "completed" means. Said by the
        # status itself, so nothing is invented. A course still in progress is
        # left alone: which lessons she got through is not ours to guess.
        if not ticks and enrollment.status == EnrollmentStatus.COMPLETED:
            ticks.update(lessons_by_program.get(enrollment.program_id, []))

        if len(ticks) != before:
            enrollment.completed_lessons = sorted(ticks)
            # Through the one service that owns progress, so the number and the
            # ticks agree and finishing has its usual consequences.
            await learning.recompute(session, enrollment=enrollment)
            moved += 1

    await session.flush()
    return moved


async def load_lessons(session: AsyncSession) -> dict[str, int]:
    """Give every module a lesson, every known programme its level, and every
    woman the lesson ticks her finished modules already earned."""
    counts = {"levels": 0, "lessons": 0, "skipped": 0, "carried": 0}

    programs = list((await session.execute(select(Program))).scalars())
    for program in programs:
        level = LEVELS.get(program.slug)
        if level is not None and program.level != level:
            program.level = level
            counts["levels"] += 1

    modules = list(
        (
            await session.execute(
                select(ProgramModule).order_by(ProgramModule.program_id, ProgramModule.order_index)
            )
        ).scalars()
    )
    existing = set((await session.execute(select(ProgramLesson.module_id).distinct())).scalars())

    for module in modules:
        if module.id in existing:
            counts["skipped"] += 1
            continue

        blocks = _blocks(module.content_i18n)
        title = module.title_i18n.get("uz") or next(iter(module.title_i18n.values()), "Dars")
        session.add(
            ProgramLesson(
                module_id=module.id,
                order_index=0,
                slug=_slug(title, module.order_index),
                title_i18n=dict(module.title_i18n),
                # Prose unless the module points at something to watch.
                kind=LessonKind.VIDEO if module.media_url else LessonKind.READING,
                duration_minutes=module.duration_minutes,
                blocks=blocks,
                media_url=module.media_url,
            )
        )
        counts["lessons"] += 1

    await session.flush()
    counts["carried"] = await carry_progress_over(session)
    return counts


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        counts = await load_lessons(session)
        await session.commit()

    logger.info("lessons ready: %s", counts)
    print(f"\n  {counts['lessons']} ta dars yaratildi.")
    print(f"  Darajasi belgilangan dasturlar: {counts['levels']}")
    print(f"  Darsi bor modullar (oʻzgarmadi): {counts['skipped']}")
    print(f"  Progressi darslarga koʻchirilgan yozuvlar: {counts['carried']}\n")


if __name__ == "__main__":
    asyncio.run(_main())
