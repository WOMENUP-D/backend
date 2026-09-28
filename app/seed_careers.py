"""Career paths over the seeded catalogue.

Six directions, and not one new course, task or listing. Each one below names
skills that the catalogue already teaches, practises or asks for, and a
learning path that already exists. A skill the taxonomy does not know is
skipped with a warning rather than minted, and a direction nothing in the
catalogue can help with is not published — a path to work WomanUP cannot
prepare anybody for is not content.

How the six were chosen: a direction has to be backed from both ends. Courses
or tasks that teach its skills must exist, *and* the kind of listing it leads
to must exist on the platform — a vacancy, an internship, a grant, a
marketplace. Where only one end exists, there is no direction yet, and the
reasons are written at the bottom of this file.

Nothing here promises work. Every description says what a path can help her
prepare for, never what it will get her.

Related courses, tasks and listings are not listed here: they are read off the
skills every time — see `services.career_path`.

Idempotent: a direction that exists is updated in place, never duplicated.
Safe to run against production.

    python -m app.seed_careers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import CareerCategory, OpportunityType, ProficiencyLevel
from app.models.career_path import CareerPath
from app.models.learning_path import LearningPath
from app.models.skill import Skill

logger = logging.getLogger(__name__)

C = CareerCategory
L = ProficiencyLevel
O = OpportunityType  # noqa: E741

CAREERS: list[dict] = [
    {
        "slug": "buxgalter",
        "title": ("Buxgalter", "Бухгалтер", "Accountant"),
        "summary": (
            "Kichik biznesning hisob-kitobini yuritadi: hujjatlar, 1C va Excel.",
            "Ведёт учёт в малом бизнесе: документы, 1С и Excel.",
            "Keeps the books of a small business: documents, 1C and Excel.",
        ),
        "description": (
            "Bu yoʻl sizga kichik biznesda buxgalter yoki buxgalter yordamchisi boʻlib "
            "ishlashga tayyorlanishda yordam beradi. Avval kompyuter, rezyume va ish "
            "suhbati, keyin 1C va Excel bilan hisob yuritish. Oxirida — buxgalteriya "
            "boʻyicha amaliyot va boʻsh ish oʻrinlari bilan tanishish.",
            "Этот путь поможет подготовиться к работе бухгалтером или помощником "
            "бухгалтера в малом бизнесе. Сначала компьютер, резюме и собеседование, "
            "затем учёт в 1С и Excel. В конце — стажировки и вакансии в бухгалтерии.",
            "This path can help you prepare to work as an accountant or an assistant "
            "accountant in a small business. First the computer, a CV and the "
            "interview, then bookkeeping in 1C and Excel. At the end, internships and "
            "vacancies in accounting.",
        ),
        "category": C.EMPLOYMENT,
        "level": L.ELEMENTARY,
        "skills": ["accounting", "excel", "1c", "cv-writing", "job-interviews"],
        "learning_path": "birinchi-ishga-yol",
        "opportunities": [O.VACANCY, O.INTERNSHIP],
    },
    {
        "slug": "smm-mutaxassis",
        "title": ("SMM mutaxassisi", "SMM-специалист", "Social media specialist"),
        "summary": (
            "Kompaniya sahifalarini yuritadi: kontent reja, dizayn va reklama.",
            "Ведёт страницы компании: контент-план, дизайн и продвижение.",
            "Runs a company's pages: content plan, design and promotion.",
        ),
        "description": (
            "Bu yoʻl sizga ijtimoiy tarmoqlarda ishlashga tayyorlanishda yordam beradi: "
            "kontent reja tuzish, Canva'da dizayn qilish, sunʼiy intellekt vositalaridan "
            "foydalanish va marketing asoslari. Koʻpincha masofadan ham ishlash mumkin "
            "boʻlgan kasb.",
            "Этот путь поможет подготовиться к работе в соцсетях: составлять "
            "контент-план, делать дизайн в Canva, пользоваться инструментами ИИ и "
            "понимать основы маркетинга. Эту работу часто можно делать удалённо.",
            "This path can help you prepare for work on social media: planning "
            "content, designing in Canva, using AI tools and understanding the basics "
            "of marketing. It is work that can often be done remotely.",
        ),
        "category": C.EMPLOYMENT,
        "level": L.ELEMENTARY,
        "skills": ["smm", "content-marketing", "canva", "marketing", "ai-tools"],
        "learning_path": None,
        "opportunities": [O.VACANCY, O.INTERNSHIP],
    },
    {
        "slug": "tikuvchi",
        "title": ("Tikuvchi-dizayner", "Швея-дизайнер", "Seamstress and designer"),
        "summary": (
            "Kiyim tikadi va loyihalaydi: lekalo, dizayn va tikuv mashinalari.",
            "Шьёт и конструирует одежду: лекала, дизайн и швейное оборудование.",
            "Sews and designs clothes: patterns, design and sewing machines.",
        ),
        "description": (
            "Bu yoʻl sizga tikuv sexi yoki ustaxonada ishlashga tayyorlanishda yordam "
            "beradi: tikish, lekalo tayyorlash va kiyim dizayni. Ish beruvchilar "
            "koʻpincha overlokda ishlashni ham soʻraydi — bu koʻnikma hozircha WomanUP "
            "kurslarida oʻrgatilmaydi, buni ochiq aytamiz.",
            "Этот путь поможет подготовиться к работе в швейном цехе или ателье: "
            "шитьё, построение лекал и дизайн одежды. Работодатели часто просят и "
            "работу на оверлоке — этому пока не учат курсы WomanUP, и мы говорим об "
            "этом прямо.",
            "This path can help you prepare for work in a sewing workshop or a studio: "
            "sewing, pattern making and clothing design. Employers often ask for "
            "overlock work too — WomanUP courses do not teach that yet, and we say so.",
        ),
        "category": C.EMPLOYMENT,
        "level": L.INTERMEDIATE,
        "skills": ["sewing", "pattern-making", "design", "overlock"],
        "learning_path": None,
        "opportunities": [O.VACANCY, O.INTERNSHIP],
    },
    {
        "slug": "loyiha-koordinatori",
        "title": (
            "Jamoat loyihalari koordinatori",
            "Координатор общественных проектов",
            "Community project coordinator",
        ),
        "summary": (
            "Mahalla va jamoat loyihalarini boshqaradi: jamoa, nutq va taqdimot.",
            "Ведёт проекты махалли и сообщества: команда, выступления и презентации.",
            "Leads neighbourhood and community projects: a team, speaking and presenting.",
        ),
        "description": (
            "Bu yoʻl sizga jamoat tashkilotlari, mahalla yoki xalqaro dasturlarda "
            "loyiha yuritishga tayyorlanishda yordam beradi: gʻoyani loyihaga "
            "aylantirish, jamoa bilan ishlash, odamlar oldida gapirish va ularni "
            "ergashtirish.",
            "Этот путь поможет подготовиться к работе с проектами в общественных "
            "организациях, махалле или международных программах: превращать идею в "
            "проект, работать в команде, выступать и вести за собой.",
            "This path can help you prepare to run projects in community "
            "organisations, your neighbourhood or international programmes: turning "
            "an idea into a project, working with a team, speaking in front of people "
            "and leading them.",
        ),
        "category": C.EMPLOYMENT,
        "level": L.ELEMENTARY,
        "skills": [
            "project-management",
            "leadership",
            "public-speaking",
            "teamwork",
            "presentation",
        ],
        "learning_path": "yetakchilik-va-jamoa",
        "opportunities": [O.INTERNATIONAL_PROGRAM, O.MENTORSHIP, O.INTERNSHIP],
    },
    {
        "slug": "kichik-biznes",
        "title": ("Kichik biznes egasi", "Владелица малого бизнеса", "Small business owner"),
        "summary": (
            "Oʻz ishini ochadi: biznes-reja, sotuv, marketing va mablagʻ topish.",
            "Открывает своё дело: бизнес-план, продажи, маркетинг и финансирование.",
            "Starts her own business: a plan, sales, marketing and funding.",
        ),
        "description": (
            "Bu yoʻl sizga oʻz biznesingizni boshlashga tayyorlanishda yordam beradi: "
            "biznes-reja yozish, mijoz topish va sotish, kredit va bank xizmatlarini "
            "tushunish. Oxirida — grantlar, mentorlik va investitsiya imkoniyatlari "
            "bilan tanishish.",
            "Этот путь поможет подготовиться к открытию своего дела: написать "
            "бизнес-план, найти клиентов и продавать, разобраться в кредитах и "
            "банковских услугах. В конце — гранты, наставничество и инвестиции.",
            "This path can help you prepare to start your own business: writing a "
            "business plan, finding customers and selling, and understanding credit "
            "and banking. At the end, grants, mentoring and investment.",
        ),
        "category": C.OWN_BUSINESS,
        "level": L.BEGINNER,
        "skills": [
            "business-plan",
            "entrepreneurship",
            "marketing",
            "sales",
            "credit",
            "banking",
            "financial-model",
        ],
        "learning_path": "oz-biznesingni-boshlash",
        "opportunities": [O.GRANT, O.MENTORSHIP, O.INVESTMENT, O.MARKETPLACE],
    },
    {
        "slug": "hunarmand-tadbirkor",
        "title": ("Hunarmand tadbirkor", "Ремесленница-предприниматель", "Craft entrepreneur"),
        "summary": (
            "Qoʻl mehnati mahsulotini sotadi: narx, brend, qadoqlash va marketpleys.",
            "Продаёт изделия ручной работы: цена, бренд, упаковка и маркетплейс.",
            "Sells handmade goods: price, brand, packaging and a marketplace.",
        ),
        "description": (
            "Bu yoʻl sizga hunaringizni daromad manbaiga aylantirishga tayyorlanishda "
            "yordam beradi: mahsulotga narx qoʻyish, brend yaratish, mahsulot "
            "kartochkasini tuzish va marketpleysda sotish.",
            "Этот путь поможет подготовиться к тому, чтобы ремесло приносило доход: "
            "назначить цену, создать бренд, оформить карточку товара и продавать на "
            "маркетплейсе.",
            "This path can help you prepare to turn a craft into an income: pricing "
            "what you make, building a brand, writing a product listing and selling on "
            "a marketplace.",
        ),
        "category": C.OWN_BUSINESS,
        "level": L.INTERMEDIATE,
        "skills": [
            "handicraft",
            "sewing",
            "pricing",
            "branding",
            "product-listing",
            "marketplaces",
            "packaging",
        ],
        "learning_path": "hunardan-biznesgacha",
        "opportunities": [O.MARKETPLACE, O.GRANT, O.MENTORSHIP],
    },
]

#: Where the catalogue does not yet support a direction, and why. Kept here
#: rather than invented into existence.
#:
#: * English teacher: two English courses exist, but no course teaches
#:   teaching itself and a single vacancy asks for it. Worth a direction once a
#:   pedagogy course or more listings exist.
#: * Call-centre operator / cashier: listings exist, but every one of them has
#:   closed, and no course teaches working with clients.
#: * Health, parenting and digital safety: important to the platform, but they
#:   are areas of life rather than kinds of work, and nothing on the platform
#:   lists work in them.


def _i18n(values: tuple[str, str, str]) -> dict:
    uz, ru, en = values
    return {"uz": uz, "ru": ru, "en": en}


async def load_careers(session: AsyncSession) -> dict[str, int]:
    """Create or refresh the curated directions. Never duplicates, never deletes."""
    counts = {"careers": 0, "updated": 0, "skipped": 0, "unknown_skills": 0}
    now = datetime.now(UTC)

    known = set((await session.execute(select(Skill.slug).where(Skill.is_active))).scalars())
    paths = {path.slug: path.id for path in (await session.execute(select(LearningPath))).scalars()}

    for order, spec in enumerate(CAREERS):
        skills = [slug for slug in spec["skills"] if slug in known]
        unknown = len(spec["skills"]) - len(skills)
        if unknown:
            counts["unknown_skills"] += unknown
            logger.warning(
                "career path %s: %d of its skills are not in the taxonomy", spec["slug"], unknown
            )
        if not skills:
            # A direction with no skill WomanUP knows is not a direction.
            counts["skipped"] += 1
            continue

        learning_path_id = paths.get(spec["learning_path"]) if spec["learning_path"] else None
        if spec["learning_path"] and learning_path_id is None:
            logger.warning(
                "career path %s: learning path %s is not in the catalogue",
                spec["slug"],
                spec["learning_path"],
            )

        path = await session.scalar(select(CareerPath).where(CareerPath.slug == spec["slug"]))
        if path is None:
            path = CareerPath(slug=spec["slug"], published_at=now)
            session.add(path)
            counts["careers"] += 1
        else:
            counts["updated"] += 1

        path.title_i18n = _i18n(spec["title"])
        path.summary_i18n = _i18n(spec["summary"])
        path.description_i18n = _i18n(spec["description"])
        path.category = spec["category"]
        path.level = spec["level"]
        path.skill_slugs = skills
        path.learning_path_id = learning_path_id
        path.opportunity_types = [kind.value for kind in spec["opportunities"]]
        path.order_index = order
        path.is_published = True
        if path.published_at is None:
            path.published_at = now

    await session.flush()
    return counts


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        counts = await load_careers(session)
        await session.commit()

    logger.info("career paths ready: %s", counts)
    print(f"\n  {counts['careers']} ta yangi kasb yoʻli yaratildi.")
    print(f"  Yangilangan kasb yoʻllari: {counts['updated']}")
    print(f"  Taksonomiyada topilmagan koʻnikmalar: {counts['unknown_skills']}\n")


if __name__ == "__main__":
    asyncio.run(_main())
