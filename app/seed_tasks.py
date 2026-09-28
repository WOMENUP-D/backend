"""Practical tasks over the seeded catalogue.

Eight briefs, each tied to a course that already exists and each asking for
something a woman could actually hand in from a phone in a district centre: a
written plan, a budget she has drafted, a link to a shop she has opened. No
uploads, because the platform has object-storage settings and no storage
service, and a form that swallows a file it cannot keep is worse than one that
never offered.

**This seeds catalogue content only.** It creates no attempts, no submissions,
no evaluations and no skill evidence. Every one of those has to be earned by a
real woman doing real work — inventing them would put a verified skill on
somebody's profile that nobody verified, which is the one thing this system
exists to prevent.

The criteria are written out in full because they are shown to her before she
starts and handed to whoever assesses it. Being marked against something you
were never told is the ordinary experience of assessment, and it is avoidable.

Idempotent: a task that exists is updated in place, never duplicated.

    python -m app.seed_tasks
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProficiencyLevel, TaskSubmissionKind
from app.models.practice import PracticalTask
from app.models.program import Program

logger = logging.getLogger(__name__)

L = ProficiencyLevel
K = TaskSubmissionKind


def _i18n(uz: str, ru: str, en: str) -> dict:
    return {"uz": uz, "ru": ru, "en": en}


def _c(key: str, uz: str, ru: str, en: str) -> dict:
    return {"key": key, "text_i18n": _i18n(uz, ru, en)}


TASKS: list[dict] = [
    {
        "slug": "oylik-byudjet-rejasi",
        "program": "moliyaviy-savodxonlik",
        "level": L.BEGINNER,
        "minutes": 45,
        "kind": K.TEXT,
        "min_chars": 400,
        "skills": ["budjet", "jamgarma"],
        "title": _i18n(
            "Bir oylik byudjet rejasi",
            "План бюджета на месяц",
            "A one-month budget plan",
        ),
        "summary": _i18n(
            "Oʻz oilangizning bir oylik daromad va xarajatlarini yozing va "
            "jamgʻarma rejasini tuzing.",
            "Распишите доходы и расходы своей семьи за месяц и составьте план накоплений.",
            "Write out your household's income and spending for one month and "
            "plan what you will save.",
        ),
        "instructions": _i18n(
            "Kelgusi oy uchun byudjet tuzing. Daromadning barcha manbalarini yozing. "
            "Xarajatlarni turkumlarga ajrating: oziq-ovqat, kommunal, transport, "
            "taʼlim, sogʻliq, boshqa. Har bir turkum uchun taxminiy summani koʻrsating. "
            "Oxirida qancha jamgʻarishni rejalashtirganingizni va buni qanday "
            "qilishingizni yozing. Haqiqiy raqamlardan foydalaning — bu reja siz uchun.",
            "Составьте бюджет на следующий месяц. Перечислите все источники дохода. "
            "Разделите расходы по категориям: еда, коммунальные, транспорт, образование, "
            "здоровье, прочее. Для каждой категории укажите примерную сумму. "
            "В конце напишите, сколько планируете отложить и как именно. "
            "Используйте реальные цифры — это план для вас.",
            "Draft a budget for next month. List every source of income. Break spending "
            "into categories: food, utilities, transport, education, health, other. Give "
            "an approximate figure for each. At the end, write how much you plan to save "
            "and how you will do it. Use real numbers — this plan is for you.",
        ),
        "outcome": _i18n(
            "Daromad, turkumlashtirilgan xarajatlar va aniq jamgʻarma summasi boʻlgan reja.",
            "План с доходом, расходами по категориям и конкретной суммой накоплений.",
            "A plan with income, categorised spending and a specific savings figure.",
        ),
        "criteria": [
            _c(
                "income",
                "Daromadning barcha manbalari koʻrsatilgan",
                "Перечислены все источники дохода",
                "Every source of income is listed",
            ),
            _c(
                "categories",
                "Xarajatlar kamida beshta turkumga ajratilgan va summalar bor",
                "Расходы разделены минимум на пять категорий с суммами",
                "Spending is split into at least five categories, each with a figure",
            ),
            _c(
                "savings",
                "Aniq jamgʻarma summasi va uni qanday amalga oshirish yozilgan",
                "Указана конкретная сумма накоплений и способ её отложить",
                "A specific savings figure is given, with how it will be set aside",
            ),
        ],
    },
    {
        "slug": "oilaviy-byudjet-tahlili",
        "program": "oilaviy-byudjet",
        "level": L.ELEMENTARY,
        "minutes": 60,
        "kind": K.FIELDS,
        "skills": ["byudjet", "moliyaviy reja"],
        "title": _i18n(
            "Uch oylik xarajat tahlili",
            "Анализ расходов за три месяца",
            "A three-month spending review",
        ),
        "summary": _i18n(
            "Oxirgi uch oy xarajatlaringizni koʻrib chiqing va nimani "
            "oʻzgartirish mumkinligini toping.",
            "Просмотрите расходы за последние три месяца и найдите, что можно изменить.",
            "Look back over three months of spending and find what could change.",
        ),
        "instructions": _i18n(
            "Har bir savolga oʻz raqamlaringiz bilan javob bering.",
            "Ответьте на каждый вопрос своими цифрами.",
            "Answer each question with your own figures.",
        ),
        "outcome": _i18n(
            "Uchta savolga aniq, raqamli javob.",
            "Три конкретных ответа с цифрами.",
            "Three concrete answers, with numbers.",
        ),
        "fields": [
            {
                "key": "biggest",
                "label_i18n": _i18n(
                    "Eng katta uchta xarajat turkumi qaysi va har biriga qancha ketgan?",
                    "Три самые большие категории расходов и сколько ушло на каждую?",
                    "Your three largest spending categories, and how much each took?",
                ),
                "min_chars": 80,
            },
            {
                "key": "surprise",
                "label_i18n": _i18n(
                    "Qaysi xarajat sizni hayron qoldirdi va nima uchun?",
                    "Какой расход вас удивил и почему?",
                    "Which spending surprised you, and why?",
                ),
                "min_chars": 60,
            },
            {
                "key": "change",
                "label_i18n": _i18n(
                    "Kelgusi oyda nimani oʻzgartirasiz va bu qancha tejaydi?",
                    "Что вы измените в следующем месяце и сколько это сэкономит?",
                    "What will you change next month, and how much will it save?",
                ),
                "min_chars": 80,
            },
        ],
        "criteria": [
            _c(
                "figures",
                "Uchala javobda ham haqiqiy raqamlar bor",
                "Во всех трёх ответах есть реальные цифры",
                "All three answers contain real figures",
            ),
            _c(
                "specific",
                "Oʻzgarish aniq va amalga oshirsa boʻladigan",
                "Изменение конкретное и выполнимое",
                "The change named is specific and doable",
            ),
        ],
    },
    {
        "slug": "biznes-reja-qisqacha",
        "program": "tadbirkorlik-asoslari",
        "level": L.BEGINNER,
        "minutes": 90,
        "kind": K.TEXT,
        "min_chars": 700,
        "skills": ["biznes-reja", "marketing"],
        "title": _i18n(
            "Bir sahifalik biznes-reja",
            "Бизнес-план на одну страницу",
            "A one-page business plan",
        ),
        "summary": _i18n(
            "Gʻoyangizni bir sahifada tushuntiring: kim uchun, qancha turadi, qanday sotasiz.",
            "Объясните свою идею на одной странице: для кого, сколько стоит, как продавать.",
            "Explain your idea on one page: who for, what it costs, how you will sell it.",
        ),
        "instructions": _i18n(
            "Biznes gʻoyangizni yozing. Quyidagilarni albatta yoriting: mahsulot yoki "
            "xizmat nima; mijozingiz kim va u qayerda; bir dona mahsulotning tannarxi "
            "va sotuv narxi; birinchi oyda qancha sotmoqchisiz; mijozga qanday yetib "
            "borasiz. Agar biznes hali boshlanmagan boʻlsa, taxminiy raqamlarni yozing "
            "va qayerdan olganingizni ayting.",
            "Опишите свою бизнес-идею. Обязательно раскройте: что за продукт или услуга; "
            "кто ваш клиент и где он; себестоимость и цена продажи одной единицы; "
            "сколько планируете продать в первый месяц; как вы дойдёте до клиента. "
            "Если бизнес ещё не начат — напишите оценочные цифры и откуда они.",
            "Describe your business idea. Cover all of: what the product or service is; "
            "who your customer is and where they are; what one unit costs you and what "
            "you will sell it for; how many you plan to sell in the first month; how you "
            "will reach the customer. If the business has not started, give estimates and "
            "say where they come from.",
        ),
        "outcome": _i18n(
            "Mahsulot, mijoz, narx va sotuv yoʻli yoritilgan bir sahifalik reja.",
            "План на страницу: продукт, клиент, цена и канал продаж.",
            "A one-page plan covering product, customer, price and channel.",
        ),
        "criteria": [
            _c(
                "product",
                "Mahsulot yoki xizmat aniq tushuntirilgan",
                "Продукт или услуга описаны конкретно",
                "The product or service is described concretely",
            ),
            _c(
                "customer",
                "Mijoz kim ekani va uni qayerdan topish aytilgan",
                "Сказано, кто клиент и где его найти",
                "It says who the customer is and where to find them",
            ),
            _c(
                "numbers",
                "Tannarx, sotuv narxi va birinchi oylik reja raqamlarda",
                "Себестоимость, цена и план первого месяца в цифрах",
                "Cost, price and first-month target are given as numbers",
            ),
            _c(
                "channel",
                "Mijozga qanday yetib borish yoʻli koʻrsatilgan",
                "Указан способ дойти до клиента",
                "A way of reaching the customer is named",
            ),
        ],
    },
    {
        "slug": "smm-kontent-rejasi",
        "program": "smm-va-raqamli-marketing",
        "level": L.ELEMENTARY,
        "minutes": 60,
        "kind": K.TEXT,
        "min_chars": 500,
        "skills": ["smm", "kontent marketing"],
        "title": _i18n(
            "Bir haftalik kontent rejasi",
            "Контент-план на неделю",
            "A one-week content plan",
        ),
        "summary": _i18n(
            "Ijtimoiy tarmoq uchun yetti kunlik post rejasini tuzing.",
            "Составьте план постов на семь дней для соцсети.",
            "Plan seven days of posts for one social account.",
        ),
        "instructions": _i18n(
            "Bitta ijtimoiy tarmoq tanlang. Yetti kun uchun post rejasini yozing: har "
            "kun uchun mavzu, post turi (foto, video, matn, storis) va qisqa matn "
            "gʻoyasi. Kim uchun yozayotganingizni boshida ayting. Kamida ikkita post "
            "sotuvga, qolganlari foyda berishga qaratilgan boʻlsin.",
            "Выберите одну соцсеть. Распишите посты на семь дней: тема дня, формат "
            "(фото, видео, текст, сторис) и короткая идея текста. В начале укажите, для "
            "кого вы пишете. Минимум два поста — продающие, остальные — полезные.",
            "Pick one social account. Plan seven days: the topic for each day, the format "
            "(photo, video, text, story) and a one-line idea for the caption. Say at the "
            "start who you are writing for. At least two posts should sell; the rest "
            "should be useful.",
        ),
        "outcome": _i18n(
            "Auditoriya koʻrsatilgan, yetti kunlik toʻliq reja.",
            "Полный план на семь дней с указанной аудиторией.",
            "A full seven-day plan with the audience named.",
        ),
        "criteria": [
            _c(
                "audience",
                "Auditoriya kim ekani aniq yozilgan",
                "Чётко указано, кто аудитория",
                "The audience is stated clearly",
            ),
            _c(
                "seven",
                "Yettala kun uchun ham mavzu va format bor",
                "Для всех семи дней есть тема и формат",
                "All seven days have a topic and a format",
            ),
            _c(
                "mix",
                "Sotuv va foydali postlar aralash",
                "Продающие и полезные посты чередуются",
                "Selling and useful posts are mixed",
            ),
        ],
    },
    {
        "slug": "mahsulot-kartochkasi",
        "program": "onlayn-savdo-boshlash",
        "level": L.ELEMENTARY,
        "minutes": 45,
        "kind": K.LINK,
        "skills": ["mahsulot kartochkasi", "marketpleys"],
        "title": _i18n(
            "Marketpleysda mahsulot kartochkasi",
            "Карточка товара на маркетплейсе",
            "A marketplace product listing",
        ),
        "summary": _i18n(
            "Haqiqiy mahsulot kartochkasini yarating va havolasini yuboring.",
            "Создайте настоящую карточку товара и пришлите ссылку.",
            "Create a real product listing and send the link.",
        ),
        "instructions": _i18n(
            "Istalgan marketpleysda yoki ijtimoiy tarmoq doʻkoningizda bitta mahsulot "
            "kartochkasini toʻliq tayyorlang: nom, tavsif, narx, kamida uchta surat, "
            "oʻlcham yoki xususiyatlar. Tayyor boʻlgach, ochiq havolasini yuboring — "
            "havola roʻyxatdan oʻtmasdan ochilishi kerak.",
            "На любом маркетплейсе или в вашем магазине в соцсети полностью оформите "
            "карточку одного товара: название, описание, цена, минимум три фото, "
            "размеры или характеристики. Пришлите открытую ссылку — она должна "
            "открываться без регистрации.",
            "On any marketplace, or in your own social shop, build one complete product "
            "listing: name, description, price, at least three photos, and sizes or "
            "specifications. Send the public link — it must open without signing in.",
        ),
        "outcome": _i18n(
            "Ochiq havola orqali koʻrish mumkin boʻlgan toʻliq kartochka.",
            "Полностью заполненная карточка, доступная по открытой ссылке.",
            "A complete listing, reachable at a public link.",
        ),
        "criteria": [
            _c(
                "reachable",
                "Havola ochiladi va kartochkaga olib boradi",
                "Ссылка открывается и ведёт на карточку",
                "The link opens and leads to the listing",
            ),
            _c(
                "complete",
                "Nom, tavsif, narx va suratlar bor",
                "Есть название, описание, цена и фото",
                "Name, description, price and photos are all present",
            ),
        ],
        # A link cannot be read by the model — it would have to guess what is
        # behind it, and guessing is the one thing the reviewer must not do.
        "ai_reviewed": False,
    },
    {
        "slug": "rezyume-tayyorlash",
        "program": "ish-suhbatiga-tayyorgarlik",
        "level": L.ELEMENTARY,
        "minutes": 60,
        "kind": K.TEXT,
        "min_chars": 600,
        "skills": ["rezyume", "portfolio"],
        "title": _i18n("Rezyume yozish", "Составить резюме", "Write your CV"),
        "summary": _i18n(
            "Bitta aniq vakansiya uchun rezyume matnini tayyorlang.",
            "Подготовьте текст резюме под одну конкретную вакансию.",
            "Write a CV aimed at one specific vacancy.",
        ),
        "instructions": _i18n(
            "Sizni qiziqtirgan bitta vakansiyani tanlang va uni qisqacha yozing. "
            "Soʻng oʻsha ish uchun rezyume matnini yozing: qisqa tanishtiruv, ish "
            "tajribasi (yoki tajriba oʻrniga nima qilganingiz), taʼlim, koʻnikmalar, "
            "aloqa. Har bir tajriba uchun nima qilganingizni emas, nimaga erishganingizni "
            "yozing — iloji boʻlsa raqam bilan.",
            "Выберите одну интересную вам вакансию и коротко опишите её. Затем напишите "
            "текст резюме под эту работу: краткое представление, опыт работы (или то, чем "
            "вы занимались вместо него), образование, навыки, контакты. По каждому опыту "
            "пишите не что вы делали, а чего достигли — по возможности с цифрой.",
            "Pick one vacancy that interests you and describe it briefly. Then write a CV "
            "for that job: a short introduction, work experience (or what you did instead "
            "of it), education, skills, contact details. For each role write what you "
            "achieved rather than what you did — with a number where you can.",
        ),
        "outcome": _i18n(
            "Tanlangan vakansiyaga moslangan toʻliq rezyume matni.",
            "Полный текст резюме под выбранную вакансию.",
            "A full CV, aimed at the vacancy you chose.",
        ),
        "criteria": [
            _c(
                "target",
                "Qaysi vakansiya uchun ekani aytilgan",
                "Указано, под какую вакансию написано",
                "It says which vacancy it is written for",
            ),
            _c(
                "sections",
                "Tanishtiruv, tajriba, taʼlim, koʻnikma va aloqa bor",
                "Есть представление, опыт, образование, навыки и контакты",
                "Introduction, experience, education, skills and contacts are all there",
            ),
            _c(
                "results",
                "Tajriba natija bilan yozilgan, vazifa roʻyxati emas",
                "Опыт описан через результат, а не список обязанностей",
                "Experience is written as results, not a list of duties",
            ),
        ],
    },
    {
        "slug": "mahalla-loyiha-taklifi",
        "program": "mahalla-tashabbusi",
        "level": L.ELEMENTARY,
        "minutes": 60,
        "kind": K.TEXT,
        "min_chars": 500,
        "skills": ["loyiha", "jamoa"],
        "title": _i18n(
            "Mahalla uchun loyiha taklifi",
            "Проектное предложение для махалли",
            "A project proposal for your mahalla",
        ),
        "summary": _i18n(
            "Mahallangizdagi bitta muammoni tanlang va uni hal qilish rejasini yozing.",
            "Выберите одну проблему вашей махалли и напишите план её решения.",
            "Pick one problem in your neighbourhood and write a plan to fix it.",
        ),
        "instructions": _i18n(
            "Mahallangizda haqiqatan mavjud bitta muammoni tanlang. Yozing: muammo nima "
            "va kimga taʼsir qiladi; qanday hal qilish mumkin; kim yordam berishi kerak; "
            "qancha vaqt va qancha mablagʻ kerak; natijani qanday oʻlchaysiz. "
            "Katta muammoni emas, bir necha oyda hal qilish mumkin boʻlganini tanlang.",
            "Выберите реально существующую проблему вашей махалли. Напишите: в чём "
            "проблема и на кого влияет; как её решить; кто должен помочь; сколько времени "
            "и средств нужно; как измерите результат. Берите не глобальную проблему, а "
            "ту, что решается за несколько месяцев.",
            "Pick a problem that actually exists where you live. Write: what the problem "
            "is and who it affects; how it could be solved; who would need to help; how "
            "long it would take and what it would cost; how you would measure the result. "
            "Choose something solvable in a few months, not something vast.",
        ),
        "outcome": _i18n(
            "Muammo, yechim, resurs va oʻlchov koʻrsatilgan taklif.",
            "Предложение с проблемой, решением, ресурсами и метрикой.",
            "A proposal naming the problem, the fix, the resources and the measure.",
        ),
        "criteria": [
            _c(
                "problem",
                "Muammo aniq va kimga taʼsir qilishi aytilgan",
                "Проблема конкретна и сказано, на кого влияет",
                "The problem is specific and it says who it affects",
            ),
            _c(
                "plan",
                "Yechim bosqichlari va kim yordam berishi yozilgan",
                "Описаны шаги решения и кто помогает",
                "The steps of the fix and who helps are written out",
            ),
            _c(
                "measure",
                "Natijani qanday oʻlchash koʻrsatilgan",
                "Указано, как измерить результат",
                "It says how the result will be measured",
            ),
        ],
    },
    {
        "slug": "ish-suhbati-javoblari",
        "program": "ish-suhbatiga-tayyorgarlik",
        "level": L.BEGINNER,
        "minutes": 40,
        "kind": K.FIELDS,
        "skills": ["suhbat", "rezyume"],
        "title": _i18n(
            "Ish suhbatining uchta savoli",
            "Три вопроса собеседования",
            "Three interview questions",
        ),
        "summary": _i18n(
            "Eng koʻp beriladigan uchta savolga oʻz javobingizni tayyorlang.",
            "Подготовьте свои ответы на три самых частых вопроса.",
            "Prepare your own answers to the three most common questions.",
        ),
        "instructions": _i18n(
            "Har bir javobni ovoz chiqarib aytib koʻring, soʻng yozing. Javob qisqa "
            "boʻlsin — bir daqiqadan oshmasin.",
            "Проговорите каждый ответ вслух, потом запишите. Ответ должен быть коротким — "
            "не длиннее минуты.",
            "Say each answer out loud first, then write it down. Keep each one short — no "
            "longer than a minute spoken.",
        ),
        "outcome": _i18n(
            "Uchta tayyor, qisqa javob.",
            "Три готовых коротких ответа.",
            "Three prepared, short answers.",
        ),
        "fields": [
            {
                "key": "about",
                "label_i18n": _i18n(
                    "Oʻzingiz haqingizda gapirib bering",
                    "Расскажите о себе",
                    "Tell me about yourself",
                ),
                "min_chars": 120,
            },
            {
                "key": "why",
                "label_i18n": _i18n(
                    "Nega aynan shu ishga?",
                    "Почему именно эта работа?",
                    "Why this job in particular?",
                ),
                "min_chars": 100,
            },
            {
                "key": "gap",
                "label_i18n": _i18n(
                    "Tajribangizdagi boʻshliqni qanday tushuntirasiz?",
                    "Как вы объясните перерыв в опыте?",
                    "How will you explain a gap in your experience?",
                ),
                "min_chars": 100,
            },
        ],
        "criteria": [
            _c(
                "concrete",
                "Javoblarda aniq misol yoki fakt bor",
                "В ответах есть конкретный пример или факт",
                "The answers contain a concrete example or fact",
            ),
            _c(
                "short",
                "Har bir javob qisqa va tushunarli",
                "Каждый ответ короткий и понятный",
                "Each answer is short and clear",
            ),
        ],
    },
]


async def load_tasks(session: AsyncSession) -> dict[str, int]:
    """Create or refresh the curated task catalogue. Never creates an attempt."""
    counts = {"created": 0, "updated": 0, "missing_program": 0}
    now = datetime.now(UTC)

    programs = {
        program.slug: program for program in (await session.execute(select(Program))).scalars()
    }

    for order, spec in enumerate(TASKS):
        program = programs.get(spec["program"])
        if program is None:
            counts["missing_program"] += 1
            logger.warning(
                "practical task %s: programme %s is not in the catalogue",
                spec["slug"],
                spec["program"],
            )

        task = await session.scalar(select(PracticalTask).where(PracticalTask.slug == spec["slug"]))
        if task is None:
            task = PracticalTask(slug=spec["slug"], published_at=now)
            session.add(task)
            counts["created"] += 1
        else:
            counts["updated"] += 1

        task.title_i18n = spec["title"]
        task.summary_i18n = spec["summary"]
        task.instructions_i18n = spec["instructions"]
        task.outcome_i18n = spec["outcome"]
        task.criteria = spec["criteria"]
        task.kind = spec["kind"]
        task.fields = spec.get("fields", [])
        task.min_chars = spec.get("min_chars")
        task.level = spec["level"]
        task.estimated_minutes = spec["minutes"]
        task.skills_practised = spec["skills"]
        task.program_id = program.id if program else None
        task.ai_reviewed = spec.get("ai_reviewed", True)
        task.order_index = order
        task.is_published = True
        if task.published_at is None:
            task.published_at = now

    await session.flush()
    return counts


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        counts = await load_tasks(session)
        await session.commit()

    logger.info("practical tasks ready: %s", counts)
    print(f"\n  {counts['created']} ta yangi amaliy topshiriq yaratildi.")
    print(f"  Yangilangan topshiriqlar: {counts['updated']}")
    print(f"  Katalogda topilmagan dasturlar: {counts['missing_program']}\n")


if __name__ == "__main__":
    asyncio.run(_main())
