"""The skill catalogue, and the backfill that connects it to what already exists.

Kept out of `seed.py` for the same reason the news is: this is vocabulary, not
logic, and `seed.py` clears `users` while this touches nothing it did not write.

Two jobs, both idempotent and safe to re-run:

* `load_skills` upserts the curated catalogue below. Every entry carries the
  spellings actually found on courses, listings and profiles, which is what
  folds "jamgʻarma" and "jamgarma", or "muloqot" and "kommunikatsiya", into one
  skill instead of four.
* `backfill` gives every label already in the database a taxonomy entry and
  turns what women have already done into evidence: the skills they listed,
  the courses they finished, the certificates they hold.

Run after a migration:  python -m app.seed_skills
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EnrollmentStatus, ScoreDimension, SkillCategory
from app.models.mentor import MentorProfile
from app.models.opportunity import Opportunity
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill
from app.services import skills as skill_service

logger = logging.getLogger(__name__)

D = ScoreDimension
C = SkillCategory

# (slug, category, dimensions, (uz, ru, en), extra aliases)
#
# The names are the display text; the aliases are what is written in the wild.
# Every label found in the seeded catalogue, the demo population and the
# questionnaire's own suggestions resolves to one of these.
CATALOGUE: list[dict[str, Any]] = [
    # ---- digital -------------------------------------------------------
    {
        "slug": "computer-literacy",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS, D.EMPLOYMENT],
        "names": ("Kompyuter savodxonligi", "Компьютерная грамотность", "Computer literacy"),
        "aliases": ["kompyuter savodxonligi", "kompyuter"],
    },
    {
        "slug": "digital-literacy",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Raqamli savodxonlik", "Цифровая грамотность", "Digital literacy"),
        "aliases": ["raqamli savodxonlik"],
    },
    {
        "slug": "excel",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS, D.EMPLOYMENT],
        "names": ("Excel", "Excel", "Excel"),
        "aliases": ["excel", "эксель"],
    },
    {
        "slug": "word",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Word", "Word", "Word"),
        "aliases": ["word", "ворд"],
    },
    {
        "slug": "powerpoint",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("PowerPoint", "PowerPoint", "PowerPoint"),
        "aliases": ["powerpoint"],
    },
    {
        "slug": "google-docs",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Google Docs", "Google Docs", "Google Docs"),
        "aliases": ["google docs"],
    },
    {
        "slug": "1c",
        "category": C.DIGITAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("1C", "1С", "1C"),
        "aliases": ["1c", "1с"],
    },
    {
        "slug": "canva",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS, D.ENTREPRENEURSHIP],
        "names": ("Canva", "Canva", "Canva"),
        "aliases": ["canva", "канва"],
    },
    {
        "slug": "photoshop",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Photoshop", "Photoshop", "Photoshop"),
        "aliases": ["photoshop"],
    },
    {
        "slug": "telegram",
        "category": C.DIGITAL,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Telegram", "Telegram", "Telegram"),
        "aliases": ["telegram"],
    },
    {
        "slug": "instagram",
        "category": C.DIGITAL,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Instagram", "Instagram", "Instagram"),
        "aliases": ["instagram"],
    },
    {
        "slug": "ai-tools",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Sunʼiy intellekt vositalari", "Инструменты ИИ", "AI tools"),
        "aliases": ["ai", "prompt", "ии"],
    },
    {
        "slug": "information-technology",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS, D.EMPLOYMENT],
        "names": ("IT", "IT", "IT"),
        "aliases": ["it", "ит"],
    },
    {
        "slug": "programming",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS, D.EMPLOYMENT],
        "names": ("Dasturlash", "Программирование", "Programming"),
        "aliases": ["dasturlash", "программирование", "python", "c++"],
    },
    {
        "slug": "cybersecurity",
        "category": C.DIGITAL,
        "dimensions": [D.EDUCATION_SKILLS],
        "names": ("Kiberxavfsizlik", "Кибербезопасность", "Cybersecurity"),
        "aliases": ["kiberxavfsizlik", "parol", "пароль"],
    },
    {
        "slug": "childrens-online-safety",
        "category": C.WELLBEING,
        "dimensions": [D.FAMILY_PARENTING],
        "names": ("Bolalar xavfsizligi", "Безопасность детей онлайн", "Children's online safety"),
        "aliases": ["bolalar xavfsizligi"],
    },
    # ---- professional --------------------------------------------------
    {
        "slug": "accounting",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT, D.FINANCIAL_LITERACY],
        "names": ("Buxgalteriya", "Бухгалтерия", "Accounting"),
        "aliases": ["buxgalteriya", "бухгалтерия", "accounting"],
    },
    {
        "slug": "tax",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Soliq", "Налоги", "Tax"),
        "aliases": ["soliq", "налоги"],
    },
    {
        "slug": "numeracy",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Hisob-kitob", "Работа с цифрами", "Working with numbers"),
        "aliases": ["hisob-kitob", "hisob kitob"],
    },
    {
        "slug": "document-management",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Hujjat yuritish", "Делопроизводство", "Document management"),
        "aliases": ["hujjat yuritish"],
    },
    {
        "slug": "cash-handling",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Kassa ishi", "Работа на кассе", "Cash handling"),
        "aliases": ["kassa"],
    },
    {
        "slug": "customer-service",
        "category": C.COMMUNICATION,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Mijozlar bilan ishlash", "Работа с клиентами", "Working with clients"),
        "aliases": ["mijozlar bilan ishlash"],
    },
    {
        "slug": "sales",
        "category": C.BUSINESS,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Sotuv", "Продажи", "Sales"),
        "aliases": ["sotuv", "продажи"],
    },
    {
        "slug": "cv-writing",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Rezyume yozish", "Составление резюме", "CV writing"),
        "aliases": ["rezyume", "резюме"],
    },
    {
        "slug": "job-interviews",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Ish suhbati", "Собеседование", "Job interviews"),
        "aliases": ["ish suhbati", "suhbat", "собеседование"],
    },
    {
        "slug": "portfolio",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Portfolio", "Портфолио", "Portfolio"),
        "aliases": ["portfolio", "портфолио"],
    },
    {
        "slug": "business-writing",
        "category": C.COMMUNICATION,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Biznes yozishma", "Деловая переписка", "Business writing"),
        "aliases": ["biznes yozishma"],
    },
    {
        "slug": "presentation",
        "category": C.COMMUNICATION,
        "dimensions": [D.EMPLOYMENT, D.SOCIAL_ACTIVITY],
        "names": ("Taqdimot", "Презентации", "Presentations"),
        "aliases": ["taqdimot", "презентация"],
    },
    {
        "slug": "project-management",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Loyiha boshqaruvi", "Управление проектами", "Project management"),
        "aliases": ["loyiha boshqaruvi", "loyiha"],
    },
    {
        "slug": "hr",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Kadrlar bilan ishlash", "Работа с кадрами", "HR"),
        "aliases": ["kadrlar"],
    },
    {
        "slug": "logistics",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Logistika", "Логистика", "Logistics"),
        "aliases": ["logistika"],
    },
    {
        "slug": "manufacturing",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Ishlab chiqarish", "Производство", "Manufacturing"),
        "aliases": ["ishlab chiqarish"],
    },
    {
        "slug": "agriculture",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Qishloq xoʻjaligi", "Сельское хозяйство", "Agriculture"),
        "aliases": ["qishloq xojaligi"],
    },
    {
        "slug": "food-production",
        "category": C.CRAFT,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Oziq-ovqat ishlab chiqarish", "Пищевое производство", "Food production"),
        "aliases": ["oziq-ovqat"],
    },
    {
        "slug": "packaging",
        "category": C.CRAFT,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Qadoqlash", "Упаковка", "Packaging"),
        "aliases": ["qadoqlash"],
    },
    {
        "slug": "teaching",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT, D.EDUCATION_SKILLS],
        "names": ("Pedagogika", "Педагогика", "Teaching"),
        "aliases": ["pedagogika", "oqituvchilik", "педагогика"],
    },
    {
        "slug": "psychology",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE],
        "names": ("Psixologiya", "Психология", "Psychology"),
        "aliases": ["psixologiya", "психология"],
    },
    {
        "slug": "certification",
        "category": C.PROFESSIONAL,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Sertifikatlash", "Сертификация", "Certification"),
        "aliases": ["sertifikat"],
    },
    # ---- business and money --------------------------------------------
    {
        "slug": "entrepreneurship",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Tadbirkorlik", "Предпринимательство", "Entrepreneurship"),
        "aliases": ["tadbirkorlik", "предпринимательство"],
    },
    {
        "slug": "business-plan",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Biznes-reja", "Бизнес-план", "Business plan"),
        "aliases": ["biznes-reja", "biznes reja", "бизнес-план"],
    },
    {
        "slug": "pricing",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Narx belgilash", "Ценообразование", "Pricing"),
        "aliases": ["narx belgilash"],
    },
    {
        "slug": "marketplaces",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Marketpleyslar", "Маркетплейсы", "Marketplaces"),
        "aliases": ["marketpleys", "маркетплейс"],
    },
    {
        "slug": "product-listing",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Mahsulot kartochkasi", "Карточка товара", "Product listing"),
        "aliases": ["mahsulot kartochkasi"],
    },
    {
        "slug": "ecommerce",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Onlayn savdo", "Онлайн-торговля", "E-commerce"),
        "aliases": ["e-commerce", "onlayn savdo", "электронная коммерция"],
    },
    {
        "slug": "branding",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Brending", "Брендинг", "Branding"),
        "aliases": ["brending"],
    },
    {
        "slug": "marketing",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP, D.EMPLOYMENT],
        "names": ("Marketing", "Маркетинг", "Marketing"),
        "aliases": ["marketing", "маркетинг"],
    },
    {
        "slug": "content-marketing",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Kontent marketing", "Контент-маркетинг", "Content marketing"),
        "aliases": ["kontent marketing"],
    },
    {
        "slug": "smm",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP, D.EDUCATION_SKILLS],
        "names": ("SMM", "SMM", "SMM"),
        "aliases": ["smm", "ijtimoiy tarmoqlar", "соцсети"],
    },
    {
        "slug": "copywriting",
        "category": C.COMMUNICATION,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Matn yozish", "Копирайтинг", "Writing"),
        "aliases": ["matn yozish"],
    },
    {
        "slug": "financial-model",
        "category": C.FINANCE,
        "dimensions": [D.ENTREPRENEURSHIP, D.FINANCIAL_LITERACY],
        "names": ("Moliyaviy model", "Финансовая модель", "Financial model"),
        "aliases": ["moliyaviy model"],
    },
    {
        "slug": "financial-planning",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Moliyaviy reja", "Финансовый план", "Financial planning"),
        "aliases": ["moliyaviy reja"],
    },
    {
        "slug": "budgeting",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Byudjet yuritish", "Ведение бюджета", "Budgeting"),
        "aliases": ["byudjet", "budjet", "бюджет"],
    },
    {
        "slug": "savings",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Jamgʻarma", "Сбережения", "Savings"),
        "aliases": ["jamgarma", "сбережения"],
    },
    {
        "slug": "credit",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Kredit", "Кредит", "Credit"),
        "aliases": ["kredit", "кредит"],
    },
    {
        "slug": "banking",
        "category": C.FINANCE,
        "dimensions": [D.FINANCIAL_LITERACY],
        "names": ("Bank xizmatlari", "Банковские услуги", "Banking"),
        "aliases": ["bank", "банк"],
    },
    {
        "slug": "grants",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP, D.INTERNATIONAL_INTEGRATION],
        "names": ("Grant arizasi", "Грантовые заявки", "Grant applications"),
        "aliases": ["grant", "грант"],
    },
    {
        "slug": "cooperatives",
        "category": C.BUSINESS,
        "dimensions": [D.ENTREPRENEURSHIP, D.SOCIAL_ACTIVITY],
        "names": ("Kooperatsiya", "Кооперация", "Cooperatives"),
        "aliases": ["kooperatsiya"],
    },
    # ---- craft ---------------------------------------------------------
    {
        "slug": "sewing",
        "category": C.CRAFT,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Tikuvchilik", "Швейное дело", "Sewing"),
        "aliases": ["tikuvchilik", "шитьё", "shite"],
    },
    {
        "slug": "overlock",
        "category": C.CRAFT,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Overlok", "Оверлок", "Overlock"),
        "aliases": ["overlok"],
    },
    {
        "slug": "pattern-making",
        "category": C.CRAFT,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Lekalo tayyorlash", "Построение лекал", "Pattern making"),
        "aliases": ["lekalo"],
    },
    {
        "slug": "handicraft",
        "category": C.CRAFT,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Hunarmandchilik", "Ремесло", "Handicraft"),
        "aliases": ["hunarmandchilik"],
    },
    {
        "slug": "design",
        "category": C.CRAFT,
        "dimensions": [D.EDUCATION_SKILLS, D.ENTREPRENEURSHIP],
        "names": ("Dizayn", "Дизайн", "Design"),
        "aliases": ["dizayn", "дизайн"],
    },
    {
        "slug": "cooking",
        "category": C.CRAFT,
        "dimensions": [D.ENTREPRENEURSHIP],
        "names": ("Pazandachilik", "Кулинария", "Cooking"),
        "aliases": ["pazandachilik"],
    },
    {
        "slug": "event-organising",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY, D.ENTREPRENEURSHIP],
        "names": ("Tadbir tashkil qilish", "Организация мероприятий", "Organising events"),
        "aliases": ["tadbir tashkil qilish"],
    },
    # ---- languages -----------------------------------------------------
    {
        "slug": "english",
        "category": C.LANGUAGE,
        "dimensions": [D.INTERNATIONAL_INTEGRATION, D.EDUCATION_SKILLS],
        "names": ("Ingliz tili", "Английский язык", "English"),
        "aliases": ["ingliz tili", "английский", "english"],
    },
    {
        "slug": "ielts",
        "category": C.LANGUAGE,
        "dimensions": [D.INTERNATIONAL_INTEGRATION],
        "names": ("IELTS", "IELTS", "IELTS"),
        "aliases": ["ielts"],
    },
    {
        "slug": "motivation-letter",
        "category": C.LANGUAGE,
        "dimensions": [D.INTERNATIONAL_INTEGRATION],
        "names": ("Motivatsion xat", "Мотивационное письмо", "Motivation letter"),
        "aliases": ["motivatsion xat"],
    },
    # ---- people and community ------------------------------------------
    {
        "slug": "communication",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY, D.EMPLOYMENT],
        "names": ("Muloqot", "Коммуникация", "Communication"),
        "aliases": ["muloqot", "kommunikatsiya", "коммуникация"],
    },
    {
        "slug": "public-speaking",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY],
        "names": ("Notiqlik", "Публичные выступления", "Public speaking"),
        "aliases": ["notiqlik"],
    },
    {
        "slug": "negotiation",
        "category": C.COMMUNICATION,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Muzokara", "Переговоры", "Negotiation"),
        "aliases": ["muzokara", "переговоры"],
    },
    {
        "slug": "leadership",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY, D.EMPLOYMENT],
        "names": ("Liderlik", "Лидерство", "Leadership"),
        "aliases": ["liderlik", "лидерство"],
    },
    {
        "slug": "teamwork",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY, D.EMPLOYMENT],
        "names": ("Jamoada ishlash", "Работа в команде", "Teamwork"),
        "aliases": ["jamoa"],
    },
    {
        "slug": "mentoring",
        "category": C.COMMUNICATION,
        "dimensions": [D.SOCIAL_ACTIVITY],
        "names": ("Mentorlik", "Наставничество", "Mentoring"),
        "aliases": ["mentorlik", "наставничество"],
    },
    {
        "slug": "volunteering",
        "category": C.CIVIC,
        "dimensions": [D.SOCIAL_ACTIVITY],
        "names": ("Volontyorlik", "Волонтёрство", "Volunteering"),
        "aliases": ["volontyorlik"],
    },
    # ---- rights --------------------------------------------------------
    {
        "slug": "labour-law",
        "category": C.CIVIC,
        "dimensions": [D.EMPLOYMENT],
        "names": ("Mehnat huquqi", "Трудовое право", "Labour law"),
        "aliases": ["mehnat huquqi"],
    },
    {
        "slug": "contracts",
        "category": C.CIVIC,
        "dimensions": [D.EMPLOYMENT, D.ENTREPRENEURSHIP],
        "names": ("Shartnomalar", "Договоры", "Contracts"),
        "aliases": ["shartnoma", "договор"],
    },
    {
        "slug": "family-law",
        "category": C.CIVIC,
        "dimensions": [D.FAMILY_PARENTING],
        "names": ("Oila huquqi", "Семейное право", "Family law"),
        "aliases": ["oila huquqi"],
    },
    # ---- wellbeing and family ------------------------------------------
    {
        "slug": "nutrition",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE],
        "names": ("Sogʻlom ovqatlanish", "Здоровое питание", "Nutrition"),
        "aliases": ["ovqatlanish", "питание"],
    },
    {
        "slug": "prevention",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE],
        "names": ("Profilaktika", "Профилактика", "Prevention"),
        "aliases": ["profilaktika", "профилактика"],
    },
    {
        "slug": "stress-management",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE],
        "names": ("Stressni boshqarish", "Управление стрессом", "Stress management"),
        "aliases": ["stress", "стресс"],
    },
    {
        "slug": "recovery",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE],
        "names": ("Tiklanish va dam olish", "Восстановление и отдых", "Rest and recovery"),
        "aliases": ["tiklanish"],
    },
    {
        "slug": "boundaries",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE, D.FAMILY_PARENTING],
        "names": ("Shaxsiy chegaralar", "Личные границы", "Personal boundaries"),
        "aliases": ["chegaralar"],
    },
    {
        "slug": "daily-routine",
        "category": C.WELLBEING,
        "dimensions": [D.HEALTHY_LIFESTYLE, D.FAMILY_PARENTING],
        "names": ("Kun tartibi", "Режим дня", "Daily routine"),
        "aliases": ["kun tartibi"],
    },
    {
        "slug": "parenting",
        "category": C.WELLBEING,
        "dimensions": [D.FAMILY_PARENTING],
        "names": ("Farzand tarbiyasi", "Воспитание детей", "Parenting"),
        "aliases": ["tarbiya", "bolalar", "воспитание"],
    },
    {
        "slug": "school-readiness",
        "category": C.WELLBEING,
        "dimensions": [D.FAMILY_PARENTING],
        "names": ("Maktabga tayyorgarlik", "Подготовка к школе", "School readiness"),
        "aliases": ["maktab"],
    },
    {
        "slug": "family-values",
        "category": C.CIVIC,
        "dimensions": [D.FAMILY_PARENTING, D.SOCIAL_ACTIVITY],
        "names": ("Qadriyatlar", "Ценности", "Values"),
        "aliases": ["qadriyatlar"],
    },
]


async def load_skills(session: AsyncSession) -> int:
    """Upsert the catalogue. The entry is the source of truth for its wording."""
    written = 0
    for item in CATALOGUE:
        skill = await session.scalar(select(Skill).where(Skill.slug == item["slug"]))
        if skill is None:
            skill = Skill(slug=item["slug"])
            session.add(skill)

        uz, ru, en = item["names"]
        skill.name_i18n = {"uz": uz, "ru": ru, "en": en}
        skill.category = item["category"]
        skill.dimensions = [dimension.value for dimension in item["dimensions"]]
        # The names count as spellings too, so a label written the way it is
        # displayed resolves without being listed twice.
        aliases = {skill_service.normalise(alias) for alias in (*item["aliases"], uz, ru, en)}
        skill.aliases = sorted(alias for alias in aliases if alias)
        skill.is_curated = True
        skill.is_active = True
        written += 1

    await session.flush()
    return written


async def backfill(session: AsyncSession) -> dict[str, int]:
    """Connect the catalogue to the records that already exist.

    Every label written on a course, a listing or a mentor profile gets an
    entry, so nothing in the live catalogue is unmatchable. Then what women
    have already done becomes evidence — listed skills as their own word,
    finished courses and certificates as learning.
    """
    counts = {"labels": 0, "profiles": 0, "completions": 0, "certificates": 0}

    labels: set[str] = set()
    for column in (Program.skills_taught, Opportunity.required_skills, MentorProfile.expertise):
        rows = await session.execute(select(column))
        for value in rows.scalars():
            labels.update(label for label in (value or []) if label and label.strip())

    index = await skill_service.SkillIndex.load(session, labels)
    for label in sorted(labels):
        await skill_service.ensure_skill(session, label, index=index)
        counts["labels"] += 1

    profiles = await session.execute(select(Profile).where(Profile.skills != []))
    for profile in profiles.scalars():
        await skill_service.sync_self_reported(session, profile.user_id, profile.skills)
        counts["profiles"] += 1

    # Finished is a status, not a timestamp: enrollments completed before the
    # platform started stamping `completed_at` still taught her something.
    completed = await session.execute(
        select(Enrollment, Program)
        .join(Program, Program.id == Enrollment.program_id)
        .where(
            or_(
                Enrollment.status == EnrollmentStatus.COMPLETED,
                Enrollment.completed_at.is_not(None),
            )
        )
    )
    for enrollment, program in completed.all():
        await skill_service.record_course_completion(
            session,
            user_id=enrollment.user_id,
            labels=program.skills_taught,
            enrollment_id=enrollment.id,
            occurred_at=enrollment.completed_at,
        )
        counts["completions"] += 1

    certificates = await session.execute(
        select(Certificate, Program)
        .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
        .join(Program, Program.id == Enrollment.program_id)
        .where(Certificate.revoked_at.is_(None))
    )
    for certificate, program in certificates.all():
        await skill_service.record_certificate(
            session,
            user_id=certificate.user_id,
            labels=program.skills_taught,
            certificate_id=certificate.id,
            occurred_at=certificate.issued_at,
        )
        counts["certificates"] += 1

    await session.flush()
    return counts


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        loaded = await load_skills(session)
        counts = await backfill(session)
        await session.commit()

    logger.info("loaded %s skills, backfilled %s", loaded, counts)
    print(f"\n  {loaded} ta koʻnikma yuklandi.")
    print(f"  Katalogdagi belgilar: {counts['labels']}")
    print(f"  Profillar: {counts['profiles']}")
    print(f"  Yakunlangan kurslar: {counts['completions']}")
    print(f"  Sertifikatlar: {counts['certificates']}\n")


if __name__ == "__main__":
    asyncio.run(_main())
