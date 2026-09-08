"""Demo data loader.

Populates a database with a realistic but entirely fictional dataset so the
portal can be demonstrated end to end. Safe to re-run: it clears the demo
tables first.

    python -m app.seed
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.core.constants import (
    ApplicationStatus,
    ConsentScope,
    EnrollmentStatus,
    GoalHorizon,
    Language,
    OpportunitySource,
    OpportunityType,
    PlanItemStatus,
    Priority,
    ProgramCategory,
    ProgramFormat,
    Region,
    RiskFlagType,
    Role,
    ScoreDimension,
    UserStatus,
    years_between,
)
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db import SessionLocal
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.audit import RiskFlag
from app.models.consent import ConsentLog
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.mentor import MentorProfile
from app.models.news import NewsPost
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.profile import Profile
from app.models.program import Enrollment, Program, ProgramModule
from app.models.user import User, UserRole
from app.seed_modules import MODULES as PROGRAM_MODULES
from app.seed_news import load_news
from app.services import rag
from app.services.news_age import group_for_age
from app.services.scoring import calculate_dimension_scores, persist_scores

logger = logging.getLogger(__name__)

# Deterministic dataset — the demo looks the same on every machine.
random.seed(20260819)

DEMO_ADMIN_PHONE = "+998900000001"
DEMO_USER_PHONE = "+998900000002"
# Sign-in is by e-mail, so the demo learner needs one she can actually type.
DEMO_USER_EMAIL = "demo@womanup.uz"
DEMO_USER_PASSWORD = "WomanUP2026"
DEMO_ADMIN_LOGIN = "admin@womanup.uz"
DEMO_ADMIN_PASSWORD = "WomanUP2026!"


def _merge(uz: str, ru: str | None, en: str | None) -> dict[str, str]:
    """Build an i18n map, omitting languages that have no translation yet.

    An absent key is what lets the reader fall back to Uzbek — writing an empty
    string instead would render as a blank card.
    """
    out = {"uz": uz}
    if ru:
        out["ru"] = ru
    if en:
        out["en"] = en
    return out


def _tri(value: tuple[str, str, str]) -> dict[str, str]:
    """(uz, ru, en) -> the i18n map the API and the frontend both expect."""
    return {"uz": value[0], "ru": value[1], "en": value[2]}


def _slug(value: str) -> str:
    """A stable, readable key for an opportunity.

    `external_id` plus `source` is the idempotency key the partner sync relies
    on, so it has to survive the full title — truncating to the first 20
    characters collided as soon as two grants shared an opening phrase.
    """
    out = []
    for char in value.lower():
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:90]


# --------------------------------------------------------------------------
# Vocabulary for the synthetic population. Names, professions and skills are
# ordinary Uzbek ones so the demo reads as a real register rather than
# "User 42" — but every combination below is fictional.
# --------------------------------------------------------------------------

FIRST_NAMES = [
    "Dilnoza",
    "Nilufar",
    "Zulfiya",
    "Malika",
    "Gulnora",
    "Sevara",
    "Kamola",
    "Nodira",
    "Shahnoza",
    "Feruza",
    "Umida",
    "Mohira",
    "Zarina",
    "Aziza",
    "Dilfuza",
    "Nasiba",
    "Gulbahor",
    "Mavluda",
    "Ozoda",
    "Rayhona",
    "Sabina",
    "Munisa",
    "Charos",
    "Lola",
    "Yulduz",
    "Nargiza",
    "Dildora",
    "Shahlo",
]
LAST_NAMES = [
    "Karimova",
    "Yusupova",
    "Rahmonova",
    "Tosheva",
    "Abdullayeva",
    "Ergasheva",
    "Sultonova",
    "Nazarova",
    "Qodirova",
    "Ismoilova",
    "Xolmatova",
    "Joʻrayeva",
    "Sharipova",
    "Bekmurodova",
    "Tursunova",
    "Alimova",
    "Saidova",
    "Umarova",
]
DISTRICTS = [
    "Markaziy tuman",
    "Yangi tuman",
    "Shimoliy tuman",
    "Chorbog' tumani",
    "Boʻston tumani",
    "Qishloq hududi",
    "Sanoat mavzesi",
]

# (education level, typical field)
EDUCATION = [
    ("oʻrta", None),
    ("oʻrta maxsus", "buxgalteriya"),
    ("oʻrta maxsus", "tikuvchilik"),
    ("oʻrta maxsus", "pedagogika"),
    ("oliy (bakalavr)", "iqtisodiyot"),
    ("oliy (bakalavr)", "filologiya"),
    ("oliy (bakalavr)", "pedagogika"),
    ("oliy (bakalavr)", "axborot texnologiyalari"),
    ("oliy (magistr)", "menejment"),
]

# (employment status, profession, skills) — weighted by how common each is.
OCCUPATIONS = [
    ("ish qidirmoqda", None, ["kompyuter savodxonligi"]),
    ("ish qidirmoqda", "buxgalter", ["1c", "excel", "buxgalteriya"]),
    ("ish qidirmoqda", "sotuvchi", ["mijozlar bilan ishlash", "kassa"]),
    ("band", "oʻqituvchi", ["pedagogika", "ingliz tili"]),
    ("band", "buxgalter", ["1c", "excel", "soliq"]),
    ("band", "tikuvchi", ["tikuvchilik", "overlok"]),
    ("band", "administrator", ["hujjat yuritish", "excel"]),
    ("oʻz ishi bor", "tadbirkor", ["marketpleys", "narx belgilash"]),
    ("oʻz ishi bor", "hunarmand", ["hunarmandchilik", "qadoqlash"]),
    ("uy bekasi", None, []),
    ("uy bekasi", "hunarmand", ["tikuvchilik"]),
    ("talaba", None, ["ingliz tili", "kompyuter savodxonligi"]),
]

INTERESTS = [
    "kasb egallash",
    "onlayn savdo",
    "ingliz tili",
    "moliyaviy savodxonlik",
    "farzand tarbiyasi",
    "sogʻliq",
    "liderlik",
    "hunarmandchilik",
    "raqamli koʻnikmalar",
    "xalqaro dasturlar",
    "mentorlik",
    "volontyorlik",
]

# The partner scope each opportunity source needs before anything is sent.
CONSENT_FOR_SOURCE = {
    OpportunitySource.EDU_JOB: ConsentScope.SHARE_EDU_JOB,
    OpportunitySource.INVEST_HUB: ConsentScope.SHARE_INVEST_HUB,
    OpportunitySource.COMMERCE: ConsentScope.SHARE_COMMERCE,
}

# Plan actions per dimension — what the planner proposes when a dimension is
# the user's weakest. Kept short and concrete, the way section 03 asks.
# Plan steps per dimension, in all three locales. PlanItem.action is a plain
# string in the database, so the language has to be chosen when the row is
# written — a Russian-speaking user must not open her plan and find Uzbek.
PLAN_ACTIONS: dict[ScoreDimension, list[tuple[str, str, str]]] = {
    ScoreDimension.EDUCATION_SKILLS: [
        (
            "Kasbiy dasturni tanlab, birinchi modulni tugatish",
            "Выбрать профессиональную программу и завершить первый модуль",
            "Pick a vocational programme and finish the first module",
        ),
        (
            "Yangi koʻnikmani profilga qoʻshish",
            "Добавить новый навык в профиль",
            "Add a new skill to your profile",
        ),
        (
            "Sertifikatli kursni yakunlash",
            "Завершить курс с сертификатом",
            "Complete a course that carries a certificate",
        ),
    ],
    ScoreDimension.EMPLOYMENT: [
        (
            "Rezyumeni yangilab, portalga yuklash",
            "Обновить резюме и загрузить его на портал",
            "Update your CV and upload it to the portal",
        ),
        (
            "Uchta mos vakansiyaga ariza berish",
            "Подать заявки на три подходящие вакансии",
            "Apply to three matching vacancies",
        ),
        (
            "Ish suhbatiga tayyorgarlik mashgʻulotidan oʻtish",
            "Пройти подготовку к собеседованию",
            "Work through the interview preparation session",
        ),
    ],
    ScoreDimension.ENTREPRENEURSHIP: [
        (
            "Biznes gʻoyani bir sahifada tavsiflash",
            "Описать бизнес-идею на одной странице",
            "Describe your business idea on one page",
        ),
        (
            "Tannarx va narxni hisoblash",
            "Рассчитать себестоимость и цену",
            "Work out your costs and your price",
        ),
        (
            "Marketpleysda sotuvchi profilini ochish",
            "Открыть профиль продавца на маркетплейсе",
            "Open a seller profile on a marketplace",
        ),
    ],
    ScoreDimension.FINANCIAL_LITERACY: [
        (
            "Oylik byudjet jadvalini yuritishni boshlash",
            "Начать вести таблицу месячного бюджета",
            "Start keeping a monthly budget sheet",
        ),
        (
            "Zaxira jamgʻarmasi uchun maqsad qoʻyish",
            "Поставить цель по резервному фонду",
            "Set a target for an emergency fund",
        ),
        (
            "Kredit shartlarini mustaqil tahlil qilish",
            "Самостоятельно разобрать условия кредита",
            "Read through loan terms on your own",
        ),
    ],
    ScoreDimension.HEALTHY_LIFESTYLE: [
        (
            "Yillik profilaktik tekshiruvdan oʻtish",
            "Пройти ежегодный профилактический осмотр",
            "Go for your annual preventive check-up",
        ),
        (
            "Kundalik harakat rejasini kiritish",
            "Ввести план ежедневной активности",
            "Put a daily activity plan in place",
        ),
        (
            "Tiklanish amaliyotini haftalik jadvalga qoʻshish",
            "Добавить практику восстановления в недельный график",
            "Add a recovery practice to your weekly schedule",
        ),
    ],
    ScoreDimension.FAMILY_PARENTING: [
        (
            "Oila uchun haftalik rejani kelishib olish",
            "Согласовать недельный план для семьи",
            "Agree a weekly plan with your family",
        ),
        (
            "Bola bilan kunlik oʻqish odatini boshlash",
            "Завести привычку ежедневного чтения с ребёнком",
            "Start a daily reading habit with your child",
        ),
        (
            "Farzand tarbiyasi boʻyicha dasturni tugatish",
            "Завершить программу по воспитанию детей",
            "Finish the parenting programme",
        ),
    ],
    ScoreDimension.SOCIAL_ACTIVITY: [
        (
            "Mahalladagi tashabbusga qoʻshilish",
            "Присоединиться к инициативе в махалле",
            "Join an initiative in your community",
        ),
        (
            "Kasbiy hamjamiyatda roʻyxatdan oʻtish",
            "Зарегистрироваться в профессиональном сообществе",
            "Register with a professional community",
        ),
        (
            "Mentor bilan birinchi uchrashuvni oʻtkazish",
            "Провести первую встречу с ментором",
            "Hold your first meeting with a mentor",
        ),
    ],
    ScoreDimension.INTERNATIONAL_INTEGRATION: [
        (
            "Ingliz tili darajasini baholash",
            "Оценить свой уровень английского",
            "Assess your level of English",
        ),
        (
            "Xalqaro dastur talablarini oʻrganish",
            "Изучить требования международной программы",
            "Study the requirements of an international programme",
        ),
        (
            "Motivatsion xat qoralamasini tayyorlash",
            "Подготовить черновик мотивационного письма",
            "Draft a motivation letter",
        ),
    ],
}

PLAN_TITLE = {
    "uz": "Individual rivojlanish rejasi",
    "ru": "Индивидуальный план развития",
    "en": "Individual development plan",
}
PLAN_SUMMARY = {
    "uz": "Diagnostika natijasiga koʻra eng zaif uchta yoʻnalish boʻyicha qadamlar.",
    "ru": "Шаги по трём самым слабым направлениям по результатам диагностики.",
    "en": "Steps across the three weakest dimensions from your diagnostic.",
}


# --------------------------------------------------------------------------
# Diagnostic instrument: 8 dimensions x 3 questions
# --------------------------------------------------------------------------

QUESTIONS: dict[ScoreDimension, list[tuple[str, str, str]]] = {
    ScoreDimension.EDUCATION_SKILLS: [
        (
            "Taʼlim darajangizni qanday baholaysiz?",
            "Как вы оцениваете свой уровень образования?",
            "How do you rate your level of education?",
        ),
        (
            "Soʻnggi yilda yangi kasbiy koʻnikma oʻrgandingizmi?",
            "Осваивали ли вы новый профессиональный навык за последний год?",
            "Have you learned a new professional skill in the past year?",
        ),
        (
            "Kompyuter va internetdan qanchalik erkin foydalanasiz?",
            "Насколько свободно вы пользуетесь компьютером и интернетом?",
            "How confidently do you use a computer and the internet?",
        ),
    ],
    ScoreDimension.EMPLOYMENT: [
        (
            "Hozirgi bandlik holatingiz qanday?",
            "Каков ваш текущий статус занятости?",
            "What is your current employment status?",
        ),
        (
            "Rezyume va suhbatga tayyorgarligingizni qanday baholaysiz?",
            "Как вы оцениваете готовность резюме и к собеседованию?",
            "How ready are your CV and interview skills?",
        ),
        (
            "Kasbiy aloqalar tarmogʻingiz qanchalik keng?",
            "Насколько широка ваша сеть профессиональных контактов?",
            "How wide is your professional network?",
        ),
    ],
    ScoreDimension.ENTREPRENEURSHIP: [
        (
            "Biznes yuritish tajribangiz bormi?",
            "Есть ли у вас опыт ведения бизнеса?",
            "Do you have experience running a business?",
        ),
        (
            "Biznes-reja tuza olasizmi?",
            "Умеете ли вы составить бизнес-план?",
            "Can you put together a business plan?",
        ),
        (
            "Mahsulot yoki xizmatingizni sotish kanallari bormi?",
            "Есть ли каналы сбыта вашего товара или услуги?",
            "Do you have sales channels for your product or service?",
        ),
    ],
    ScoreDimension.FINANCIAL_LITERACY: [
        (
            "Oilaviy budjetni muntazam yuritasizmi?",
            "Ведёте ли вы регулярно семейный бюджет?",
            "Do you keep a household budget regularly?",
        ),
        ("Jamgʻarmangiz bormi?", "Есть ли у вас сбережения?", "Do you have savings?"),
        (
            "Kredit shartlarini mustaqil tahlil qila olasizmi?",
            "Можете ли самостоятельно разобрать условия кредита?",
            "Can you assess loan terms on your own?",
        ),
    ],
    ScoreDimension.HEALTHY_LIFESTYLE: [
        (
            "Profilaktik tibbiy koʻrikdan muntazam oʻtasizmi?",
            "Проходите ли регулярно профилактические осмотры?",
            "Do you attend preventive health check-ups regularly?",
        ),
        (
            "Jismoniy faollik darajangiz qanday?",
            "Каков ваш уровень физической активности?",
            "What is your level of physical activity?",
        ),
        (
            "Uyqu va stressni boshqarish holatingiz qanday?",
            "Как обстоят дела со сном и управлением стрессом?",
            "How are your sleep and stress management?",
        ),
    ],
    ScoreDimension.FAMILY_PARENTING: [
        (
            "Farzand tarbiyasi boʻyicha yetarli bilimingiz bormi?",
            "Достаточно ли у вас знаний по воспитанию детей?",
            "Do you have enough knowledge about raising children?",
        ),
        (
            "Oilada muloqot sifatini qanday baholaysiz?",
            "Как оцениваете качество общения в семье?",
            "How do you rate communication in your family?",
        ),
        (
            "Bolalarning onlayn xavfsizligini nazorat qilasizmi?",
            "Контролируете ли онлайн-безопасность детей?",
            "Do you supervise your children's online safety?",
        ),
    ],
    ScoreDimension.SOCIAL_ACTIVITY: [
        (
            "Mahalla yoki jamoat tashabbuslarida qatnashasizmi?",
            "Участвуете ли в инициативах махалли или сообщества?",
            "Do you take part in community initiatives?",
        ),
        (
            "Boshqalarga maslahat berish tajribangiz bormi?",
            "Есть ли опыт наставничества для других?",
            "Do you have experience mentoring others?",
        ),
        (
            "Oʻz huquqlaringizni bilasizmi va himoya qila olasizmi?",
            "Знаете ли свои права и умеете ли их защищать?",
            "Do you know your rights and can you defend them?",
        ),
    ],
    ScoreDimension.INTERNATIONAL_INTEGRATION: [
        (
            "Chet tilini qay darajada bilasiz?",
            "На каком уровне вы владеете иностранным языком?",
            "What is your foreign language level?",
        ),
        (
            "Xalqaro dastur yoki grantlarda qatnashganmisiz?",
            "Участвовали ли в международных программах или грантах?",
            "Have you taken part in international programmes or grants?",
        ),
        (
            "Eksport yoki xalqaro hamkorlik tajribangiz bormi?",
            "Есть ли опыт экспорта или международного сотрудничества?",
            "Do you have export or international cooperation experience?",
        ),
    ],
}

# Five-point scale reused by every question.
OPTIONS = [
    {"value": 0, "label_i18n": {"uz": "Umuman yoʻq", "ru": "Совсем нет", "en": "Not at all"}},
    {"value": 25, "label_i18n": {"uz": "Juda kam", "ru": "Очень мало", "en": "Very little"}},
    {"value": 50, "label_i18n": {"uz": "Oʻrtacha", "ru": "Средне", "en": "Moderate"}},
    {"value": 75, "label_i18n": {"uz": "Yaxshi", "ru": "Хорошо", "en": "Good"}},
    {"value": 100, "label_i18n": {"uz": "Aʼlo darajada", "ru": "Отлично", "en": "Excellent"}},
]


PROGRAMS = [
    (
        "buxgalteriya-asoslari",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.VIDEO,
        "Buxgalteriya asoslari",
        "Основы бухгалтерии",
        "Bookkeeping basics",
        "Noldan buxgalteriya hisobini yuritishni oʻrganing va 1C bilan ishlashni boshlang.",
        40,
        6,
        ["1c", "excel", "buxgalteriya"],
        True,
        [
            "Birlamchi hujjatlarni rasmiylashtirish",
            "1C da operatsiyalarni kiritish",
            "Soliq hisobotini tayyorlash",
            "Oylik balansni yopish",
        ],
    ),
    (
        "smm-va-raqamli-marketing",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.BLENDED,
        "SMM va raqamli marketing",
        "SMM и digital-маркетинг",
        "SMM and digital marketing",
        "Ijtimoiy tarmoqlarda brend yuritish, kontent rejalashtirish va reklama sozlash.",
        32,
        4,
        ["smm", "canva", "kontent marketing"],
        True,
        [
            "Kontent-reja tuzish",
            "Target reklama sozlash",
            "Statistikani tahlil qilish",
            "Brend ovozini shakllantirish",
        ],
    ),
    (
        "tikuvchilik-va-dizayn",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.OFFLINE,
        "Tikuvchilik va dizayn",
        "Швейное дело и дизайн",
        "Sewing and design",
        "Milliy va zamonaviy kiyim tikish, oʻlchov olish va kichik sex tashkil etish.",
        60,
        8,
        ["tikuvchilik", "dizayn", "lekalo"],
        True,
        [
            "Lekalo tayyorlash",
            "Buyurtma boʻyicha tikish",
            "Narx belgilash",
            "Kichik sex jarayonini tashkil etish",
        ],
    ),
    (
        "ai-va-raqamli-savodxonlik",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.VIDEO,
        "AI va raqamli savodxonlik",
        "ИИ и цифровая грамотность",
        "AI and digital literacy",
        "Sunʼiy intellekt vositalaridan kundalik ish va biznesda foydalanish.",
        24,
        4,
        ["ai", "prompt", "raqamli savodxonlik"],
        True,
        [
            "AI yordamchilardan foydalanish",
            "Hujjat va matn tayyorlash",
            "Maʼlumotni tekshirish",
            "Ishni avtomatlashtirish",
        ],
    ),
    (
        "moliyaviy-savodxonlik",
        ProgramCategory.FINANCIAL_LITERACY,
        ProgramFormat.TEXT,
        "Moliyaviy savodxonlik",
        "Финансовая грамотность",
        "Financial literacy",
        "Oilaviy budjet, jamgʻarma, kredit madaniyati va firibgarlikdan himoya.",
        16,
        3,
        ["budjet", "jamgarma", "kredit"],
        True,
        [
            "Oilaviy budjet tuzish",
            "Jamgʻarma rejasini yaratish",
            "Kredit shartlarini tahlil qilish",
            "Moliyaviy firibgarlikni aniqlash",
        ],
    ),
    (
        "tadbirkorlik-asoslari",
        ProgramCategory.ENTREPRENEURSHIP,
        ProgramFormat.BLENDED,
        "Tadbirkorlik asoslari",
        "Основы предпринимательства",
        "Entrepreneurship basics",
        "Gʻoyadan roʻyxatdan oʻtgan biznesgacha: model, narx, mijoz va birinchi sotuv.",
        48,
        8,
        ["biznes-reja", "marketing", "sotuv"],
        True,
        [
            "Biznes modelni tavsiflash",
            "Moliyaviy modelni hisoblash",
            "YaTT roʻyxatdan oʻtkazish",
            "Birinchi mijozni topish",
            "Investor uchun pitch tayyorlash",
        ],
    ),
    (
        "liderlik-va-notiqlik",
        ProgramCategory.LEADERSHIP,
        ProgramFormat.LIVE,
        "Liderlik va notiqlik",
        "Лидерство и публичные выступления",
        "Leadership and public speaking",
        "Maqsad qoʻyish, vaqt boshqaruvi, ommaviy nutq va muzokara olib borish.",
        20,
        4,
        ["notiqlik", "muzokara", "liderlik"],
        False,
        [
            "Maqsadni SMART formatda qoʻyish",
            "5 daqiqalik nutq tayyorlash",
            "Muzokarada pozitsiyani himoya qilish",
        ],
    ),
    (
        "huquqiy-savodxonlik",
        ProgramCategory.LEGAL_LITERACY,
        ProgramFormat.TEXT,
        "Huquqiy savodxonlik",
        "Правовая грамотность",
        "Legal literacy",
        "Mehnat, oila va tadbirkorlik huquqi boʻyicha amaliy qoʻllanma.",
        12,
        2,
        ["mehnat huquqi", "shartnoma"],
        False,
        ["Mehnat shartnomasini oʻqish", "Dekret kafolatlarini bilish", "Murojaat arizasini yozish"],
    ),
    (
        "onalar-maktabi",
        ProgramCategory.PARENTING,
        ProgramFormat.VIDEO,
        "Onalar maktabi",
        "Школа матерей",
        "Mothers' school",
        "Yosh bosqichlari boʻyicha tarbiya, muloqot va bolaning onlayn xavfsizligi.",
        18,
        4,
        ["tarbiya", "bolalar xavfsizligi"],
        True,
        [
            "Yosh bosqichiga mos muloqot",
            "Taʼlimga motivatsiya berish",
            "Onlayn xavfsizlikni taʼminlash",
            "Kasbga yoʻnaltirish",
        ],
    ),
    (
        "soglom-turmush",
        ProgramCategory.HEALTH,
        ProgramFormat.VIDEO,
        "Sogʻlom turmush tarzi",
        "Здоровый образ жизни",
        "Healthy lifestyle",
        "Profilaktika, ovqatlanish, jismoniy faollik va stressni boshqarish.",
        14,
        3,
        ["profilaktika", "ovqatlanish"],
        False,
        [
            "Profilaktik koʻrik rejasini tuzish",
            "Kunlik faollik normasini bajarish",
            "Stressni boshqarish texnikasi",
        ],
    ),
    (
        "ingliz-tili-b1",
        ProgramCategory.INTERNATIONAL,
        ProgramFormat.LIVE,
        "Ingliz tili B1",
        "Английский язык B1",
        "English B1",
        "Xalqaro dastur va grantlarga ariza berish uchun yetarli daraja.",
        72,
        12,
        ["ingliz tili", "motivatsion xat"],
        True,
        [
            "Kundalik muloqotni olib borish",
            "Motivatsion xat yozish",
            "Intervyuda oʻzini tanishtirish",
            "Grant arizasini toʻldirish",
        ],
    ),
    (
        "raqamli-xavfsizlik",
        ProgramCategory.DIGITAL_SAFETY,
        ProgramFormat.TEXT,
        "Raqamli xavfsizlik",
        "Цифровая безопасность",
        "Digital safety",
        "Parol, fishing, shaxsiy maʼlumot va bolalarni onlayn himoya qilish.",
        8,
        2,
        ["kiberxavfsizlik", "parol"],
        False,
        ["Kuchli parol tizimini qurish", "Fishingni aniqlash", "Shaxsiy maʼlumotni cheklash"],
    ),
    (
        "oilaviy-byudjet",
        ProgramCategory.FINANCIAL_LITERACY,
        ProgramFormat.TEXT,
        "Oilaviy byudjetni rejalashtirish",
        "Планирование семейного бюджета",
        "Household budget planning",
        "Daromad va xarajatlarni hisobga olish, jamgʻarma yigʻish va qarzdan chiqish yoʻllari.",
        16,
        3,
        ["byudjet", "jamgʻarma", "moliyaviy reja"],
        True,
        [
            "Oylik byudjet jadvalini yuritish",
            "Majburiy va ixtiyoriy xarajatlarni ajratish",
            "Zaxira jamgʻarmasini shakllantirish",
        ],
    ),
    (
        "mikrokredit-va-bank",
        ProgramCategory.FINANCIAL_LITERACY,
        ProgramFormat.BLENDED,
        "Mikrokredit va bank bilan ishlash",
        "Микрокредит и работа с банком",
        "Microcredit and working with a bank",
        "Kredit shartlarini oʻqish, biznes-reja tayyorlash va bankka murojaat qilish tartibi.",
        20,
        4,
        ["kredit", "biznes-reja", "bank"],
        True,
        [
            "Kredit shartnomasini tahlil qilish",
            "Foiz va jarimalarni hisoblash",
            "Bank uchun hujjatlar toʻplamini yigʻish",
        ],
    ),
    (
        "onlayn-savdo-boshlash",
        ProgramCategory.ENTREPRENEURSHIP,
        ProgramFormat.VIDEO,
        "Onlayn savdoni noldan boshlash",
        "Запуск онлайн-торговли с нуля",
        "Starting online sales from scratch",
        "Marketpleysda doʻkon ochish, mahsulot kartochkasi va birinchi savdogacha boʻlgan yoʻl.",
        28,
        5,
        ["marketpleys", "mahsulot kartochkasi", "logistika"],
        True,
        [
            "Marketpleysda sotuvchi profilini ochish",
            "Mahsulot kartochkasini toʻgʻri toʻldirish",
            "Narx va yetkazib berishni hisoblash",
        ],
    ),
    (
        "hunarmandchilik-biznesi",
        ProgramCategory.ENTREPRENEURSHIP,
        ProgramFormat.OFFLINE,
        "Hunarmandchilikni biznesga aylantirish",
        "Превращаем ремесло в бизнес",
        "Turning a craft into a business",
        "Uy hunarmandchiligidan barqaror daromadga: narx, brend va sotuv kanallari.",
        36,
        6,
        ["hunarmandchilik", "brending", "narx belgilash"],
        True,
        [
            "Mahsulot tannarxini hisoblash",
            "Brend nomi va qadoqni tayyorlash",
            "Doimiy xaridor bazasini yigʻish",
        ],
    ),
    (
        "ish-suhbatiga-tayyorgarlik",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.LIVE,
        "Rezyume va ish suhbati",
        "Резюме и собеседование",
        "CV and job interview",
        "Rezyume yozish, portfolio yigʻish va ish suhbatida oʻzini ishonchli tutish.",
        12,
        2,
        ["rezyume", "suhbat", "portfolio"],
        True,
        [
            "Vakansiyaga mos rezyume tayyorlash",
            "Suhbatning tipik savollariga javob berish",
            "Ish haqi haqida muzokara olib borish",
        ],
    ),
    (
        "ingliz-tili-ish-uchun",
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramFormat.BLENDED,
        "Ish uchun ingliz tili (A2-B1)",
        "Английский для работы (A2-B1)",
        "English for work (A2-B1)",
        "Xat yozish, qisqa uchrashuv oʻtkazish va kasbiy lugʻatni oʻzlashtirish.",
        60,
        12,
        ["ingliz tili", "biznes yozishma"],
        True,
        [
            "Ish xatini mustaqil yozish",
            "Qisqa taqdimot qilish",
            "Kasbiy lugʻatdan foydalanish",
        ],
    ),
    (
        "ayol-rahbar",
        ProgramCategory.LEADERSHIP,
        ProgramFormat.LIVE,
        "Ayol rahbar: jamoa boshqaruvi",
        "Женщина-руководитель: управление командой",
        "Women in leadership: managing a team",
        "Jamoa tuzish, vazifa taqsimlash va qiyin suhbatlarni olib borish koʻnikmalari.",
        24,
        6,
        ["liderlik", "jamoa", "kommunikatsiya"],
        True,
        [
            "Jamoa uchun maqsad qoʻyish",
            "Vazifalarni delegatsiya qilish",
            "Konfliktni konstruktiv hal qilish",
        ],
    ),
    (
        "jamoatchilik-nutqi",
        ProgramCategory.LEADERSHIP,
        ProgramFormat.VIDEO,
        "Jamoatchilik oldida nutq soʻzlash",
        "Публичные выступления",
        "Public speaking",
        "Qoʻrquvni yengish, nutq tuzilmasi va auditoriya bilan ishlash.",
        14,
        3,
        ["notiqlik", "taqdimot"],
        False,
        [
            "Nutq tuzilmasini rejalashtirish",
            "Auditoriya savollariga javob berish",
            "Sahna hayajonini boshqarish",
        ],
    ),
    (
        "ayollar-salomatligi",
        ProgramCategory.HEALTH,
        ProgramFormat.VIDEO,
        "Ayollar salomatligi: profilaktika",
        "Женское здоровье: профилактика",
        "Women's health: prevention",
        "Muntazam tekshiruvlar, ovqatlanish va stressni boshqarish boʻyicha amaliy tavsiyalar.",
        10,
        2,
        ["profilaktika", "ovqatlanish"],
        False,
        [
            "Yillik tekshiruv rejasini tuzish",
            "Kundalik ovqatlanishni muvozanatlash",
            "Ogohlantiruvchi belgilarni erta tanib olish",
        ],
    ),
    (
        "emotsional-barqarorlik",
        ProgramCategory.HEALTH,
        ProgramFormat.AUDIO,
        "Emotsional barqarorlik va tiklanish",
        "Эмоциональная устойчивость",
        "Emotional resilience",
        "Charchoqni tanib olish, chegara qoʻyish va yordam soʻrash koʻnikmasi.",
        8,
        2,
        ["stress", "chegaralar", "tiklanish"],
        False,
        [
            "Charchoq belgilarini erta aniqlash",
            "Kundalik tiklanish amaliyotini kiritish",
            "Chegara qoʻyish haqida suhbat qurish",
        ],
    ),
    (
        "bolaning-maktabga-tayyorgarligi",
        ProgramCategory.PARENTING,
        ProgramFormat.TEXT,
        "Bolani maktabga tayyorlash",
        "Подготовка ребёнка к школе",
        "Preparing a child for school",
        "Kognitiv va ijtimoiy tayyorgarlik, kun tartibi va motivatsiya.",
        12,
        3,
        ["bolalar", "maktab", "kun tartibi"],
        False,
        [
            "Bola uchun kun tartibini tuzish",
            "Oʻqishga qiziqishni qoʻllab-quvvatlash",
            "Maktabgacha koʻnikmalarni baholash",
        ],
    ),
    (
        "oiladagi-muloqot",
        ProgramCategory.ETHICS_CULTURE,
        ProgramFormat.VIDEO,
        "Oilada hurmatli muloqot",
        "Уважительное общение в семье",
        "Respectful communication in the family",
        "Milliy qadriyatlarga tayangan holda oilada muloqot va nizolarni hal qilish.",
        10,
        2,
        ["muloqot", "qadriyatlar"],
        False,
        [
            "Faol tinglash amaliyoti",
            "Nizoni tinch yoʻl bilan hal qilish",
            "Oilaviy kelishuvlarni ogʻzaki mustahkamlash",
        ],
    ),
    (
        "mentorlik-asoslari",
        ProgramCategory.MENTORSHIP_NETWORKING,
        ProgramFormat.LIVE,
        "Mentorlik asoslari",
        "Основы менторства",
        "Foundations of mentoring",
        "Mentor sifatida ish boshlash: uchrashuv tuzilmasi, chegaralar va natijani oʻlchash.",
        18,
        4,
        ["mentorlik", "kommunikatsiya"],
        True,
        [
            "Mentorlik uchrashuvini tuzilmalash",
            "Mentee uchun maqsad qoʻyish",
            "Natijani baholash",
        ],
    ),
    (
        "mahalla-tashabbusi",
        ProgramCategory.VOLUNTEERING,
        ProgramFormat.OFFLINE,
        "Mahalla tashabbusini boshlash",
        "Запуск инициативы в махалле",
        "Starting a community initiative",
        "Muammoni aniqlash, jamoani yigʻish va kichik loyihani amalga oshirish.",
        16,
        4,
        ["jamoa", "loyiha", "volontyorlik"],
        False,
        [
            "Mahalladagi ehtiyojni aniqlash",
            "Kichik loyiha rejasini tuzish",
            "Natijani jamoaga taqdim etish",
        ],
    ),
]


# Russian and English for the parts of a programme card that were Uzbek-only:
# the goal line and the learning outcomes. Keyed by slug and kept beside the
# catalogue rather than inside the tuples, so the catalogue stays readable and
# a missing translation degrades to Uzbek instead of breaking the card.
PROGRAM_TRANSLATIONS: dict[str, dict[str, object]] = {
    "buxgalteriya-asoslari": {
        "goal": (
            "Научитесь вести бухгалтерский учёт с нуля и работать в 1С.",
            "Learn to keep the books from scratch and start working in 1C.",
        ),
        "outcomes": [
            ("Оформление первичных документов", "Preparing primary documents"),
            ("Ввод операций в 1С", "Entering transactions in 1C"),
            ("Подготовка налоговой отчётности", "Preparing tax reports"),
            ("Закрытие месячного баланса", "Closing the monthly balance"),
        ],
    },
    "smm-va-raqamli-marketing": {
        "goal": (
            "Ведение бренда в соцсетях, планирование контента и настройка рекламы.",
            "Running a brand on social media, planning content and setting up ads.",
        ),
        "outcomes": [
            ("Составление контент-плана", "Building a content plan"),
            ("Настройка таргетированной рекламы", "Setting up targeted ads"),
            ("Анализ статистики", "Analysing the statistics"),
            ("Формирование голоса бренда", "Shaping the brand voice"),
        ],
    },
    "tikuvchilik-va-dizayn": {
        "goal": (
            "Пошив национальной и современной одежды, снятие мерок и организация небольшого цеха.",
            "Sewing traditional and modern clothing, taking measurements, running a workshop.",
        ),
        "outcomes": [
            ("Подготовка лекал", "Preparing patterns"),
            ("Пошив на заказ", "Sewing to order"),
            ("Ценообразование", "Setting prices"),
            ("Организация работы небольшого цеха", "Organising a small workshop"),
        ],
    },
    "ai-va-raqamli-savodxonlik": {
        "goal": (
            "Использование инструментов искусственного интеллекта в работе и бизнесе.",
            "Using artificial intelligence tools in daily work and in business.",
        ),
        "outcomes": [
            ("Работа с ИИ-помощниками", "Working with AI assistants"),
            ("Подготовка документов и текстов", "Preparing documents and texts"),
            ("Проверка достоверности информации", "Checking whether information holds up"),
            ("Автоматизация рутины", "Automating routine work"),
        ],
    },
    "moliyaviy-savodxonlik": {
        "goal": (
            "Семейный бюджет, сбережения, культура кредита и защита от мошенничества.",
            "Household budgets, savings, borrowing sensibly and spotting fraud.",
        ),
        "outcomes": [
            ("Составление семейного бюджета", "Building a household budget"),
            ("План накоплений", "Making a savings plan"),
            ("Разбор условий кредита", "Reading the terms of a loan"),
            ("Распознавание финансового мошенничества", "Recognising financial fraud"),
        ],
    },
    "tadbirkorlik-asoslari": {
        "goal": (
            "От идеи до зарегистрированного бизнеса: модель, цена, клиент и первая продажа.",
            "From idea to a registered business: model, price, customer and first sale.",
        ),
        "outcomes": [
            ("Описание бизнес-модели", "Describing the business model"),
            ("Расчёт финансовой модели", "Building the financial model"),
            ("Регистрация ИП", "Registering as a sole trader"),
            ("Поиск первого клиента", "Finding the first customer"),
            ("Подготовка питча для инвестора", "Preparing an investor pitch"),
        ],
    },
    "liderlik-va-notiqlik": {
        "goal": (
            "Постановка целей, управление временем, публичная речь и переговоры.",
            "Setting goals, managing time, speaking publicly and negotiating.",
        ),
        "outcomes": [
            ("Постановка цели по SMART", "Setting a goal in SMART form"),
            ("Подготовка пятиминутного выступления", "Preparing a five-minute talk"),
            ("Защита позиции в переговорах", "Holding a position in a negotiation"),
        ],
    },
    "huquqiy-savodxonlik": {
        "goal": (
            "Практическое руководство по трудовому, семейному и предпринимательскому праву.",
            "A practical guide to labour, family and business law.",
        ),
        "outcomes": [
            ("Чтение трудового договора", "Reading an employment contract"),
            ("Знание декретных гарантий", "Knowing your maternity entitlements"),
            ("Составление обращения", "Writing a formal complaint"),
        ],
    },
    "onalar-maktabi": {
        "goal": (
            "Воспитание по возрастам, общение и онлайн-безопасность ребёнка.",
            "Age-appropriate parenting, communication and a child's safety online.",
        ),
        "outcomes": [
            ("Общение с учётом возраста", "Communicating in an age-appropriate way"),
            ("Мотивация к учёбе", "Motivating a child to learn"),
            ("Обеспечение онлайн-безопасности", "Keeping a child safe online"),
            ("Профориентация", "Guiding career choices"),
        ],
    },
    "soglom-turmush": {
        "goal": (
            "Профилактика, питание, физическая активность и управление стрессом.",
            "Prevention, nutrition, physical activity and managing stress.",
        ),
        "outcomes": [
            ("План профилактических осмотров", "Planning preventive check-ups"),
            ("Выполнение дневной нормы активности", "Meeting a daily activity target"),
            ("Техники управления стрессом", "Techniques for managing stress"),
        ],
    },
    "ingliz-tili-b1": {
        "goal": (
            "Уровень, достаточный для заявок на международные программы и гранты.",
            "The level you need to apply for international programmes and grants.",
        ),
        "outcomes": [
            ("Ведение повседневного разговора", "Holding an everyday conversation"),
            ("Написание мотивационного письма", "Writing a motivation letter"),
            ("Самопрезентация на интервью", "Introducing yourself in an interview"),
            ("Заполнение грантовой заявки", "Filling in a grant application"),
        ],
    },
    "raqamli-xavfsizlik": {
        "goal": (
            "Пароли, фишинг, личные данные и защита детей в интернете.",
            "Passwords, phishing, personal data and protecting children online.",
        ),
        "outcomes": [
            ("Построение системы надёжных паролей", "Building a system of strong passwords"),
            ("Распознавание фишинга", "Spotting phishing"),
            ("Ограничение личных данных", "Limiting what personal data you share"),
        ],
    },
    "oilaviy-byudjet": {
        "goal": (
            "Учёт доходов и расходов, накопления и выход из долгов.",
            "Tracking income and spending, saving, and getting out of debt.",
        ),
        "outcomes": [
            ("Ведение таблицы месячного бюджета", "Keeping a monthly budget sheet"),
            (
                "Разделение обязательных и необязательных трат",
                "Separating essential from optional spending",
            ),
            ("Формирование резервного фонда", "Building an emergency fund"),
        ],
    },
    "mikrokredit-va-bank": {
        "goal": (
            "Чтение условий кредита, подготовка бизнес-плана и обращение в банк.",
            "Reading loan terms, preparing a business plan and approaching a bank.",
        ),
        "outcomes": [
            ("Анализ кредитного договора", "Analysing a loan agreement"),
            ("Расчёт процентов и штрафов", "Calculating interest and penalties"),
            ("Сбор пакета документов для банка", "Assembling the bank's document pack"),
        ],
    },
    "onlayn-savdo-boshlash": {
        "goal": (
            "Открытие магазина на маркетплейсе, карточка товара и путь до первой продажи.",
            "Opening a marketplace shop, the product listing, and the road to a first sale.",
        ),
        "outcomes": [
            ("Открытие профиля продавца", "Opening a seller profile"),
            ("Правильное заполнение карточки товара", "Filling in a product listing properly"),
            ("Расчёт цены и доставки", "Costing the price and the delivery"),
        ],
    },
    "hunarmandchilik-biznesi": {
        "goal": (
            "От домашнего ремесла к устойчивому доходу: цена, бренд и каналы продаж.",
            "From a home craft to steady income: pricing, branding and sales channels.",
        ),
        "outcomes": [
            ("Расчёт себестоимости изделия", "Costing a product"),
            ("Название бренда и упаковка", "Choosing a brand name and packaging"),
            ("Сбор базы постоянных покупателей", "Building a base of repeat customers"),
        ],
    },
    "ish-suhbatiga-tayyorgarlik": {
        "goal": (
            "Написание резюме, сбор портфолио и уверенное поведение на собеседовании.",
            "Writing a CV, assembling a portfolio and holding your own in an interview.",
        ),
        "outcomes": [
            ("Резюме под конкретную вакансию", "Tailoring a CV to a vacancy"),
            ("Ответы на типичные вопросы интервью", "Answering the usual interview questions"),
            ("Переговоры о зарплате", "Negotiating pay"),
        ],
    },
    "ingliz-tili-ish-uchun": {
        "goal": (
            "Деловая переписка, короткие встречи и профессиональная лексика.",
            "Business correspondence, short meetings and professional vocabulary.",
        ),
        "outcomes": [
            ("Самостоятельное деловое письмо", "Writing a work email unaided"),
            ("Короткая презентация", "Giving a short presentation"),
            ("Использование профессиональной лексики", "Using professional vocabulary"),
        ],
    },
    "ayol-rahbar": {
        "goal": (
            "Формирование команды, распределение задач и трудные разговоры.",
            "Building a team, delegating work and handling difficult conversations.",
        ),
        "outcomes": [
            ("Постановка цели для команды", "Setting a goal for the team"),
            ("Делегирование задач", "Delegating tasks"),
            ("Конструктивное разрешение конфликта", "Resolving conflict constructively"),
        ],
    },
    "jamoatchilik-nutqi": {
        "goal": (
            "Преодоление страха, структура речи и работа с аудиторией.",
            "Getting past the fear, structuring a talk and working an audience.",
        ),
        "outcomes": [
            ("Планирование структуры речи", "Planning the structure of a talk"),
            ("Ответы на вопросы аудитории", "Answering questions from the room"),
            ("Управление волнением на сцене", "Managing nerves on stage"),
        ],
    },
    "ayollar-salomatligi": {
        "goal": (
            "Регулярные осмотры, питание и управление стрессом — практические советы.",
            "Regular check-ups, nutrition and stress — practical guidance.",
        ),
        "outcomes": [
            ("План ежегодного обследования", "Planning an annual check-up"),
            ("Сбалансированное ежедневное питание", "Balancing daily nutrition"),
            ("Раннее распознавание тревожных признаков", "Spotting warning signs early"),
        ],
    },
    "emotsional-barqarorlik": {
        "goal": (
            "Распознавание усталости, умение ставить границы и просить о помощи.",
            "Recognising burnout, setting boundaries and asking for help.",
        ),
        "outcomes": [
            ("Раннее распознавание усталости", "Spotting burnout early"),
            ("Ежедневная практика восстановления", "Building a daily recovery habit"),
            ("Разговор о границах", "Holding a conversation about boundaries"),
        ],
    },
    "bolaning-maktabga-tayyorgarligi": {
        "goal": (
            "Когнитивная и социальная готовность, режим дня и мотивация.",
            "Cognitive and social readiness, daily routine and motivation.",
        ),
        "outcomes": [
            ("Составление режима дня для ребёнка", "Setting a daily routine for a child"),
            ("Поддержка интереса к учёбе", "Keeping a child interested in learning"),
            ("Оценка дошкольных навыков", "Assessing pre-school readiness"),
        ],
    },
    "oiladagi-muloqot": {
        "goal": (
            "Общение и разрешение конфликтов в семье с опорой на национальные ценности.",
            "Communication and resolving conflict in the family, grounded in local values.",
        ),
        "outcomes": [
            ("Практика активного слушания", "Practising active listening"),
            ("Мирное разрешение конфликта", "Settling a conflict peacefully"),
            ("Закрепление семейных договорённостей", "Confirming family agreements out loud"),
        ],
    },
    "mentorlik-asoslari": {
        "goal": (
            "Старт в роли ментора: структура встречи, границы и измерение результата.",
            "Starting out as a mentor: structuring a session, boundaries and measuring results.",
        ),
        "outcomes": [
            ("Структурирование менторской встречи", "Structuring a mentoring session"),
            ("Постановка цели для подопечной", "Setting a goal with a mentee"),
            ("Оценка результата", "Assessing the result"),
        ],
    },
    "mahalla-tashabbusi": {
        "goal": (
            "Выявление проблемы, сбор команды и реализация небольшого проекта.",
            "Identifying a problem, gathering people and delivering a small project.",
        ),
        "outcomes": [
            ("Определение потребности махалли", "Identifying what the community needs"),
            ("План небольшого проекта", "Planning a small project"),
            ("Представление результата сообществу", "Presenting the result to the community"),
        ],
    },
}

# (source, type, title uz/ru/en, description uz/ru/en, org, region, skills, reward, days)
OPPORTUNITIES = [
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        ("Buxgalter (kichik biznes)", "Бухгалтер (малый бизнес)", "Accountant (small business)"),
        (
            "Toshkent shahridagi savdo kompaniyasiga buxgalter kerak. Moslashuvchan jadval.",
            "Торговой компании в Ташкенте нужен бухгалтер. Гибкий график.",
            "A trading company in Tashkent needs an accountant. Flexible schedule.",
        ),
        "Alfa Savdo MChJ",
        "tashkent_city",
        ["1c", "excel", "buxgalteriya"],
        {"salary_from": 6000000, "salary_to": 9000000, "currency": "UZS"},
        21,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        ("SMM mutaxassis (masofaviy)", "SMM-специалист (удалённо)", "SMM specialist (remote)"),
        (
            "Onlayn doʻkon uchun ijtimoiy tarmoqlarni yuritish. Toʻliq masofaviy.",
            "Ведение соцсетей для интернет-магазина. Полностью удалённо.",
            "Running social media for an online shop. Fully remote.",
        ),
        "Bozor Online",
        "samarkand",
        ["smm", "canva", "kontent marketing"],
        {"salary_from": 4500000, "salary_to": 7000000, "currency": "UZS"},
        14,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.INTERNSHIP,
        ("Marketing boʻyicha stajirovka", "Стажировка по маркетингу", "Marketing internship"),
        (
            "3 oylik toʻlanadigan stajirovka, keyin doimiy ishga oʻtish imkoniyati.",
            "Оплачиваемая стажировка на 3 месяца с возможностью перейти в штат.",
            "A paid three-month internship with the option of a permanent role.",
        ),
        "Milliy Bank",
        "tashkent_city",
        ["marketing", "excel"],
        {"stipend": 3000000, "currency": "UZS"},
        30,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        ("Tikuvchi-usta", "Швея-мастер", "Master seamstress"),
        (
            "Tikuvchilik sexiga tajribali usta kerak. Ish joyi Fargʻona shahrida.",
            "Швейному цеху нужна опытная мастерица. Место работы — Фергана.",
            "A sewing workshop needs an experienced master. Based in Fergana.",
        ),
        "Hunarmand Tekstil",
        "fergana",
        ["tikuvchilik", "overlok"],
        {"salary_from": 5000000, "salary_to": 8000000, "currency": "UZS"},
        28,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.GRANT,
        (
            "Ayol tadbirkorlar uchun grant",
            "Грант для женщин-предпринимателей",
            "Grant for women entrepreneurs",
        ),
        (
            "Kichik biznesni boshlash uchun 50 mln soʻmgacha qaytarilmaydigan grant.",
            "Безвозвратный грант до 50 млн сумов на запуск малого бизнеса.",
            "A non-repayable grant of up to 50m UZS to start a small business.",
        ),
        "Invest HUB",
        None,
        ["biznes-reja"],
        {"amount": 50000000, "currency": "UZS", "repayable": False},
        45,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.INVESTMENT,
        (
            "Mikromoliyalashtirish liniyasi",
            "Линия микрофинансирования",
            "Microfinance line",
        ),
        (
            "Hunarmandchilik va uy mehnati uchun imtiyozli mikrokredit.",
            "Льготный микрокредит для ремесла и надомного труда.",
            "A subsidised microloan for crafts and home-based work.",
        ),
        "Invest HUB",
        None,
        [],
        {"amount": 20000000, "currency": "UZS", "rate_percent": 4},
        60,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.MENTORSHIP,
        (
            "Biznes-akselerator, 3-oqim",
            "Бизнес-акселератор, 3-й поток",
            "Business accelerator, cohort 3",
        ),
        (
            "8 haftalik akseleratsiya: mentor, moliyaviy model va investor demo-kuni.",
            "8 недель акселерации: ментор, финансовая модель и демо-день для инвесторов.",
            "Eight weeks of acceleration: a mentor, a financial model and an investor demo day.",
        ),
        "Invest HUB",
        None,
        ["biznes-reja", "moliyaviy model"],
        {"sessions": 16, "price": 0},
        35,
    ),
    (
        OpportunitySource.COMMERCE,
        OpportunityType.MARKETPLACE,
        (
            "Marketpleysda sotuvchi profili",
            "Профиль продавца на маркетплейсе",
            "Marketplace seller profile",
        ),
        (
            "Hunarmandchilik mahsulotlari uchun bepul vitrina va birinchi oy komissiyasiz.",
            "Бесплатная витрина для изделий ручной работы, первый месяц без комиссии.",
            "A free storefront for handmade goods, with no commission in the first month.",
        ),
        "Tijorat markazi",
        None,
        ["hunarmandchilik"],
        {"commission_percent": 0, "first_month_free": True},
        90,
    ),
    (
        OpportunitySource.COMMERCE,
        OpportunityType.MARKETPLACE,
        (
            "B2B buyurtma: maktab formasi",
            "B2B-заказ: школьная форма",
            "B2B order: school uniforms",
        ),
        (
            "Viloyat maktablari uchun forma tikish boʻyicha kooperatsiya buyurtmasi.",
            "Кооперационный заказ на пошив формы для школ области.",
            "A cooperative order to sew uniforms for schools across the region.",
        ),
        "Tijorat markazi",
        "jizzakh",
        ["tikuvchilik", "ishlab chiqarish"],
        {"volume": 4000, "currency": "UZS"},
        25,
    ),
    (
        OpportunitySource.INTERNAL,
        OpportunityType.INTERNATIONAL_PROGRAM,
        (
            "Xalqaro almashinuv dasturi",
            "Программа международного обмена",
            "International exchange programme",
        ),
        (
            "Ayol liderlar uchun 2 haftalik xalqaro almashinuv.",
            "Двухнедельный международный обмен для женщин-лидеров.",
            "A two-week international exchange for women leaders.",
        ),
        "WomanUP International",
        None,
        ["ingliz tili"],
        {"covered": ["travel", "accommodation"]},
        50,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        ("Kassir-operator", "Кассир-оператор", "Cashier"),
        (
            "Namangandagi supermarket tarmogʻiga kassir kerak. Smenali ish, taʼlim beriladi.",
            "Сети супермаркетов в Намангане нужен кассир. Сменный график, обучение на месте.",
            "A supermarket chain in Namangan needs a cashier. Shift work, training provided.",
        ),
        "Hilol Market",
        "namangan",
        ["kassa", "mijozlar bilan ishlash"],
        {"salary_from": 3500000, "salary_to": 4500000, "currency": "UZS"},
        18,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        ("Tikuvchi-operator", "Швея-оператор", "Sewing machine operator"),
        (
            "Andijondagi tikuv fabrikasiga tajribali tikuvchilar kerak. Transport taʼminlanadi.",
            "Швейной фабрике в Андижане нужны опытные швеи. Транспорт предоставляется.",
            "A garment factory in Andijan needs experienced seamstresses. Transport provided.",
        ),
        "Andijon Teks",
        "andijan",
        ["tikuvchilik", "overlok"],
        {"salary_from": 4000000, "salary_to": 6500000, "currency": "UZS"},
        30,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        (
            "Ingliz tili oʻqituvchisi (yarim stavka)",
            "Преподаватель английского (полставки)",
            "English teacher (part-time)",
        ),
        (
            "Buxorodagi oʻquv markaziga A2-B1 daraja uchun oʻqituvchi kerak.",
            "Учебному центру в Бухаре нужен преподаватель для уровня A2-B1.",
            "A study centre in Bukhara needs a teacher for A2-B1 levels.",
        ),
        "Bukhara Study",
        "bukhara",
        ["ingliz tili", "pedagogika"],
        {"salary_from": 3000000, "salary_to": 5000000, "currency": "UZS"},
        25,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.VACANCY,
        (
            "Call-markaz operatori (masofaviy)",
            "Оператор call-центра (удалённо)",
            "Call centre operator (remote)",
        ),
        (
            "Toʻliq masofaviy ish, moslashuvchan smena. Kompyuter va internet talab qilinadi.",
            "Полностью удалённая работа, гибкая смена. Нужны компьютер и интернет.",
            "Fully remote work on a flexible shift. A computer and internet are required.",
        ),
        "Aloqa Servis",
        "karakalpakstan",
        ["mijozlar bilan ishlash", "kompyuter savodxonligi"],
        {"salary_from": 3200000, "salary_to": 4800000, "currency": "UZS"},
        20,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.INTERNSHIP,
        (
            "Buxgalteriya boʻyicha amaliyot",
            "Практика по бухгалтерии",
            "Bookkeeping placement",
        ),
        (
            "Uch oylik amaliyot, eng yaxshi ishtirokchilarga doimiy ish taklif qilinadi.",
            "Трёхмесячная практика; лучшим участницам предложат постоянную работу.",
            "A three-month placement; the strongest participants are offered a permanent role.",
        ),
        "Samarqand Audit",
        "samarkand",
        ["buxgalteriya", "excel"],
        {"stipend": 2000000, "currency": "UZS"},
        35,
    ),
    (
        OpportunitySource.EDU_JOB,
        OpportunityType.INTERNSHIP,
        (
            "IT-loyihalarda yordamchi amaliyotchi",
            "Помощник в IT-проектах, практика",
            "Assistant intern on IT projects",
        ),
        (
            "Raqamli loyihalarda hujjat yuritish va testlashda amaliyot.",
            "Практика в цифровых проектах: ведение документации и тестирование.",
            "A placement on digital projects: documentation and testing.",
        ),
        "Digital Qashqadaryo",
        "kashkadarya",
        ["kompyuter savodxonligi", "hujjat yuritish"],
        {"stipend": 1800000, "currency": "UZS"},
        28,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.GRANT,
        (
            "Ayol tadbirkorlar uchun boshlangʻich grant",
            "Стартовый грант для женщин-предпринимателей",
            "Start-up grant for women entrepreneurs",
        ),
        (
            "Yangi boshlagan ayol tadbirkorlarga qaytarilmas moliyaviy yordam.",
            "Безвозвратная финансовая помощь начинающим предпринимательницам.",
            "Non-repayable funding for women starting out in business.",
        ),
        "Invest HUB",
        "khorezm",
        ["biznes-reja"],
        {"amount": 30000000, "currency": "UZS", "repayable": False},
        45,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.GRANT,
        (
            "Qishloq xoʻjaligi kooperatsiyasi granti",
            "Грант на сельскохозяйственную кооперацию",
            "Agricultural cooperative grant",
        ),
        (
            "Qishloq joylarda ayollar kooperativlarini qoʻllab-quvvatlash dasturi.",
            "Программа поддержки женских кооперативов в сельской местности.",
            "A programme supporting women's cooperatives in rural areas.",
        ),
        "Invest HUB",
        "surkhandarya",
        ["qishloq xoʻjaligi", "kooperatsiya"],
        {"amount": 50000000, "currency": "UZS", "repayable": False},
        60,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.INVESTMENT,
        (
            "Ishlab chiqarishni kengaytirish uchun investitsiya",
            "Инвестиция на расширение производства",
            "Investment to expand production",
        ),
        (
            "Ishlayotgan kichik ishlab chiqarish uchun ulushli investitsiya.",
            "Долевая инвестиция в действующее малое производство.",
            "An equity investment in an operating small production line.",
        ),
        "Invest HUB",
        "tashkent_region",
        ["ishlab chiqarish", "moliyaviy model"],
        {"amount": 150000000, "currency": "UZS", "equity_percent": 20},
        50,
    ),
    (
        OpportunitySource.INVEST_HUB,
        OpportunityType.MENTORSHIP,
        (
            "Tadbirkorlik boʻyicha 3 oylik mentorlik",
            "Трёхмесячное менторство по предпринимательству",
            "Three months of entrepreneurship mentoring",
        ),
        (
            "Tajribali tadbirkor bilan oyiga ikki marta individual uchrashuv.",
            "Индивидуальные встречи с опытным предпринимателем дважды в месяц.",
            "One-to-one meetings with an experienced entrepreneur twice a month.",
        ),
        "Invest HUB",
        "navoi",
        ["tadbirkorlik"],
        {"sessions": 6, "price": 0},
        40,
    ),
    (
        OpportunitySource.COMMERCE,
        OpportunityType.MARKETPLACE,
        (
            "Milliy hunarmandchilik uchun savdo maydoni",
            "Торговая площадка для народных ремёсел",
            "A marketplace for traditional crafts",
        ),
        (
            "Qoʻlda tayyorlangan mahsulotlar uchun onlayn vitrina va yetkazib berish.",
            "Онлайн-витрина и доставка для изделий ручной работы.",
            "An online storefront and delivery for handmade goods.",
        ),
        "Tijorat Markazi",
        "jizzakh",
        ["hunarmandchilik", "qadoqlash"],
        {"commission_percent": 8},
        90,
    ),
    (
        OpportunitySource.COMMERCE,
        OpportunityType.MARKETPLACE,
        (
            "Uy sharoitida tayyorlangan oziq-ovqat uchun javon",
            "Полка для домашней пищевой продукции",
            "Shelf space for home-produced food",
        ),
        (
            "Sertifikatlangan uy mahsulotlarini shahar doʻkonlarida sotish imkoniyati.",
            "Возможность продавать сертифицированную домашнюю продукцию в городских магазинах.",
            "A route to sell certified home-produced food in city shops.",
        ),
        "Tijorat Markazi",
        "sirdarya",
        ["oziq-ovqat", "sertifikat"],
        {"commission_percent": 12},
        75,
    ),
    (
        OpportunitySource.INTERNAL,
        OpportunityType.INTERNATIONAL_PROGRAM,
        (
            "Xalqaro almashinuv: ayol yetakchilar maktabi",
            "Международный обмен: школа женского лидерства",
            "International exchange: school for women leaders",
        ),
        (
            "Ikki haftalik xalqaro dastur. Ingliz tili B1 va motivatsion xat talab qilinadi.",
            "Двухнедельная международная программа. Нужны английский B1 и мотивационное письмо.",
            "A two-week international programme. Requires English at B1 and a motivation letter.",
        ),
        "WomanUP International",
        None,
        ["ingliz tili", "liderlik"],
        {"covered": ["travel", "accommodation"]},
        55,
    ),
    (
        OpportunitySource.INTERNAL,
        OpportunityType.MENTORSHIP,
        (
            "WomanUP mentorlik dasturi (kuzgi oqim)",
            "Программа менторства WomanUP (осенний поток)",
            "WomanUP mentoring programme (autumn cohort)",
        ),
        (
            "Portal mentorlari bilan uch oylik hamrohlik. Barcha viloyatlar uchun ochiq.",
            "Три месяца сопровождения с менторами портала. Открыто для всех регионов.",
            "Three months alongside a portal mentor. Open to every region.",
        ),
        "WomanUP Academy",
        None,
        [],
        {"sessions": 8, "price": 0},
        32,
    ),
]


MENTORS = [
    (
        "Moliya va buxgalteriya boʻyicha 14 yillik tajriba",
        "Kichik biznes uchun hisob yuritish va soliqlarni soddalashtirishga yordam beraman.",
        ["buxgalteriya", "soliq", "moliyaviy model"],
        ["uz", "ru"],
        14,
        4.8,
    ),
    (
        "Tikuvchilik sexi asoschisi",
        "Uy sharoitidagi tikuvchilikdan 20 kishilik sexgacha boʻlgan yoʻlni birga oʻtamiz.",
        ["tikuvchilik", "ishlab chiqarish", "narx belgilash"],
        ["uz"],
        11,
        4.9,
    ),
    (
        "Raqamli marketing yetakchisi",
        "Ijtimoiy tarmoqlarda birinchi mijozgacha boʻlgan yoʻlni tuzamiz.",
        ["smm", "kontent marketing", "e-commerce"],
        ["uz", "ru", "en"],
        8,
        4.7,
    ),
    (
        "Yuridik maslahatchi",
        "Mehnat va oila huquqi boʻyicha amaliy tushuntirish beraman.",
        ["mehnat huquqi", "shartnoma", "oila huquqi"],
        ["uz", "ru"],
        16,
        4.6,
    ),
    (
        "Xalqaro grantlar boʻyicha ekspert",
        "Grant arizasi va motivatsion xatni birga tayyorlaymiz.",
        ["grant", "ingliz tili", "loyiha boshqaruvi"],
        ["uz", "en"],
        9,
        4.9,
    ),
    (
        "Kadrlar boʻlimi boshligʻi, 12 yil tajriba",
        "Rezyume, ish suhbati va birinchi ish oyini oʻtishda amaliy yordam beraman.",
        ["rezyume", "ish suhbati", "kadrlar"],
        ["uz", "ru"],
        12,
        4.7,
    ),
    (
        "Onlayn savdo boʻyicha amaliyotchi",
        "Marketpleysda birinchi savdodan barqaror oqimgacha boʻlgan yoʻlni koʻrsataman.",
        ["marketpleys", "logistika", "mahsulot kartochkasi"],
        ["uz"],
        7,
        4.6,
    ),
    (
        "Yuridik maslahatchi",
        "Mehnat shartnomasi, taʼtil va dekret huquqlari boʻyicha tushuntiraman.",
        ["mehnat huquqi", "shartnoma"],
        ["uz", "ru"],
        9,
        4.9,
    ),
    (
        "Ingliz tili oʻqituvchisi va IELTS mentori",
        "Nol darajadan B1 gacha reja tuzamiz va xalqaro dasturlarga tayyorlanamiz.",
        ["ingliz tili", "ielts"],
        ["uz", "ru", "en"],
        10,
        4.8,
    ),
    (
        "Psixolog, oilaviy maslahatchi",
        "Charchoq, chegara qoʻyish va oiladagi muloqot boʻyicha ishlaymiz.",
        ["psixologiya", "chegaralar", "muloqot"],
        ["uz", "ru"],
        13,
        4.9,
    ),
    (
        "Qishloq xoʻjaligi kooperativi rahbari",
        "Yerdan daromadgacha: kooperatsiya, sertifikat va sotuv kanallari.",
        ["qishloq xoʻjaligi", "kooperatsiya"],
        ["uz"],
        16,
        4.5,
    ),
    (
        "IT-loyihalar menejeri",
        "Raqamli kasbga oʻtishni rejalashtiramiz va birinchi portfolio yigʻamiz.",
        ["it", "loyiha boshqaruvi", "portfolio"],
        ["uz", "ru", "en"],
        8,
        4.7,
    ),
]


# Russian and English editions of the knowledge base, keyed by the Uzbek title.
# The navigator retrieves by document language, so without a native edition per
# locale a Russian- or English-speaking user matches nothing at all — keyword
# search cannot bridge languages, and translating the query at request time
# would put a model call in front of the safety filter.
KNOWLEDGE_TRANSLATIONS: dict[str, dict[str, tuple[str, str]]] = {
    "WomanUP portali nima va u qanday ishlaydi": {
        "ru": (
            "Что такое портал WomanUP и как он работает",
            """
WomanUP — национальная цифровая платформа развития для женщин и девушек. Портал
регистрирует вас, определяет ваши потребности, составляет индивидуальный план
развития и связывает с реальными возможностями.

Путь на портале состоит из пяти шагов. Сначала вы регистрируетесь по номеру
телефона и получаете WomanUP ID. Затем проходите диагностику на 10-15 минут.
После этого система показывает ваш Development Score и предлагает индивидуальный
план. Дальше вы записываетесь на программы и начинаете учиться. И наконец, вам
рекомендуют подходящие возможности — вакансию, грант или торговую площадку.

Пользование порталом бесплатное. Основные услуги открыты всем пользователям.
""",
        ),
        "en": (
            "What the WomanUP portal is and how it works",
            """
WomanUP is a national digital development platform for women and girls. The
portal registers you, works out what you need, builds an individual development
plan and connects you to real opportunities.

The journey has five steps. First you register with your phone number and
receive a WomanUP ID. Second, you take a 10-15 minute diagnostic. Third, the
system shows your Development Score and proposes an individual plan. Fourth, you
enrol on programmes and start studying. Fifth, matching opportunities — a
vacancy, a grant or a marketplace slot — are recommended to you.

Using the portal is free. The core services are open to every user.
""",
        ),
    },
    "Development Score qanday hisoblanadi": {
        "ru": (
            "Как рассчитывается Development Score",
            """
WomanUP Development Score — это составной показатель от 0 до 100. Он считается по
восьми измерениям: образование и навыки, занятость, предпринимательство,
финансовая грамотность, здоровый образ жизни, семья и воспитание, социальная
активность, международная интеграция.

Каждое измерение оценивается отдельно, затем значения сводятся во взвешенное
среднее. Первый результат сохраняется как базовый, и все последующие
сравниваются именно с ним.

Балл — не оценка и не приговор. Он нужен, чтобы найти направление, где поддержка
нужнее всего, и построить план именно от него. Низкий балл в каком-то измерении
означает лишь, что там больше пространства для роста.
""",
        ),
        "en": (
            "How the Development Score is calculated",
            """
The WomanUP Development Score is a composite figure from 0 to 100. It is
calculated across eight dimensions: education and skills, employment,
entrepreneurship, financial literacy, healthy lifestyle, family and parenting,
social activity, and international integration.

Each dimension is scored separately, then combined into a weighted average. Your
first result is stored as the baseline, and every later result is compared
against it.

The score is not a grade or a verdict. It exists to find where support is most
needed and to build your plan from there. A low score in one dimension only
means there is more room to grow in it.
""",
        ),
    },
    "Shaxsiy maʼlumotlaringiz qanday himoyalanadi": {
        "ru": (
            "Как защищены ваши персональные данные",
            """
Ваши данные хранятся на портале и не передаются третьим лицам без вашего
согласия. Каждое согласие фиксируется отдельно и может быть отозвано в любой
момент.

Партнёрам передаётся минимальный набор: псевдонимный идентификатор, регион,
профессиональные навыки и уровень образования. Номер телефона, полное имя,
семейное положение и число детей не передаются никогда.

Чувствительные поля — здоровье, семья — имеют отдельную область доступа. Каждое
обращение к ним записывается в журнал аудита.
""",
        ),
        "en": (
            "How your personal data is protected",
            """
Your data is held on the portal and is not passed to third parties without your
consent. Each consent is recorded separately and can be withdrawn at any time.

Partners receive a minimal set: a pseudonymous identifier, your region, your
professional skills and your education level. Your phone number, full name,
marital status and number of children are never sent.

Sensitive fields — health, family — sit behind a separate access scope. Every
access to them is written to the audit log.
""",
        ),
    },
    "Kursga qanday yozilaman va sertifikat olamanmi": {
        "ru": (
            "Как записаться на курс и получу ли я сертификат",
            """
Выберите курс в каталоге программ и нажмите «Записаться». Курс появится в разделе
«Мои курсы» вашего личного кабинета, и вы сможете продолжить в любое время.

Курс состоит из модулей. Прогресс сохраняется автоматически. Когда прогресс
достигает 100 процентов и сдан итоговый тест, выдаётся сертификат — его можно
скачать из кабинета.

Сертификат предусмотрен не во всех курсах: это указано в карточке курса. Курсы
без сертификата всё равно учитываются в вашем плане развития.
""",
        ),
        "en": (
            "How to enrol on a course and whether you get a certificate",
            """
Pick a course from the programme catalogue and press "Enrol". The course appears
under "My courses" in your personal cabinet, and you can pick it up whenever you
like.

A course is made of modules. Progress saves automatically. When progress reaches
100 per cent and the final test is passed, a certificate is issued — you can
download it from your cabinet.

Not every course carries a certificate; the course card says so explicitly.
Courses without one still count towards your development plan.
""",
        ),
    },
    "Ishga joylashish uchun nima qilishim kerak": {
        "ru": (
            "Что нужно сделать, чтобы найти работу",
            """
Сначала заполните профиль: образование, профессия, навыки и регион. Чем полнее
профиль, тем точнее подбор вакансий.

Затем дайте согласие на передачу данных в Edu-Job. Без согласия вакансии
показываются, но подать заявку нельзя — это защитная мера.

Если навыков не хватает, система покажет, каких именно, и предложит курс,
который их закрывает. После курса ваш профиль обновляется, и подбор становится
точнее.

Отклик на вакансию отслеживается в кабинете: отправлена, на рассмотрении,
принята или отклонена.
""",
        ),
        "en": (
            "What to do to find work",
            """
Start by completing your profile: education, profession, skills and region. The
fuller the profile, the better the vacancy matching.

Then give consent to share your data with Edu-Job. Without it you can still see
vacancies but cannot apply — that is a safeguard, not an obstacle.

If you are short of a skill, the system names which one and suggests the course
that closes the gap. After the course your profile updates and the matching
improves.

Every application is tracked in your cabinet: submitted, under review, accepted
or rejected.
""",
        ),
    },
    "Biznes ochish uchun qanday yordam bor": {
        "ru": (
            "Какая поддержка есть для открытия бизнеса",
            """
На портале три вида поддержки предпринимательства: обучение, менторство и
финансирование.

Обучение — курсы по основам предпринимательства, финансовой модели и продажам. С
них стоит начать, если бизнеса ещё нет.

Менторство — встречи с предпринимательницей, которая уже прошла этот путь.
Ментор не принимает решения за вас, а показывает дорогу.

Финансирование идёт через Invest HUB: безвозвратные гранты, льготные микрокредиты
и долевые инвестиции. Для любой заявки нужны бизнес-план и согласие на передачу
данных.
""",
        ),
        "en": (
            "What support there is for starting a business",
            """
The portal offers three kinds of support for entrepreneurship: training,
mentoring and funding.

Training covers the basics of business, the financial model and selling. Start
here if the business does not exist yet.

Mentoring means meeting a woman who has already walked this road. A mentor does
not decide for you; she shows you the way.

Funding runs through Invest HUB: non-repayable grants, subsidised microloans and
equity investment. Every application needs a business plan and your consent to
share data.
""",
        ),
    },
    "Dasturga qanday yozilish va sertifikat olish mumkin": {
        "ru": (
            "Как записаться на программу и получить сертификат",
            """
Чтобы записаться, выберите подходящую программу в каталоге и нажмите
«Записаться». После этого программа появится в разделе «Мои программы» вашего
кабинета, и вы сможете вернуться к ней в любой момент.

Каждая программа состоит из модулей. Когда вы завершаете модуль, прогресс
сохраняется автоматически. Когда программа доходит до 100 процентов и сдан
итоговый тест, выдаётся сертификат. Сертификат скачивается из кабинета.

Сертификат есть не у всех программ — это отдельно отмечено в карточке программы.
Программы без сертификата тоже учитываются в вашем плане.
""",
        ),
        "en": (
            "How to enrol on a programme and get a certificate",
            """
To enrol, choose a programme from the catalogue and press "Enrol". The programme
then appears under "My programmes" in your cabinet, and you can return to it
whenever you like.

Every programme is made of modules. Finishing a module saves your progress
automatically. When the programme reaches 100 per cent and the final test is
passed, a certificate is issued. You download it from your cabinet.

Not every programme carries a certificate — the programme card states this
separately. Programmes without one still count towards your plan.
""",
        ),
    },
    "Individual rivojlanish rejasi qanday tuziladi": {
        "ru": (
            "Как составляется индивидуальный план развития",
            """
План строится по результатам диагностики. Система определяет измерения с самым
низким баллом и предлагает конкретные шаги именно по этим направлениям. Каждый
шаг может быть привязан к программе или возможности.

Важное правило: план не вступает в силу автоматически. Система только предлагает
— решение за вами. Вы просматриваете план и подтверждаете его кнопкой
«Принять». Неподтверждённый план не считается активным.

План можно пересоздать в любой момент — например, после новой диагностики или
когда изменилась цель. Старый план сохраняется в истории.
""",
        ),
        "en": (
            "How your individual development plan is built",
            """
The plan is built from your diagnostic results. The system identifies the
lowest-scoring dimensions and proposes concrete steps in exactly those
directions. Each step may be tied to a programme or an opportunity.

One rule matters most: the plan does not take effect on its own. The system only
proposes — the decision is yours. You review the plan and confirm it with the
"Accept" button. An unconfirmed plan is not treated as active.

You can regenerate the plan at any time — after a new diagnostic, say, or when
your goal changes. The old plan is kept in your history.
""",
        ),
    },
    "Mening maʼlumotlarim kimga va qachon uzatiladi": {
        "ru": (
            "Кому и когда передаются мои данные",
            """
Ваши данные никогда не покидают портал без вашего согласия. Для каждой
партнёрской системы — Edu-Job, Invest HUB, Тижорат Маркази — согласие
запрашивается отдельно.

Даже когда согласие дано, партнёру передаётся только минимум: псевдонимный
идентификатор, регион, профессиональные навыки и уровень образования. Номер
телефона, полное имя, семейное положение и число детей не передаются никогда.

Согласие можно отозвать в любой момент — в разделе «Согласия» вашего кабинета.
После отзыва новые передачи прекращаются.
""",
        ),
        "en": (
            "Who receives my data, and when",
            """
Your data never leaves the portal without your consent. Consent is asked
separately for each partner system — Edu-Job, Invest HUB, Tijorat Markazi.

Even once consent is given, the partner receives only the minimum: a pseudonymous
identifier, your region, your professional skills and your education level. Your
phone number, full name, marital status and number of children are never sent.

You can withdraw consent at any time, in the "Consents" section of your cabinet.
After withdrawal, no further transfers are made.
""",
        ),
    },
    "Imkoniyatga ariza berish tartibi": {
        "ru": (
            "Как подать заявку на возможность",
            """
В разделе возможностей вам рекомендуются подходящие вакансии, гранты и торговые
площадки. Соответствие считается по вашим навыкам, региону и плану развития.

Чтобы подать заявку, сначала нужно дать согласие на передачу данных
соответствующей партнёрской системе. Без согласия система заявку не примет — это
защитная мера.

Статус заявки виден в кабинете: отправлена, на рассмотрении, принята или
отклонена. Когда приходит ответ от партнёра, вам приходит уведомление.
""",
        ),
        "en": (
            "How to apply for an opportunity",
            """
The opportunities section recommends vacancies, grants and marketplace slots that
suit you. The match is calculated from your skills, your region and your
development plan.

To apply you must first consent to sharing your data with that partner system.
Without consent the system will not accept the application — that is a
safeguard.

The status is visible in your cabinet: submitted, under review, accepted or
rejected. When the partner responds, you receive a notification.
""",
        ),
    },
    "Mentor bilan qanday ishlash kerak": {
        "ru": (
            "Как работать с ментором",
            """
Ментор — это практик, который делится своим опытом. Ментор не принимает решения
за вас и не находит вам работу; он показывает путь.

Подготовьтесь к первой встрече: запишите, на какой вопрос хотите получить ответ
и какого результата ждёте. Встреча обычно длится 45-60 минут.

Менторство бесплатное и добровольное. Если не подошло — можно выбрать другого
ментора, это нормально.
""",
        ),
        "en": (
            "How to work with a mentor",
            """
A mentor is a practitioner who shares her own experience. A mentor does not make
decisions for you and does not find you a job; she shows you the way.

Prepare for the first meeting: write down the question you want answered and the
result you are hoping for. A session usually runs 45-60 minutes.

Mentoring is free and voluntary. If the fit is wrong you can choose a different
mentor — that is normal.
""",
        ),
    },
    "Mehnat huquqlari: dekret, taʼtil va shartnoma": {
        "ru": (
            "Трудовые права: декрет, отпуск и договор",
            """
Трудовой договор должен быть заключён в письменной форме. В нём указываются
должность, оплата, рабочее время и продолжительность отпуска. Устная
договорённость вас не защищает.

Ежегодный оплачиваемый отпуск составляет не менее 15 рабочих дней. Отпуск по
беременности и родам установлен законом отдельно, и отменить его нельзя.

Работодатель не вправе отказать в приёме на работу или расторгнуть договор по
причине беременности. Если ваши права нарушены, можно обратиться в трудовую
инспекцию.

Это общая информация. По конкретной ситуации проконсультируйтесь с юристом.
""",
        ),
        "en": (
            "Employment rights: maternity leave, holiday and contracts",
            """
An employment contract must be in writing. It states the role, the pay, the
working hours and the length of holiday. A verbal agreement does not protect
you.

Paid annual leave is at least 15 working days. Maternity leave is set separately
in law and cannot be taken away.

An employer may not refuse to hire you or terminate your contract because of
pregnancy. If your rights are breached, you can go to the labour inspectorate.

This is general information. For your specific situation, consult a lawyer.
""",
        ),
    },
    "Raqamli xavfsizlik: firibgarlikdan himoya": {
        "ru": (
            "Цифровая безопасность: защита от мошенничества",
            """
Никому не сообщайте код из SMS. Ни банк, ни портал, ни государственное
учреждение никогда не спрашивают код. Любой, кто его просит, — мошенник.

Если за предложение работы просят деньги вперёд — это мошенничество. Настоящий
работодатель не требует оплаты от кандидата.

Делайте отдельный пароль для каждого сервиса и по возможности включайте
двухфакторное подтверждение. Не открывайте подозрительные ссылки.
""",
        ),
        "en": (
            "Digital safety: protecting yourself from fraud",
            """
Never give anyone the code from an SMS. No bank, portal or government office
will ever ask for it. Anyone who does is a fraudster.

If money is asked for up front in exchange for a job offer, it is fraud. A real
employer does not charge a candidate.

Use a separate password for each service and turn on two-factor confirmation
where you can. Do not open suspicious links.
""",
        ),
    },
    "Diagnostikadan qayta oʻtish va natijani yaxshilash": {
        "ru": (
            "Повторная диагностика и улучшение результата",
            """
Диагностику можно пройти повторно, и это рекомендуется — обычно раз в три
месяца. Первый результат сохраняется как базовый, последующие сравниваются с
ним.

Не пытайтесь искусственно завысить балл. Диагностика нужна не для оценки вас, а
чтобы найти подходящее направление. Честный ответ даёт более точный план.

Чтобы балл рос, выполняйте шаги плана: завершайте программы, добавляйте навыки,
подавайте заявки на возможности. Каждый выполненный шаг отражается в
соответствующем измерении.
""",
        ),
        "en": (
            "Retaking the diagnostic and improving your result",
            """
You can retake the diagnostic, and it is recommended — usually once every three
months. Your first result is kept as the baseline and later results are compared
against it.

Do not try to inflate the score. The diagnostic exists to find the right
direction for you, not to grade you. An honest answer produces a better plan.

To move the score, work through your plan: finish programmes, add skills, apply
for opportunities. Every completed step shows up in the dimension it belongs to.
""",
        ),
    },
}

KNOWLEDGE = [
    (
        "WomanUP portali nima va u qanday ishlaydi",
        "faq",
        """
WomanUP — ayol-qizlar uchun milliy raqamli rivojlanish platformasi. Portal sizni
roʻyxatdan oʻtkazadi, ehtiyojlaringizni aniqlaydi, individual rivojlanish rejasini
tuzadi va sizni real imkoniyatlarga ulaydi.

Portaldagi yoʻl beshta bosqichdan iborat. Birinchidan, siz telefon raqami orqali
roʻyxatdan oʻtasiz va WomanUP ID olasiz. Ikkinchidan, 10-15 daqiqalik diagnostikadan
oʻtasiz. Uchinchidan, tizim sizga Development Score va individual reja taklif qiladi.
Toʻrtinchidan, siz dasturlarga yozilasiz va oʻqishni boshlaysiz. Beshinchidan, mos
imkoniyatlar - vakansiya, grant yoki savdo - sizga tavsiya qilinadi.

Portaldan foydalanish bepul. Asosiy xizmatlar barcha foydalanuvchilar uchun ochiq.
""",
    ),
    (
        "Development Score qanday hisoblanadi",
        "guideline",
        """
WomanUP Development Score - bu 0 dan 100 gacha boʻlgan kompozit koʻrsatkich. U
sakkizta oʻlchov boʻyicha hisoblanadi: taʼlim va koʻnikma, bandlik, tadbirkorlik,
moliyaviy savodxonlik, sogʻlom turmush, oila va farzand tarbiyasi, ijtimoiy faollik,
xalqaro integratsiya.

Har bir oʻlchov uchun uchta qiymat saqlanadi: baseline - birinchi diagnostikadagi
natijangiz, current - hozirgi natijangiz, target - maqsadli qiymat. Progress
mutlaq idealga emas, aynan sizning boshlangʻich nuqtangizga nisbatan oʻlchanadi.

Ball past boʻlishi muammo emas. U shunchaki qayerdan boshlashni koʻrsatadi.
Diagnostikani istalgan vaqtda qayta topshirishingiz mumkin.
""",
    ),
    (
        "Shaxsiy maʼlumotlaringiz qanday himoyalanadi",
        "legal",
        """
WomanUP shaxsiy maʼlumotlarni minimal hajmda yigʻadi va faqat sizning roziligingiz
asosida ishlatadi.

Hamkor platformalarga - Edu-Job, Invest HUB va Tijorat markaziga - maʼlumot
yuborilishidan oldin sizdan alohida rozilik soʻraladi. Rozilik bermasangiz, hech
qanday maʼlumot yuborilmaydi. Roziligingizni istalgan vaqtda qaytarib olishingiz
mumkin.

Hamkorlarga telefon raqamingiz, elektron pochtangiz, familiya-ismingiz, oilaviy
holatingiz va farzandlaringiz soni yuborilmaydi. Faqat psevdonim identifikator va
kasbiy maʼlumotlar - taʼlim, kasb, koʻnikmalar - almashiladi.

Har bir maʼlumot almashish hodisasi audit jurnaliga yoziladi. Hisobingizni
oʻchirishingiz mumkin: identifikatorlar tozalanadi, faoliyat tarixi anonim qoladi.
""",
    ),
    (
        "Kursga qanday yozilaman va sertifikat olamanmi",
        "faq",
        """
Dasturlar katalogidan kursni tanlang va "Yozilish" tugmasini bosing. Kurs shaxsiy
kabinetingizdagi "Mening kurslarim" boʻlimida paydo boʻladi.

Har bir kurs modullardan iborat. Modulni tugatganingizda progress avtomatik
yangilanadi. Barcha modullar tugaganda kurs "yakunlangan" holatiga oʻtadi.

Agar kursda sertifikat koʻzda tutilgan boʻlsa, u yakunlanganda avtomatik beriladi.
Har bir sertifikatda noyob raqam va tekshirish kodi boʻladi - ish beruvchi bu kod
orqali sertifikatning haqiqiyligini hisobsiz ham tekshira oladi.
""",
    ),
    (
        "Ishga joylashish uchun nima qilishim kerak",
        "guideline",
        """
Portal taʼlimdan ishga oʻtish mexanizmini Edu-Job platformasi bilan integratsiya
orqali taʼminlaydi.

Avval profilingizni toʻldiring va koʻnikmalaringizni kiriting - tavsiyalar aynan
shu maʼlumotga tayanadi. Keyin "Imkoniyatlar" boʻlimiga oʻting. Har bir vakansiya
uchun tizim sizning koʻnikmalaringiz bilan talab qilinganlarini solishtiradi va
qaysi koʻnikma yetishmayotganini koʻrsatadi.

Yetishmayotgan koʻnikma boʻlsa, tizim uni beradigan kursni tavsiya qiladi. Bu
"skill-gap tahlili" deb ataladi. Kursni tugatib, arizani qayta yuborishingiz mumkin.

Ariza yuborish uchun Edu-Job bilan maʼlumot almashishga rozilik berishingiz kerak.
Ariza holati shaxsiy kabinetingizda kuzatiladi.
""",
    ),
    (
        "Biznes ochish uchun qanday yordam bor",
        "guideline",
        """
Invest HUB integratsiyasi tadbirkorlik yoʻlidagi ayollar uchun moʻljallangan.

Portalda tadbirkorlik yoʻnalishidagi dasturlarni tugatganingizdan soʻng sizga
grant, imtiyozli kredit, akselerator va mentor imkoniyatlari tavsiya qilinadi.

Biznes-reja va moliyaviy modelni tayyorlashda "Tadbirkorlik asoslari" kursi yordam
beradi. Ariza yuborilgandan keyin uning holati kabinetingizda koʻrinadi.

Agar mahsulot yoki xizmat sotmoqchi boʻlsangiz, Tijorat markazi orqali sotuvchi
profilini ochishingiz va raqamli vitrina yaratishingiz mumkin.
""",
    ),
    (
        "Dasturga qanday yozilish va sertifikat olish mumkin",
        "faq",
        """
Dasturga yozilish uchun katalogdan mos dasturni tanlang va "Yozilish" tugmasini
bosing. Yozilgandan keyin dastur sizning kabinetingizda "Mening dasturlarim"
boʻlimida paydo boʻladi va siz istalgan vaqtda davom ettirishingiz mumkin.

Har bir dastur modullardan iborat. Modulni tugatganingizda progress avtomatik
saqlanadi. Dastur 100 foizga yetganda va yakuniy test topshirilganda sertifikat
beriladi. Sertifikat kabinetdan yuklab olinadi.

Barcha dasturlarda sertifikat boʻlavermaydi - dastur kartochkasida bu alohida
koʻrsatilgan. Sertifikatsiz dasturlar ham rejangizda hisobga olinadi.
""",
    ),
    (
        "Individual rivojlanish rejasi qanday tuziladi",
        "guideline",
        """
Reja diagnostika natijalari asosida tuziladi. Tizim eng past ball toʻplangan
oʻlchovlarni aniqlaydi va shu yoʻnalishlar boʻyicha aniq qadamlar taklif qiladi.
Har bir qadam dastur yoki imkoniyat bilan bogʻlanishi mumkin.

Muhim qoida: reja avtomatik kuchga kirmaydi. Tizim faqat taklif qiladi, qaror
sizniki. Rejani koʻrib chiqasiz va "Qabul qilish" tugmasi orqali tasdiqlaysiz.
Tasdiqlanmagan reja faol hisoblanmaydi.

Rejani istalgan vaqtda qayta yaratish mumkin - masalan, yangi diagnostikadan
keyin yoki maqsadingiz oʻzgarganda. Eski reja tarixda saqlanib qoladi.
""",
    ),
    (
        "Mening maʼlumotlarim kimga va qachon uzatiladi",
        "policy",
        """
Sizning maʼlumotlaringiz hech qachon sizning roziligingizsiz portaldan tashqariga
chiqmaydi. Har bir hamkor tizim - Edu-Job, Invest HUB, Tijorat Markazi - uchun
alohida rozilik soʻraladi.

Rozilik berganingizda ham hamkorga faqat minimal maʼlumot uzatiladi: pseudonim
identifikator, viloyat, kasbiy koʻnikmalar va taʼlim darajasi. Telefon raqami,
toʻliq ism, oilaviy holat va farzandlar soni hech qachon uzatilmaydi.

Roziligingizni istalgan vaqtda qaytarib olishingiz mumkin - kabinetdagi
"Rozilik" boʻlimidan. Qaytarib olingandan keyin yangi uzatishlar toʻxtaydi.
""",
    ),
    (
        "Imkoniyatga ariza berish tartibi",
        "faq",
        """
Imkoniyatlar boʻlimida sizga mos vakansiya, grant yoki savdo maydoni tavsiya
qilinadi. Moslik sizning koʻnikmalaringiz, viloyatingiz va rivojlanish
rejangiz asosida hisoblanadi.

Ariza berish uchun avval tegishli hamkor tizimga maʼlumot uzatishga rozilik
berishingiz kerak. Rozilik boʻlmasa, tizim arizani qabul qilmaydi - bu himoya
choralari.

Ariza holati kabinetda kuzatiladi: yuborilgan, koʻrib chiqilmoqda, qabul
qilingan yoki rad etilgan. Hamkor tizimdan javob kelganda sizga bildirishnoma
yuboriladi.
""",
    ),
    (
        "Mentor bilan qanday ishlash kerak",
        "guideline",
        """
Mentor - bu sizga oʻz tajribasini ulashadigan amaliyotchi. Mentor sizning
oʻrningizga qaror qabul qilmaydi va ish topib bermaydi; u yoʻlni koʻrsatadi.

Birinchi uchrashuvga tayyorlaning: qaysi savolga javob olmoqchisiz va qanday
natijani kutayotganingizni yozib qoʻying. Uchrashuv odatda 45-60 daqiqa davom
etadi.

Mentorlik bepul va ixtiyoriy. Agar mos kelmasa, boshqa mentorni tanlashingiz
mumkin - bu normal holat.
""",
    ),
    (
        "Mehnat huquqlari: dekret, taʼtil va shartnoma",
        "guideline",
        """
Mehnat shartnomasi yozma shaklda tuzilishi shart. Shartnomada lavozim, ish haqi,
ish vaqti va taʼtil muddati koʻrsatiladi. Ogʻzaki kelishuv sizni himoya qilmaydi.

Yillik haq toʻlanadigan taʼtil kamida 15 ish kunini tashkil qiladi. Homiladorlik
va tugʻish taʼtili qonunda alohida belgilangan va uni bekor qilib boʻlmaydi.

Ish beruvchi homiladorlik sababli ishga qabul qilishni rad eta olmaydi va
shartnomani bekor qila olmaydi. Huquqingiz buzilsa, mehnat inspeksiyasiga
murojaat qilishingiz mumkin.

Bu umumiy maʼlumot. Aniq holat boʻyicha yurist bilan maslahatlashing.
""",
    ),
    (
        "Raqamli xavfsizlik: firibgarlikdan himoya",
        "guideline",
        """
Hech kimga SMS orqali kelgan kodni aytmang. Bank, portal yoki davlat idorasi
hech qachon kodni soʻramaydi. Kodni soʻrayotgan har qanday odam - firibgar.

Ish taklifi uchun oldindan pul soʻralsa, bu firibgarlik. Haqiqiy ish beruvchi
nomzoddan toʻlov talab qilmaydi.

Parolni har bir xizmat uchun alohida qiling va imkon boʻlsa ikki bosqichli
tasdiqlashni yoqing. Shubhali havolalarni ochmang.
""",
    ),
    (
        "Diagnostikadan qayta oʻtish va natijani yaxshilash",
        "faq",
        """
Diagnostikadan qayta oʻtish mumkin va bu tavsiya etiladi - odatda uch oyda bir
marta. Birinchi natija bazaviy koʻrsatkich sifatida saqlanadi, keyingi natijalar
u bilan solishtiriladi.

Ballni sunʼiy oshirishga urinmang. Diagnostika sizni baholash uchun emas, sizga
mos yoʻnalishni topish uchun kerak. Haqiqiy javob aniqroq reja beradi.

Ball oʻsishi uchun rejadagi qadamlarni bajaring: dasturni tugating, koʻnikma
qoʻshing, imkoniyatga ariza bering. Har bir bajarilgan qadam tegishli oʻlchovda
aks etadi.
""",
    ),
]


# --------------------------------------------------------------------------
# Synthetic behaviour. The catalogue above is content; everything here is what
# users did with it — without this the cabinet, the score distribution and the
# whole funnel in the admin dashboard read as zero.
# --------------------------------------------------------------------------


def _application_status(kind: OpportunityType) -> ApplicationStatus:
    """How an application to this kind of opportunity typically ends.

    A marketplace shelf is onboarding, not a contest, so most sellers get in.
    A grant or an investment round is the opposite. Modelling them with one
    flat acceptance rate is what left commerce activation reading zero while
    thirteen women had applied.
    """
    weights = {
        OpportunityType.MARKETPLACE: (18, 12, 52, 14, 4),
        OpportunityType.MENTORSHIP: (20, 14, 46, 16, 4),
        OpportunityType.INTERNATIONAL_PROGRAM: (24, 22, 18, 32, 4),
        OpportunityType.GRANT: (26, 26, 12, 32, 4),
        OpportunityType.INVESTMENT: (26, 28, 9, 33, 4),
        OpportunityType.INTERNSHIP: (24, 20, 26, 26, 4),
        OpportunityType.VACANCY: (28, 22, 17, 27, 6),
    }
    return random.choices(
        [
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.IN_REVIEW,
            ApplicationStatus.ACCEPTED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
        ],
        weights=weights[kind],
    )[0]


def _pick_opportunities(reachable: list[Opportunity], wanted: int) -> list[Opportunity]:
    """Draw `wanted` distinct opportunities, favouring the partner platforms.

    WomanUP's own listings are open to everyone, so an unweighted draw makes
    them look like most of the traffic. In reality the point of the portal is
    the partner pipeline, and the KPI section 09 reports counts jobs and
    registered businesses — both of which come from partners.
    """
    pool = list(reachable)
    weights = [1 if o.source is OpportunitySource.INTERNAL else 4 for o in pool]
    picked: list[Opportunity] = []
    for _ in range(wanted):
        if not pool:
            break
        chosen = random.choices(pool, weights=weights)[0]
        at = pool.index(chosen)
        pool.pop(at)
        weights.pop(at)
        picked.append(chosen)
    return picked


def _outcome_for(source: OpportunitySource) -> str:
    """What a confirmed result from this partner actually is.

    Invest HUB splits two ways on purpose: money received is `funding`, but a
    registered business is the KPI section 09 reports separately, and it only
    ever appears downstream of investment.
    """
    if source is OpportunitySource.EDU_JOB:
        return "employment"
    if source is OpportunitySource.INVEST_HUB:
        return random.choice(["funding", "business_registered", "business_registered"])
    if source is OpportunitySource.COMMERCE:
        return "first_sale"
    return "milestone"


def _make_profile(user: User, now: datetime) -> Profile:
    """A plausible profile. Education and occupation are drawn together so the
    combination stays coherent — no PhD sewing-machine operators."""
    education_level, education_field = random.choice(EDUCATION)
    employment, profession, skills = random.choice(OCCUPATIONS)

    age = random.randint(19, 54)
    # Derive the date first and the bracket from it, so the two facts on the row
    # cannot disagree. Drawing them independently let a profile carry a bracket
    # a year away from the date the age gate actually reads.
    born = (now - timedelta(days=age * 365 + random.randint(0, 364))).date()
    bracket = group_for_age(years_between(born, now.date()))
    married = random.random() < 0.68
    children = random.choice([0, 1, 1, 2, 2, 3]) if married else random.choice([0, 0, 1])

    languages = ["uz"]
    if random.random() < 0.55:
        languages.append("ru")
    if random.random() < 0.18:
        languages.append("en")

    filled = sum(
        1 for value in (profession, education_field, skills, employment, education_level) if value
    )

    return Profile(
        user_id=user.id,
        full_name=f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
        birth_date=born,
        # The editorial vocabulary the news ranker keys its age map on. The
        # ten-year bands this used to write ("20-29") match no `AgeGroup` value,
        # so every seeded reader missed the lookup silently.
        age_group=bracket.value if bracket else None,
        district=random.choice(DISTRICTS),
        education_level=education_level,
        education_field=education_field,
        employment_status=employment,
        profession=profession,
        years_of_experience=max(0, min(age - 20, random.randint(0, 18))),
        marital_status="turmush qurgan" if married else "turmush qurmagan",
        children_count=children,
        has_disability=random.random() < 0.04,
        is_in_women_register=random.random() < 0.22,
        skills=list(skills),
        interests=random.sample(INTERESTS, random.randint(1, 4)),
        languages=languages,
        completeness_percent=min(100, 35 + filled * 12 + len(skills) * 4),
    )


def _consent_scopes(profile: Profile) -> list[ConsentScope]:
    """Terms and privacy are a precondition for having an account at all; the
    rest is genuinely optional, which is why the partner scopes are sparse."""
    scopes = [ConsentScope.TERMS_OF_USE, ConsentScope.PRIVACY_POLICY]
    if random.random() < 0.82:
        scopes.append(ConsentScope.PROFILE_ANALYTICS)
    if random.random() < 0.71:
        scopes.append(ConsentScope.AI_PERSONALISATION)
    if random.random() < 0.44:
        scopes.append(ConsentScope.MARKETING_COMMUNICATION)

    # Sharing consent correlates with what the woman is actually after.
    # Employment is what most women come to the portal for, so the Edu-Job
    # scope is the one they grant readily; the other two track who actually
    # has something to sell or fund.
    looking_for_work = profile.employment_status in {"ish qidirmoqda", "talaba"}
    if random.random() < (0.88 if looking_for_work else 0.44):
        scopes.append(ConsentScope.SHARE_EDU_JOB)
    runs_a_business = profile.employment_status == "oʻz ishi bor"
    if random.random() < (0.8 if runs_a_business else 0.2):
        scopes.append(ConsentScope.SHARE_INVEST_HUB)
    makes_things = "hunarmandchilik" in profile.skills or profile.profession == "hunarmand"
    if random.random() < (0.76 if makes_things else 0.15):
        scopes.append(ConsentScope.SHARE_COMMERCE)
    return scopes


def _answer_bias(profile: Profile) -> dict[ScoreDimension, float]:
    """Per-dimension offsets for one woman's answers.

    Three things stack here, and all three matter:

    * a personal level, because some women are simply further along overall —
      without it every composite score converges on the mean and the national
      distribution collapses into a single spike;
    * a per-dimension offset drawn once per person, so her three answers within
      a dimension agree and "weakest dimension" is a real signal rather than
      noise — the plan is built from exactly that;
    * profile-driven shifts, so the score matches the woman it belongs to.
    """
    level = random.gauss(0, 17)
    bias = {dimension: level + random.gauss(0, 13) for dimension in ScoreDimension}

    if profile.education_level and profile.education_level.startswith("oliy"):
        bias[ScoreDimension.EDUCATION_SKILLS] += 20
        bias[ScoreDimension.INTERNATIONAL_INTEGRATION] += 9
    elif profile.education_level == "oʻrta":
        bias[ScoreDimension.EDUCATION_SKILLS] -= 16

    if profile.employment_status == "band":
        bias[ScoreDimension.EMPLOYMENT] += 27
        bias[ScoreDimension.FINANCIAL_LITERACY] += 9
    elif profile.employment_status == "oʻz ishi bor":
        bias[ScoreDimension.ENTREPRENEURSHIP] += 30
        bias[ScoreDimension.FINANCIAL_LITERACY] += 14
        bias[ScoreDimension.EMPLOYMENT] += 10
    elif profile.employment_status in {"ish qidirmoqda", "uy bekasi"}:
        bias[ScoreDimension.EMPLOYMENT] -= 20
        bias[ScoreDimension.ENTREPRENEURSHIP] -= 10

    if "en" in profile.languages:
        bias[ScoreDimension.INTERNATIONAL_INTEGRATION] += 26
    else:
        bias[ScoreDimension.INTERNATIONAL_INTEGRATION] -= 12

    if (profile.children_count or 0) > 0:
        bias[ScoreDimension.FAMILY_PARENTING] += 16
    if len(profile.interests) >= 3:
        bias[ScoreDimension.SOCIAL_ACTIVITY] += 12
    if profile.years_of_experience and profile.years_of_experience >= 8:
        bias[ScoreDimension.EDUCATION_SKILLS] += 8
        bias[ScoreDimension.EMPLOYMENT] += 8
    return bias


async def _run_assessment(
    session,
    user: User,
    profile: Profile,
    questions: list[AssessmentQuestion],
    *,
    taken_at: datetime,
) -> dict[ScoreDimension, float]:
    """A completed diagnostic plus the scores it produces, through the same
    scoring service the API uses — so demo numbers and live numbers agree."""
    assessment = Assessment(
        user_id=user.id,
        version=1,
        started_at=taken_at - timedelta(minutes=random.randint(9, 22)),
        completed_at=taken_at,
        is_baseline=True,
    )
    session.add(assessment)
    await session.flush()

    bias = _answer_bias(profile)
    for question in questions:
        # Small per-question noise only — the dimension-level offset above is
        # what makes a dimension consistently strong or weak for this person.
        centre = 48 + bias[question.dimension] + random.gauss(0, 9)
        # Answers are one of the four fixed options, so snap to the scale.
        value = min(OPTIONS, key=lambda o: abs(o["value"] - centre))["value"]
        session.add(
            AssessmentAnswer(
                assessment_id=assessment.id,
                question_id=question.id,
                value=value,
            )
        )
    await session.flush()

    scores = await calculate_dimension_scores(session, assessment.id)
    await persist_scores(session, user.id, assessment.id, scores)
    return scores


def _make_plan(
    user: User,
    scores: dict[ScoreDimension, float],
    programs: list[Program],
    *,
    created_at: datetime,
    accepted: bool,
) -> DevelopmentPlan:
    """A roadmap over the weakest dimensions.

    Created inactive on purpose: a generated plan is a proposal until the user
    accepts it, and the accepted ones below are the ones she confirmed.
    """
    lang = user.language.value if user.language else "uz"
    index_for = {"uz": 0, "ru": 1, "en": 2}.get(lang, 0)
    weakest = [d for d, _ in sorted(scores.items(), key=lambda kv: kv[1])[:3]]
    horizon = random.choice([GoalHorizon.M3, GoalHorizon.M6, GoalHorizon.M6, GoalHorizon.M12])

    plan = DevelopmentPlan(
        user_id=user.id,
        horizon=horizon,
        title=PLAN_TITLE[lang],
        summary=PLAN_SUMMARY[lang],
        is_active=accepted,
        accepted_at=created_at + timedelta(days=random.randint(0, 3)) if accepted else None,
        generated_by_ai=True,
        model_version="rule-based-v1",
        prompt_version="v1",
        rationale={"weakest": [d.value for d in weakest]},
        created_at=created_at,
    )

    order = 0
    for dimension in weakest:
        for action in random.sample(PLAN_ACTIONS[dimension], random.randint(1, 2)):
            done = accepted and random.random() < 0.34
            in_progress = accepted and not done and random.random() < 0.3
            plan.items.append(
                PlanItem(
                    order_index=order,
                    action=action[index_for],
                    dimension=dimension,
                    priority=Priority.HIGH if order < 2 else Priority.MEDIUM,
                    status=(
                        PlanItemStatus.DONE
                        if done
                        else PlanItemStatus.IN_PROGRESS
                        if in_progress
                        else PlanItemStatus.PLANNED
                    ),
                    due_date=(created_at + timedelta(days=30 * (order + 1))).date(),
                    completed_at=(
                        created_at + timedelta(days=random.randint(5, 40)) if done else None
                    ),
                    program_id=random.choice(programs).id if random.random() < 0.45 else None,
                )
            )
            order += 1
    return plan


DEMO_NAME_SUFFIX = "(demo)"


async def _real_accounts(session) -> list[str]:
    """Accounts this script did not create.

    The wipe below takes `users` with it, and the cascade takes her profile,
    her score, her plan and her enrolments too. Every demo account is either
    generated from the name pools above or carries the "(demo)" suffix, so
    anything else on the table is somebody who registered through the sign-up
    form — and running a seeder to refresh content is not a reason to delete
    her. This is not hypothetical: it has already happened once.
    """
    rows = await session.execute(
        select(User.email, Profile.full_name)
        .join(Profile, Profile.user_id == User.id, isouter=True)
        .where(User.status != UserStatus.DELETED)
    )
    generated = {f"{first} {last}" for first in FIRST_NAMES for last in LAST_NAMES}
    real = []
    for email, full_name in rows.all():
        if full_name in generated:
            continue
        if full_name and full_name.endswith(DEMO_NAME_SUFFIX):
            continue
        if email in {DEMO_USER_EMAIL, DEMO_ADMIN_LOGIN}:
            continue
        real.append(full_name or email or "—")
    return real


async def seed(*, force: bool = False) -> None:
    configure_logging()
    now = datetime.now(UTC)

    async with SessionLocal() as session:
        registered = await _real_accounts(session)
        if registered and not force:
            listed = ", ".join(sorted(registered)[:8])
            more = f" (+{len(registered) - 8})" if len(registered) > 8 else ""
            raise SystemExit(
                f"\n  Refusing to seed: {len(registered)} account(s) on this database were "
                f"not created by this script and would be deleted — {listed}{more}.\n"
                "  To load only the news feed without touching accounts:\n"
                "      python -m app.seed_news\n"
                "  To wipe and reseed anyway:\n"
                "      python -m app.seed --force\n"
            )
        # Idempotent: clear what this script owns, in dependency order.
        # Users go last and take profiles, consents, scores, plans, enrolments,
        # applications and outcomes with them via ON DELETE CASCADE — deleting
        # only the leaf tables used to strand development_scores and to pile up
        # a fresh 120 users on every run.
        for model in (
            AssessmentAnswer,
            Assessment,
            Application,
            PlanItem,
            DevelopmentPlan,
            Enrollment,
            ProgramModule,
            Program,
            OutcomeRecord,
            Opportunity,
            NewsPost,
            MentorProfile,
            KnowledgeChunk,
            KnowledgeDocument,
            AssessmentQuestion,
            ConsentLog,
            Profile,
            UserRole,
            User,
        ):
            await session.execute(delete(model))
        await session.commit()

        # --- Diagnostic questions -----------------------------------------
        questions: list[AssessmentQuestion] = []
        for dimension, items in QUESTIONS.items():
            for index, (uz, ru, en) in enumerate(items):
                question = AssessmentQuestion(
                    version=1,
                    dimension=dimension,
                    order_index=index,
                    question_type="single_choice",
                    text_i18n={"uz": uz, "ru": ru, "en": en},
                    options=OPTIONS,
                    weight=1.0,
                    is_active=True,
                )
                session.add(question)
                questions.append(question)
        await session.flush()
        logger.info("seeded %s assessment questions", len(questions))

        # --- Programmes ----------------------------------------------------
        programs: list[Program] = []
        for slug, category, fmt, uz, ru, en, goal, hours, weeks, skills, cert, outcomes in PROGRAMS:
            translation = PROGRAM_TRANSLATIONS.get(slug, {})
            goal_ru, goal_en = translation.get("goal", (None, None))
            outcome_translations = translation.get("outcomes", [])
            program = Program(
                slug=slug,
                title_i18n={"uz": uz, "ru": ru, "en": en},
                goal_i18n=_merge(goal, goal_ru, goal_en),
                description_i18n=_merge(goal, goal_ru, goal_en),
                category=category,
                format=fmt,
                language=Language.UZ,
                target_age_min=18,
                target_age_max=65,
                duration_hours=hours,
                duration_weeks=weeks,
                learning_outcomes=[
                    _merge(
                        outcome,
                        *(
                            outcome_translations[i]
                            if i < len(outcome_translations)
                            else (None, None)
                        ),
                    )
                    for i, outcome in enumerate(outcomes)
                ],
                skills_taught=skills,
                assessment_type="test",
                has_certificate=cert,
                provider="WomanUP Academy",
                is_published=True,
                published_at=now - timedelta(days=random.randint(10, 200)),
            )
            session.add(program)
            programs.append(program)
        await session.flush()

        for program in programs:
            # Real module titles and descriptions, so the course page can say
            # what the course actually covers. A programme without a written
            # curriculum gets numbered placeholders rather than a wrong one.
            curriculum = PROGRAM_MODULES.get(program.slug)
            if curriculum:
                for index, (uz, ru, en, uz_b, ru_b, en_b, minutes) in enumerate(curriculum):
                    session.add(
                        ProgramModule(
                            program_id=program.id,
                            order_index=index,
                            title_i18n={"uz": uz, "ru": ru, "en": en},
                            content_i18n={"uz": uz_b, "ru": ru_b, "en": en_b},
                            duration_minutes=minutes,
                        )
                    )
                continue

            for index in range(max(3, int(program.duration_weeks or 4))):
                session.add(
                    ProgramModule(
                        program_id=program.id,
                        order_index=index,
                        title_i18n={
                            "uz": f"{index + 1}-modul",
                            "ru": f"Модуль {index + 1}",
                            "en": f"Module {index + 1}",
                        },
                        duration_minutes=random.choice([20, 30, 45, 60]),
                    )
                )
        await session.flush()
        logger.info("seeded %s programmes", len(programs))

        # --- Opportunities --------------------------------------------------
        for source, otype, title, description, org, region, skills, reward, days in OPPORTUNITIES:
            session.add(
                Opportunity(
                    source=source,
                    # Keyed off the Uzbek title: the primary locale is the one
                    # that stays stable if a translation is later reworded.
                    external_id=f"demo-{_slug(title[0])}",
                    type=otype,
                    title_i18n=_tri(title),
                    description_i18n=_tri(description),
                    organisation=org,
                    region=region,
                    required_skills=skills,
                    reward=reward,
                    deadline=now + timedelta(days=days),
                    is_active=True,
                    synced_at=now,
                )
            )
        await session.flush()
        logger.info("seeded %s opportunities", len(OPPORTUNITIES))

        # --- News and announcements -----------------------------------------
        # The feed is the first screen after registration, so it must not be
        # empty on a fresh database. The loader lives in `seed_news` and is
        # runnable on its own — refreshing the feed must not require running
        # this script, which clears `users` and would take every account
        # registered since the last run with it.
        logger.info("seeded %s news posts", await load_news(session, now))

        # --- Knowledge base (approved, so the navigator can retrieve it) ----
        knowledge_documents = 0
        for title, source_type, content in KNOWLEDGE:
            # One document per locale. Retrieval filters by document language,
            # so a locale without its own edition matches nothing at all —
            # keyword search cannot cross languages.
            editions = [("uz", title, content)]
            for language, (translated_title, translated_body) in KNOWLEDGE_TRANSLATIONS.get(
                title, {}
            ).items():
                editions.append((language, translated_title, translated_body))

            for language, edition_title, edition_body in editions:
                document = KnowledgeDocument(
                    title=edition_title,
                    source_type=source_type,
                    language=language,
                    tags=["demo"],
                    is_approved=True,
                    approved_at=now,
                )
                session.add(document)
                await session.flush()
                for index, chunk in enumerate(rag.chunk_text(edition_body.strip())):
                    session.add(
                        KnowledgeChunk(
                            document_id=document.id,
                            chunk_index=index,
                            content=chunk,
                            token_count=len(chunk.split()),
                        )
                    )
                knowledge_documents += 1
        await session.flush()
        logger.info(
            "seeded %s knowledge documents across %s topics",
            knowledge_documents,
            len(KNOWLEDGE),
        )

        # --- Demo accounts ---------------------------------------------------
        admin = await _ensure_account(
            session, DEMO_ADMIN_PHONE, Role.ADMIN, "Durdona (demo admin)", now
        )
        # Staff sign in with a login and a password, not with an SMS code: an
        # office account belongs to a desk, not to somebody's handset.
        admin.email = DEMO_ADMIN_LOGIN
        admin.email_verified = True
        admin.password_hash = hash_password(DEMO_ADMIN_PASSWORD)
        admin.onboarding_completed_at = admin.onboarding_completed_at or now
        demo_user = await _ensure_account(
            session,
            DEMO_USER_PHONE,
            Role.USER,
            "Dilnoza Karimova (demo)",
            now,
            email=DEMO_USER_EMAIL,
            password=DEMO_USER_PASSWORD,
            region=Region.FERGANA,
            profession="buxgalter",
            skills=["excel", "buxgalteriya"],
            education="oliy",
        )

        # Consents so the partner flows are demonstrable out of the box.
        for scope in (
            ConsentScope.TERMS_OF_USE,
            ConsentScope.PRIVACY_POLICY,
            ConsentScope.SHARE_EDU_JOB,
            ConsentScope.AI_PERSONALISATION,
        ):
            session.add(
                ConsentLog(
                    user_id=demo_user.id,
                    scope=scope,
                    accepted=True,
                    policy_version="1.0",
                    accepted_at=now,
                )
            )

        # A completed baseline assessment for the demo user.
        assessment = Assessment(
            user_id=demo_user.id,
            version=1,
            started_at=now - timedelta(minutes=15),
            completed_at=now,
            is_baseline=True,
        )
        session.add(assessment)
        await session.flush()

        demo_values = {
            ScoreDimension.EDUCATION_SKILLS: [75, 50, 75],
            ScoreDimension.EMPLOYMENT: [25, 50, 25],
            ScoreDimension.ENTREPRENEURSHIP: [0, 25, 0],
            ScoreDimension.FINANCIAL_LITERACY: [50, 25, 50],
            ScoreDimension.HEALTHY_LIFESTYLE: [50, 50, 75],
            ScoreDimension.FAMILY_PARENTING: [75, 75, 50],
            ScoreDimension.SOCIAL_ACTIVITY: [25, 25, 50],
            ScoreDimension.INTERNATIONAL_INTEGRATION: [25, 0, 0],
        }
        for question in questions:
            values = demo_values[question.dimension]
            session.add(
                AssessmentAnswer(
                    assessment_id=assessment.id,
                    question_id=question.id,
                    value=values[question.order_index],
                )
            )
        await session.flush()

        scores = await calculate_dimension_scores(session, assessment.id)
        await persist_scores(session, demo_user.id, assessment.id, scores)

        session.add(
            Enrollment(
                user_id=demo_user.id,
                program_id=programs[0].id,
                status=EnrollmentStatus.IN_PROGRESS,
                progress_percent=33,
                started_at=now - timedelta(days=9),
                last_activity_at=now - timedelta(days=1),
            )
        )
        session.add(
            Enrollment(
                user_id=demo_user.id,
                program_id=programs[1].id,
                status=EnrollmentStatus.COMPLETED,
                progress_percent=100,
                started_at=now - timedelta(days=64),
                completed_at=now - timedelta(days=21),
                last_activity_at=now - timedelta(days=21),
            )
        )

        # An accepted plan, so the demo cabinet opens on a live roadmap rather
        # than an empty "generate a plan" state.
        demo_plan = _make_plan(
            demo_user,
            scores,
            programs,
            created_at=now - timedelta(days=11),
            accepted=True,
        )
        session.add(demo_plan)

        # One application per partner the demo user consented to, in three
        # different states, so every branch of the status UI is visible.
        demo_targets = (
            (
                await session.execute(
                    select(Opportunity)
                    .where(Opportunity.source == OpportunitySource.EDU_JOB)
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        for offset, (opportunity, status) in enumerate(
            zip(
                demo_targets,
                (
                    ApplicationStatus.ACCEPTED,
                    ApplicationStatus.IN_REVIEW,
                    ApplicationStatus.REJECTED,
                ),
                strict=False,
            )
        ):
            submitted = now - timedelta(days=18 - offset * 5)
            resolved = status is not ApplicationStatus.IN_REVIEW
            demo_application = Application(
                user_id=demo_user.id,
                opportunity_id=opportunity.id,
                status=status,
                external_application_id=f"demo-user-{offset}",
                payload={"source": "seed"},
                status_history=[
                    {"status": ApplicationStatus.SUBMITTED.value, "at": submitted.isoformat()}
                ],
                submitted_at=submitted,
                resolved_at=submitted + timedelta(days=6) if resolved else None,
            )
            session.add(demo_application)
            await session.flush()
            if status is ApplicationStatus.ACCEPTED:
                session.add(
                    OutcomeRecord(
                        user_id=demo_user.id,
                        application_id=demo_application.id,
                        source=opportunity.source,
                        outcome_type="employment",
                        verified=True,
                        occurred_at=demo_application.resolved_at,
                    )
                )

        # --- The synthetic population ---------------------------------------
        # Content alone leaves every funnel at zero, so each of these women
        # gets a profile, and then a realistic slice of them actually goes
        # through the journey: diagnostic -> plan -> programme -> application.
        regions = list(Region)
        opportunities = list((await session.execute(select(Opportunity))).scalars())
        stats = {"profiles": 0, "assessed": 0, "plans": 0, "accepted": 0, "applications": 0}

        for index in range(120):
            created = now - timedelta(days=random.randint(0, 180))
            active = random.random() < 0.62
            onboarded = random.random() < 0.74
            user = User(
                phone=f"+9989{random.randint(10000000, 99999999)}{index % 10}"[:13],
                email=f"demo.user{index:03d}@womanup.uz",
                email_verified=True,
                status=UserStatus.ACTIVE,
                language=Language.UZ if random.random() < 0.78 else Language.RU,
                region=random.choice(regions),
                phone_verified=True,
                onboarding_completed_at=created if onboarded else None,
                last_active_at=now - timedelta(days=random.randint(0, 25)) if active else created,
                # Explicit, because the column defaults to now() server-side —
                # without this every woman looks like she registered today and
                # retention D30 has an empty cohort to divide by.
                created_at=created,
                updated_at=created,
            )
            session.add(user)
            await session.flush()
            session.add(UserRole(user_id=user.id, role=Role.USER))

            profile = _make_profile(user, now)
            session.add(profile)
            stats["profiles"] += 1

            scopes = _consent_scopes(profile)
            for scope in scopes:
                session.add(
                    ConsentLog(
                        user_id=user.id,
                        scope=scope,
                        accepted=True,
                        policy_version="1.0",
                        accepted_at=created,
                    )
                )

            # A woman who never finished onboarding never reached the diagnostic.
            assessed = onboarded and random.random() < 0.78
            if assessed:
                taken_at = min(created + timedelta(days=random.randint(0, 6)), now)
                scores = await _run_assessment(session, user, profile, questions, taken_at=taken_at)
                stats["assessed"] += 1

                if scores and random.random() < 0.72:
                    accepted = random.random() < 0.66
                    session.add(
                        _make_plan(
                            user,
                            scores,
                            programs,
                            created_at=min(taken_at + timedelta(days=random.randint(0, 2)), now),
                            accepted=accepted,
                        )
                    )
                    stats["plans"] += 1
                    stats["accepted"] += int(accepted)

            if random.random() < 0.55:
                session.add(
                    Enrollment(
                        user_id=user.id,
                        program_id=random.choice(programs).id,
                        status=random.choice(
                            [
                                EnrollmentStatus.COMPLETED,
                                EnrollmentStatus.IN_PROGRESS,
                                EnrollmentStatus.COMPLETED,
                                EnrollmentStatus.DROPPED,
                            ]
                        ),
                        progress_percent=random.randint(10, 100),
                        started_at=created,
                    )
                )

            # Applications only where the matching partner consent exists —
            # the consent gate is an invariant, not a nicety, so the demo data
            # must not contain a single transfer that could not have happened.
            # WomanUP's own opportunities need no partner consent — nothing
            # leaves the platform for them, so they are always reachable.
            reachable = [
                opportunity
                for opportunity in opportunities
                if opportunity.source is OpportunitySource.INTERNAL
                or CONSENT_FOR_SOURCE[opportunity.source] in scopes
            ]
            if reachable and assessed and random.random() < 0.72:
                wanted = min(len(reachable), random.randint(1, 3))
                for opportunity in _pick_opportunities(reachable, wanted):
                    submitted = created + timedelta(days=random.randint(2, 30))
                    if submitted > now:
                        submitted = now - timedelta(days=1)
                    status = _application_status(opportunity.type)
                    resolved = status in {
                        ApplicationStatus.ACCEPTED,
                        ApplicationStatus.REJECTED,
                        ApplicationStatus.WITHDRAWN,
                    }
                    application = Application(
                        user_id=user.id,
                        opportunity_id=opportunity.id,
                        status=status,
                        external_application_id=f"demo-{opportunity.source.value}-{index}",
                        payload={"source": "seed"},
                        status_history=[
                            {
                                "status": ApplicationStatus.SUBMITTED.value,
                                "at": submitted.isoformat(),
                            }
                        ],
                        submitted_at=submitted,
                        resolved_at=submitted + timedelta(days=random.randint(3, 21))
                        if resolved
                        else None,
                    )
                    session.add(application)
                    await session.flush()
                    stats["applications"] += 1

                    # A confirmed result is what the North Star metric counts,
                    # and it only exists downstream of an accepted application.
                    if status is ApplicationStatus.ACCEPTED:
                        session.add(
                            OutcomeRecord(
                                user_id=user.id,
                                application_id=application.id,
                                source=opportunity.source,
                                outcome_type=_outcome_for(opportunity.source),
                                verified=True,
                                occurred_at=application.resolved_at or now,
                            )
                        )

        logger.info(
            "population: %s profiles, %s assessed, %s plans (%s accepted), %s applications",
            stats["profiles"],
            stats["assessed"],
            stats["plans"],
            stats["accepted"],
            stats["applications"],
        )

        # --- Mentors ---------------------------------------------------------
        for index, (headline, bio, expertise, languages, years, rating) in enumerate(MENTORS):
            mentor_user = User(
                phone=f"+99893000{index:04d}",
                email=f"mentor{index:02d}@womanup.uz",
                email_verified=True,
                status=UserStatus.ACTIVE,
                region=random.choice(regions),
                phone_verified=True,
            )
            session.add(mentor_user)
            await session.flush()
            session.add(UserRole(user_id=mentor_user.id, role=Role.MENTOR))
            mentor_profile = _make_profile(mentor_user, now)
            mentor_profile.employment_status = "band"
            mentor_profile.years_of_experience = years
            mentor_profile.completeness_percent = 100
            session.add(mentor_profile)
            session.add(
                ConsentLog(
                    user_id=mentor_user.id,
                    scope=ConsentScope.TERMS_OF_USE,
                    accepted=True,
                    policy_version="1.0",
                    accepted_at=now,
                )
            )
            session.add(
                MentorProfile(
                    user_id=mentor_user.id,
                    headline=headline,
                    bio=bio,
                    expertise=expertise,
                    languages=languages,
                    years_of_experience=years,
                    rating_avg=rating,
                    sessions_count=random.randint(4, 40),
                    is_verified=True,
                    verified_at=now,
                    max_mentees=8,
                )
            )

        # --- Risk flags -------------------------------------------------
        # Derived from what the population actually did, never sprinkled at
        # random: a coordinator's queue is only useful if every entry can be
        # traced back to the row that raised it. Flags notify, they never
        # restrict access.
        await session.flush()
        flags = 0

        stale = now - timedelta(days=45)
        inactive = (
            await session.execute(
                select(User).where(
                    User.onboarding_completed_at.is_not(None),
                    User.last_active_at < stale,
                )
            )
        ).scalars()
        for user in inactive:
            session.add(
                RiskFlag(
                    user_id=user.id,
                    flag_type=RiskFlagType.INACTIVITY,
                    severity="low",
                    reason="45 kundan beri faollik yoʻq",
                    evidence={"last_active_at": user.last_active_at.isoformat()},
                )
            )
            flags += 1

        dropped = (
            await session.execute(
                select(Enrollment.user_id, func.count(Enrollment.id))
                .where(Enrollment.status == EnrollmentStatus.DROPPED)
                .group_by(Enrollment.user_id)
            )
        ).all()
        for user_id, count in dropped:
            session.add(
                RiskFlag(
                    user_id=user_id,
                    flag_type=RiskFlagType.DROPOUT_RISK,
                    severity="medium" if count > 1 else "low",
                    reason="Dasturni tashlab ketgan",
                    evidence={"dropped_enrolments": count},
                )
            )
            flags += 1

        # An accepted plan that has not moved in a month is the signal the
        # coordinator actually acts on.
        stalled = (
            await session.execute(
                select(DevelopmentPlan)
                .where(
                    DevelopmentPlan.accepted_at.is_not(None),
                    DevelopmentPlan.created_at < now - timedelta(days=30),
                )
                .options(selectinload(DevelopmentPlan.items))
            )
        ).scalars()
        for plan in stalled:
            if any(item.status is not PlanItemStatus.PLANNED for item in plan.items):
                continue
            session.add(
                RiskFlag(
                    user_id=plan.user_id,
                    flag_type=RiskFlagType.STALLED_PLAN,
                    severity="medium",
                    reason="Qabul qilingan reja bir oydan beri qimirlamagan",
                    evidence={"plan_id": str(plan.id), "items": len(plan.items)},
                )
            )
            flags += 1

        logger.info("seeded %s open risk flags", flags)

        await session.commit()

    logger.info("demo data ready")
    print("\n  Demo maʼlumotlar tayyor.\n")
    print(f"  Admin login:                  {DEMO_ADMIN_LOGIN}")
    print(f"  Admin parol:                  {DEMO_ADMIN_PASSWORD}")
    print(f"  Foydalanuvchi pochtasi:       {DEMO_USER_EMAIL}")
    print(f"  Foydalanuvchi paroli:         {DEMO_USER_PASSWORD}\n")


async def _ensure_account(
    session,
    phone: str,
    role: Role,
    full_name: str,
    now: datetime,
    *,
    region: Region = Region.TASHKENT_CITY,
    email: str | None = None,
    password: str | None = None,
    profession: str | None = None,
    skills: list[str] | None = None,
    education: str | None = None,
) -> User:
    """Create the account if missing; always leave it with the given role."""
    user = await session.scalar(select(User).where(User.phone == phone))
    if user is None:
        user = User(
            phone=phone,
            status=UserStatus.ACTIVE,
            region=region,
            language=Language.UZ,
            phone_verified=True,
        )
        session.add(user)
        await session.flush()

    # Sign-in asks for an e-mail, so every demo account needs one or the
    # catalogue of demo logins on the sign-in screen would not work.
    if email and not user.email:
        user.email = email
        user.email_verified = True
    if password:
        user.password_hash = hash_password(password)

    user.status = UserStatus.ACTIVE
    user.region = region
    user.onboarding_completed_at = user.onboarding_completed_at or now
    user.last_active_at = now

    has_role = await session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role == role)
    )
    if has_role is None:
        session.add(UserRole(user_id=user.id, role=role))

    profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is None:
        profile = Profile(user_id=user.id)
        session.add(profile)
    profile.full_name = full_name
    # Without an age the portal treats the account as possibly a minor — the
    # protective default in `age_gate` — and the demo learner was landing in
    # that band: no adult health content in her feed and an unpersonalised
    # assistant, on the one account every demonstration signs in with.
    profile.birth_date = profile.birth_date or date(now.year - 29, 6, 14)
    demo_bracket = group_for_age(years_between(profile.birth_date, now.date()))
    profile.age_group = profile.age_group or (demo_bracket.value if demo_bracket else None)
    profile.profession = profession
    profile.skills = skills or []
    profile.education_level = education
    profile.employment_status = "ish qidirmoqda" if role is Role.USER else "band"
    profile.district = "Demo tuman"
    profile.completeness_percent = 75

    await session.flush()
    return user


if __name__ == "__main__":
    asyncio.run(seed(force="--force" in sys.argv))
