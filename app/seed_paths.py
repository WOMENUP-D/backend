"""Learning paths over the seeded catalogue.

Six routes, and not one new course. Every step below names a programme that is
already in the database — if a slug is missing, that step is skipped and the
path is built from what does exist, because a route that advertises a course
nobody wrote is worse than a shorter route.

How the six were chosen: a path has to be a *progression*, not a folder. Each
one below has an order that would be wrong reversed — you cannot price a craft
you cannot make, you cannot budget a loan before you can budget a month. Where
the catalogue holds several courses on a subject but no order between them,
there is no path, and the reasons are written at the bottom of this file rather
than papered over with a seventh entry.

Nothing here invents a skill, a duration or a level for a path beyond how
demanding the route is. What a path teaches is read off its courses, and how
long it takes is their hours added up — see `services.learning_path`.

Idempotent: a path that exists is updated in place and its steps re-pointed,
never duplicated. Safe to run against production.

    python -m app.seed_paths
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProficiencyLevel, ScoreDimension
from app.models.learning_path import LearningPath, LearningPathItem
from app.models.program import Program

logger = logging.getLogger(__name__)

L = ProficiencyLevel
D = ScoreDimension

#: slug -> (title uz/ru/en, description uz/ru/en, dimension, level, steps)
#: A step is (programme slug, required).
PATHS: list[dict] = [
    {
        "slug": "moliyaviy-mustaqillik",
        "title": (
            "Moliyaviy mustaqillik",
            "Финансовая независимость",
            "Financial independence",
        ),
        "description": (
            "Oyma-oy hisob yuritishdan boshlab, oilaviy byudjetni rejalashtirishgacha va "
            "bankdan mablagʻ olishni ongli tanlashgacha. Uchta kurs ketma-ket: avval "
            "pulni sanashni, keyin rejalashtirishni, soʻng qarz bilan ishlashni "
            "oʻrganasiz.",
            "От учёта расходов до планирования семейного бюджета и осознанного выбора "
            "кредита. Три курса по порядку: сначала считать деньги, потом планировать, "
            "затем работать с заёмными средствами.",
            "From tracking what you spend to planning a household budget and choosing a "
            "loan with your eyes open. Three courses in order: count first, plan second, "
            "borrow third.",
        ),
        "dimension": D.FINANCIAL_LITERACY,
        "level": L.BEGINNER,
        "steps": [
            ("moliyaviy-savodxonlik", True),
            ("oilaviy-byudjet", True),
            ("mikrokredit-va-bank", True),
        ],
    },
    {
        "slug": "oz-biznesingni-boshlash",
        "title": (
            "Oʻz biznesingni boshlash",
            "Начать своё дело",
            "Start your own business",
        ),
        "description": (
            "Gʻoyadan birinchi sotuvgacha. Biznes-reja tuzasiz, mijozga qanday "
            "yetib borishni oʻrganasiz va onlayn savdoni ishga tushirasiz. "
            "Moliyalashtirish kursi ixtiyoriy: u kerak boʻlganda oʻtiladi.",
            "От идеи до первой продажи. Составите бизнес-план, научитесь доходить до "
            "клиента и запустите онлайн-продажи. Курс о финансировании — по желанию, "
            "когда он действительно понадобится.",
            "From an idea to a first sale. Write the business plan, learn how to reach a "
            "customer, and open an online shop. The funding course is optional — take it "
            "when you actually need it.",
        ),
        "dimension": D.ENTREPRENEURSHIP,
        "level": L.BEGINNER,
        "steps": [
            ("tadbirkorlik-asoslari", True),
            ("smm-va-raqamli-marketing", True),
            ("onlayn-savdo-boshlash", True),
            ("mikrokredit-va-bank", False),
        ],
    },
    {
        "slug": "birinchi-ishga-yol",
        "title": (
            "Birinchi ishga yoʻl",
            "Путь к первой работе",
            "The way to your first job",
        ),
        "description": (
            "Ishga kirishdan oldin kerak boʻladigan uchta narsa: kompyuter va sunʼiy "
            "intellekt bilan ishlash, rezyume va suhbat, hamda mehnat shartnomasida "
            "oʻz huquqingizni bilish. Buxgalteriya kursi — aniq bir kasbni tanlamoqchi "
            "boʻlganlar uchun.",
            "Три вещи, которые нужны до выхода на работу: уверенная работа с компьютером "
            "и ИИ, резюме и собеседование, а также знание своих прав в трудовом договоре. "
            "Курс бухгалтерии — для тех, кто хочет конкретную профессию.",
            "The three things you need before you start work: confidence with a computer "
            "and AI, a CV and an interview, and knowing your rights in an employment "
            "contract. The bookkeeping course is there if you want a concrete trade.",
        ),
        "dimension": D.EMPLOYMENT,
        "level": L.BEGINNER,
        "steps": [
            ("ai-va-raqamli-savodxonlik", True),
            ("ish-suhbatiga-tayyorgarlik", True),
            ("huquqiy-savodxonlik", True),
            ("buxgalteriya-asoslari", False),
        ],
    },
    {
        "slug": "hunardan-biznesgacha",
        "title": (
            "Hunardan biznesgacha",
            "От ремесла к бизнесу",
            "From craft to business",
        ),
        "description": (
            "Qoʻl mehnatini daromadga aylantirish. Avval kasbning oʻzi — tikuvchilik va "
            "dizayn, keyin brend va narx belgilash, soʻng marketpleysda sotish. "
            "Tartib muhim: sotib boʻlmaydigan narsani sotishni oʻrganib boʻlmaydi.",
            "Превратить ручной труд в доход. Сначала само ремесло — шитьё и дизайн, затем "
            "бренд и ценообразование, потом продажи на маркетплейсе. Порядок важен: "
            "нельзя научиться продавать то, чего ещё нет.",
            "Turning handwork into an income. The craft itself first — sewing and design "
            "— then a brand and a price, then selling on a marketplace. The order matters: "
            "you cannot learn to sell what you cannot yet make.",
        ),
        "dimension": D.ENTREPRENEURSHIP,
        "level": L.INTERMEDIATE,
        "steps": [
            ("tikuvchilik-va-dizayn", True),
            ("hunarmandchilik-biznesi", True),
            ("onlayn-savdo-boshlash", True),
        ],
    },
    {
        "slug": "yetakchilik-va-jamoa",
        "title": (
            "Yetakchilik va jamoa",
            "Лидерство и команда",
            "Leadership and team",
        ),
        "description": (
            "Ovozdan jamoagacha. Avval odamlar oldida gapirish, keyin muzokara va "
            "yetakchilik, soʻng jamoani boshqarish. Mentorlik kursi — oʻz "
            "tajribangizni boshqalarga uzatmoqchi boʻlsangiz.",
            "От голоса к команде. Сначала выступать перед людьми, затем переговоры и "
            "лидерство, потом управление командой. Курс менторства — если захотите "
            "передавать свой опыт другим.",
            "From a voice to a team. Speaking in front of people first, then negotiation "
            "and leading, then running a team. The mentoring course is for when you want "
            "to pass your own experience on.",
        ),
        "dimension": D.SOCIAL_ACTIVITY,
        "level": L.ELEMENTARY,
        "steps": [
            ("jamoatchilik-nutqi", True),
            ("liderlik-va-notiqlik", True),
            ("ayol-rahbar", True),
            ("mentorlik-asoslari", False),
        ],
    },
    {
        "slug": "onalik-va-oila",
        "title": (
            "Onalik va oila",
            "Материнство и семья",
            "Motherhood and family",
        ),
        "description": (
            "Bolaning birinchi yillaridan maktabga tayyorgarlikgacha, va oilada "
            "eshitiladigan muloqotgacha. Uchta kurs bolaning yoshi boʻyicha "
            "ketma-ket joylashgan.",
            "От первых лет ребёнка до подготовки к школе и до разговора в семье, который "
            "слышат. Три курса выстроены по возрасту ребёнка.",
            "From a child's first years to getting ready for school, and to the kind of "
            "conversation a family actually hears. Three courses, in the order a child "
            "grows.",
        ),
        "dimension": D.FAMILY_PARENTING,
        "level": L.BEGINNER,
        "steps": [
            ("onalar-maktabi", True),
            ("bolaning-maktabga-tayyorgarligi", True),
            ("oiladagi-muloqot", True),
        ],
    },
]

#: Where the catalogue does not yet support a path, and why. Kept here rather
#: than invented into existence — a route nobody would take is not content.
#:
#: * Health (`ayollar-salomatligi`, `soglom-turmush`, `emotsional-barqarorlik`):
#:   three courses on parallel subjects with no order between them. Prevention,
#:   nutrition and emotional resilience do not build on one another, so a path
#:   would only be a folder with a progress bar on it.
#: * International integration (`ingliz-tili-b1`, `ingliz-tili-ish-uchun`): a
#:   real progression, but two courses and 132 hours. Worth a path once a third
#:   step exists — an exam preparation or a mobility programme.
#: * Digital safety (`raqamli-xavfsizlik`): one course. A path of one is a
#:   course with extra words.
#: * Civic (`mahalla-tashabbusi`): one course, and the leadership route already
#:   carries the skills it needs.


def _i18n(values: tuple[str, str, str]) -> dict:
    uz, ru, en = values
    return {"uz": uz, "ru": ru, "en": en}


async def load_paths(session: AsyncSession) -> dict[str, int]:
    """Create or refresh the curated paths. Never duplicates, never deletes courses."""
    counts = {"paths": 0, "updated": 0, "items": 0, "missing": 0}
    now = datetime.now(UTC)

    programs = {
        program.slug: program for program in (await session.execute(select(Program))).scalars()
    }

    for order, spec in enumerate(PATHS):
        steps = [(programs[slug], required) for slug, required in spec["steps"] if slug in programs]
        missing = len(spec["steps"]) - len(steps)
        counts["missing"] += missing
        if missing:
            logger.warning(
                "learning path %s: %d of its programmes are not in the catalogue",
                spec["slug"],
                missing,
            )
        if not steps:
            # A route with no courses in it is not a route.
            continue

        path = await session.scalar(select(LearningPath).where(LearningPath.slug == spec["slug"]))
        if path is None:
            path = LearningPath(slug=spec["slug"], published_at=now)
            session.add(path)
            counts["paths"] += 1
        else:
            counts["updated"] += 1

        path.title_i18n = _i18n(spec["title"])
        path.description_i18n = _i18n(spec["description"])
        path.dimension = spec["dimension"]
        path.level = spec["level"]
        path.order_index = order
        path.is_published = True
        if path.published_at is None:
            path.published_at = now
        await session.flush()

        # Re-point the steps in place. The rows a woman's progress is read
        # against are the enrollments, not these, so re-ordering a path never
        # touches what she has finished.
        existing = {
            item.program_id: item
            for item in (
                await session.execute(
                    select(LearningPathItem).where(LearningPathItem.path_id == path.id)
                )
            ).scalars()
        }
        for index, (program, required) in enumerate(steps):
            item = existing.pop(program.id, None)
            if item is None:
                item = LearningPathItem(path_id=path.id, program_id=program.id)
                session.add(item)
                counts["items"] += 1
            item.order_index = index
            item.is_required = required

        # A course removed from a path in this file is removed from the path.
        for stale in existing.values():
            await session.delete(stale)

    await session.flush()
    return counts


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        counts = await load_paths(session)
        await session.commit()

    logger.info("learning paths ready: %s", counts)
    print(f"\n  {counts['paths']} ta yangi yoʻnalish yaratildi.")
    print(f"  Yangilangan yoʻnalishlar: {counts['updated']}")
    print(f"  Qoʻshilgan bosqichlar: {counts['items']}")
    print(f"  Katalogda topilmagan dasturlar: {counts['missing']}\n")


if __name__ == "__main__":
    asyncio.run(_main())
