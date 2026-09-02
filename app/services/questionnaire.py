"""The learning-profile questionnaire.

The Development Score asks how a woman is doing across eight dimensions of
life. This asks a different question — what she wants to learn and where she is
starting from — and the two are not interchangeable: the score drives her plan
and the national KPIs, this drives what the assistant recommends and how hard
the level test should be.

Kept as data rather than as a table so the wording, the order and the options
can change without a migration; the answers are stored against the question ids
below, and `VERSION` is stamped on every completed run so an old answer set is
still readable after the wording moves on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

VERSION = 2


class QuestionType(StrEnum):
    SINGLE = "single"  # one option
    MULTI = "multi"  # several options
    SCALE = "scale"  # 1-10
    TEXT = "text"  # free text, short


def _q(
    qid: str,
    section: str,
    qtype: QuestionType,
    uz: str,
    ru: str,
    en: str,
    *,
    options: list[dict[str, Any]] | None = None,
    required: bool = True,
    placeholder: tuple[str, str, str] | None = None,
    suggestions: list[tuple[str, str, str]] | None = None,
    suggest_mode: str = "set",
) -> dict[str, Any]:
    question: dict[str, Any] = {
        "id": qid,
        "section": section,
        "type": qtype.value,
        "text_i18n": {"uz": uz, "ru": ru, "en": en},
        "required": required,
    }
    if options:
        question["options"] = options
    if placeholder:
        question["placeholder_i18n"] = {
            "uz": placeholder[0],
            "ru": placeholder[1],
            "en": placeholder[2],
        }
    if suggestions:
        # Ready answers under an open field. A blank box in front of a woman
        # who has never written a CV is where a questionnaire gets abandoned;
        # a tap she can then edit is not.
        question["suggestions_i18n"] = [
            {"uz": uz_s, "ru": ru_s, "en": en_s} for uz_s, ru_s, en_s in suggestions
        ]
        # "set" replaces the field (one answer); "add" appends to a
        # comma-separated list (several).
        question["suggest_mode"] = suggest_mode
    return question


def _o(value: str, uz: str, ru: str, en: str) -> dict[str, Any]:
    return {"value": value, "label_i18n": {"uz": uz, "ru": ru, "en": en}}


SECTIONS: list[dict[str, Any]] = [
    {
        "id": "about",
        "title_i18n": {"uz": "Siz haqingizda", "ru": "О вас", "en": "About you"},
    },
    {"id": "goals", "title_i18n": {"uz": "Maqsadlar", "ru": "Цели", "en": "Goals"}},
    {
        "id": "interests",
        "title_i18n": {"uz": "Qiziqishlar", "ru": "Интересы", "en": "Interests"},
    },
    {
        "id": "skills",
        "title_i18n": {
            "uz": "Koʻnikma va bilimlar",
            "ru": "Навыки и знания",
            "en": "Skills and knowledge",
        },
    },
    {
        "id": "format",
        "title_i18n": {
            "uz": "Oʻqish formati",
            "ru": "Формат обучения",
            "en": "Study format",
        },
    },
]


QUESTIONS: list[dict[str, Any]] = [
    # --- 1. About her -----------------------------------------------------
    _q(
        "occupation",
        "about",
        # A woman on maternity leave is very often also studying and also
        # looking for work. Forcing one answer made her delete two true ones.
        QuestionType.MULTI,
        "Hozir nima bilan bandsiz?",
        "Чем вы сейчас занимаетесь?",
        "What are you doing at the moment?",
        options=[
            _o("studying", "Oʻqiyapman", "Учусь", "Studying"),
            _o("working", "Ishlayapman", "Работаю", "Working"),
            _o("own_business", "Oʻz ishim bor", "У меня своё дело", "I run my own business"),
            _o("looking", "Ish qidiryapman", "Ищу работу", "Looking for work"),
            _o("home", "Uy yumushlari bilan", "Занимаюсь домом", "At home"),
            _o("maternity", "Bola parvarishidaman", "В декрете", "On maternity leave"),
        ],
    ),
    _q(
        "education",
        "about",
        # "Currently studying" is a status, not a level: someone with a
        # bachelor's reading for a master's had to hide one of the two.
        QuestionType.MULTI,
        "Maʼlumotingiz qanday?",
        "Какое у вас образование?",
        "What is your education?",
        options=[
            _o("school", "Oʻrta", "Среднее", "School"),
            _o("college", "Oʻrta maxsus", "Среднее специальное", "College"),
            _o("bachelor", "Oliy (bakalavr)", "Высшее (бакалавр)", "Bachelor's"),
            _o("master", "Oliy (magistr)", "Высшее (магистр)", "Master's"),
            _o("studying_now", "Hozir oʻqiyapman", "Учусь сейчас", "Currently studying"),
        ],
    ),
    _q(
        "field",
        "about",
        QuestionType.TEXT,
        "Qaysi sohada oʻqiyapsiz yoki ishlayapsiz?",
        "В какой сфере вы сейчас учитесь или работаете?",
        "What field are you studying or working in?",
        required=False,
        placeholder=(
            "Masalan: buxgalteriya, tibbiyot, savdo",
            "Например: бухгалтерия, медицина, торговля",
            "For example: accounting, medicine, retail",
        ),
        suggestions=[
            ("Taʼlim", "Образование", "Education"),
            ("Sogʻliqni saqlash", "Здравоохранение", "Healthcare"),
            ("Savdo", "Торговля", "Retail"),
            ("IT", "IT", "IT"),
            ("Buxgalteriya", "Бухгалтерия", "Accounting"),
            ("Tikuvchilik", "Швейное дело", "Sewing"),
            ("Davlat xizmati", "Госслужба", "Public service"),
            ("Hozircha ishlamayman", "Пока не работаю", "Not working yet"),
        ],
    ),
    _q(
        "experience",
        "about",
        QuestionType.SINGLE,
        "Bu sohada tajribangiz qancha?",
        "Какой у вас опыт в этой сфере?",
        "How much experience do you have in it?",
        options=[
            _o("none", "Tajriba yoʻq", "Опыта нет", "None"),
            _o("under_1", "1 yilgacha", "До 1 года", "Under a year"),
            _o("1_3", "1-3 yil", "1–3 года", "1–3 years"),
            _o("3_5", "3-5 yil", "3–5 лет", "3–5 years"),
            _o("over_5", "5 yildan koʻp", "Более 5 лет", "Over 5 years"),
        ],
    ),
    _q(
        "hours_per_week",
        "about",
        QuestionType.SINGLE,
        "Haftasiga oʻqishga qancha vaqt ajrata olasiz?",
        "Сколько времени в неделю вы готовы уделять обучению?",
        "How much time a week can you give to studying?",
        options=[
            _o("under_3", "3 soatgacha", "До 3 часов", "Up to 3 hours"),
            _o("3_5", "3-5 soat", "3–5 часов", "3–5 hours"),
            _o("5_10", "5-10 soat", "5–10 часов", "5–10 hours"),
            _o("10_20", "10-20 soat", "10–20 часов", "10–20 hours"),
            _o("over_20", "20 soatdan koʻp", "Более 20 часов", "Over 20 hours"),
        ],
    ),
    # --- 2. Goals ---------------------------------------------------------
    _q(
        "goal",
        "goals",
        # These outcomes overlap by nature — a new job *is* the higher income.
        QuestionType.MULTI,
        "Oʻqishdan qanday natija kutyapsiz?",
        "Какой результат вы хотите получить от обучения?",
        "What result do you want from studying?",
        options=[
            _o("find_job", "Ish topish", "Найти работу", "Find a job"),
            _o("change_career", "Kasbni oʻzgartirish", "Сменить профессию", "Change profession"),
            _o("more_income", "Daromadni oshirish", "Повысить доход", "Increase my income"),
            _o("promotion", "Lavozimda koʻtarilish", "Получить повышение", "Get a promotion"),
            _o(
                "own_business",
                "Oʻz ishimni boshlash",
                "Начать свой бизнес",
                "Start my own business",
            ),
            _o(
                "grow_skills",
                "Bor koʻnikmalarni rivojlantirish",
                "Развить имеющиеся навыки",
                "Develop existing skills",
            ),
            _o("for_myself", "Oʻzim uchun", "Для себя", "For myself"),
        ],
    ),
    _q(
        "target_field",
        "goals",
        QuestionType.TEXT,
        "Qaysi kasb yoki sohani oʻrganmoqchisiz?",
        "Какую профессию или сферу вы хотели бы изучить?",
        "What profession or field would you like to learn?",
        placeholder=(
            "Masalan: dasturlash, dizayn, tikuvchilik",
            "Например: программирование, дизайн, шитьё",
            "For example: programming, design, sewing",
        ),
        suggestions=[
            ("IT va dasturlash", "IT и программирование", "IT and programming"),
            ("Dizayn", "Дизайн", "Design"),
            ("Marketing va SMM", "Маркетинг и SMM", "Marketing and SMM"),
            ("Moliya va buxgalteriya", "Финансы и бухгалтерия", "Finance and accounting"),
            ("Hunarmandchilik", "Ремёсла", "Crafts"),
            ("Taʼlim va pedagogika", "Образование и педагогика", "Education"),
            ("Sogʻliq", "Здоровье", "Health"),
            ("Tadbirkorlik", "Предпринимательство", "Entrepreneurship"),
            ("Chet tillari", "Иностранные языки", "Languages"),
            ("Boshqaruv", "Управление", "Management"),
        ],
    ),
    _q(
        "goal_3_6",
        "goals",
        QuestionType.TEXT,
        "Yaqin 3-6 oyda qanday maqsadga erishmoqchisiz?",
        "Какой цели вы хотите достичь в ближайшие 3–6 месяцев?",
        "What do you want to achieve in the next 3–6 months?",
        required=False,
        placeholder=(
            "Bir jumlada yozing",
            "Опишите одним предложением",
            "In one sentence",
        ),
        suggestions=[
            ("Birinchi ishimni topish", "Найти первую работу", "Find my first job"),
            ("Portfolio yigʻish", "Собрать портфолио", "Build a portfolio"),
            ("Birinchi buyurtmani olish", "Получить первый заказ", "Land my first order"),
            ("Doimiy daromadga chiqish", "Выйти на стабильный доход", "Reach a steady income"),
            (
                "Ingliz tilini B1 darajaga yetkazish",
                "Подтянуть английский до B1",
                "Get my English to B1",
            ),
            ("Oʻz ishimni boshlash", "Запустить своё дело", "Start my own business"),
        ],
    ),
    _q(
        "speed",
        "goals",
        QuestionType.SCALE,
        "Tez natija siz uchun qanchalik muhim?",
        "Насколько для вас важен быстрый результат?",
        "How important is a fast result to you?",
    ),
    # --- 3. Interests -----------------------------------------------------
    # "Which areas interest you?" used to live here. It asked the same thing as
    # "What would you like to learn?" two questions earlier, so its list of
    # areas became the tappable suggestions there and the question itself went.
    _q(
        "task_style",
        "interests",
        QuestionType.MULTI,
        "Qanday vazifalar sizga koʻproq yoqadi?",
        "Какие задачи вам больше нравятся?",
        "Which kinds of task do you enjoy most?",
        options=[
            _o(
                "analyse",
                "Maʼlumotni tahlil qilish",
                "Анализировать информацию",
                "Analysing information",
            ),
            _o("people", "Odamlar bilan ishlash", "Работать с людьми", "Working with people"),
            _o(
                "create", "Yangi narsa yaratish", "Создавать что-то новое", "Creating something new"
            ),
            _o("logic", "Mantiqiy masalalar", "Решать логические задачи", "Solving logic problems"),
            _o(
                "organise",
                "Odamlar va jarayonlarni tashkil qilish",
                "Организовывать людей и процессы",
                "Organising people and processes",
            ),
            _o("numbers", "Raqamlar bilan ishlash", "Работать с цифрами", "Working with numbers"),
            _o(
                "tech",
                "Texnologiyalar bilan ishlash",
                "Работать с технологиями",
                "Working with technology",
            ),
            _o("creative", "Ijodiy vazifalar", "Творческие задачи", "Creative work"),
        ],
    ),
    # --- 4. Skills and knowledge -----------------------------------------
    _q(
        "current_skills",
        "skills",
        QuestionType.TEXT,
        "Qanday kasbiy koʻnikmalaringiz bor?",
        "Какие профессиональные навыки у вас уже есть?",
        "What professional skills do you already have?",
        required=False,
        placeholder=(
            "Vergul bilan ajrating",
            "Перечислите через запятую",
            "Separate with commas",
        ),
        suggest_mode="add",
        suggestions=[
            ("Mijozlar bilan ishlash", "Работа с клиентами", "Working with clients"),
            ("Sotuv", "Продажи", "Sales"),
            ("Oʻqituvchilik", "Преподавание", "Teaching"),
            ("Tadbir tashkil qilish", "Организация мероприятий", "Organising events"),
            ("Matn yozish", "Написание текстов", "Writing"),
            ("Ijtimoiy tarmoqlar", "Соцсети", "Social media"),
            ("Hisob-kitob", "Работа с цифрами", "Working with numbers"),
            ("Tikuvchilik", "Шитьё", "Sewing"),
            ("Pazandachilik", "Кулинария", "Cooking"),
        ],
    ),
    _q(
        "tools",
        "skills",
        QuestionType.TEXT,
        "Qanday dastur yoki vositalardan foydalana olasiz?",
        "Какие программы или инструменты вы умеете использовать?",
        "Which programs or tools can you use?",
        required=False,
        placeholder=("Excel, 1C, Canva…", "Excel, 1C, Canva…", "Excel, 1C, Canva…"),
        suggest_mode="add",
        suggestions=[
            ("Word", "Word", "Word"),
            ("Excel", "Excel", "Excel"),
            ("PowerPoint", "PowerPoint", "PowerPoint"),
            ("Google Docs", "Google Docs", "Google Docs"),
            ("Telegram", "Telegram", "Telegram"),
            ("Instagram", "Instagram", "Instagram"),
            ("Canva", "Canva", "Canva"),
            ("1C", "1C", "1C"),
            ("Photoshop", "Photoshop", "Photoshop"),
            ("Hech qanday", "Никакие", "None"),
        ],
    ),
    _q(
        "self_level",
        "skills",
        QuestionType.SCALE,
        "Tanlagan sohangizdagi bilimingizni qanday baholaysiz?",
        "Как бы вы оценили свой уровень знаний в выбранной сфере?",
        "How would you rate your knowledge in your chosen field?",
    ),
    _q(
        "studied",
        "skills",
        QuestionType.TEXT,
        "Qanday mavzularni allaqachon oʻrgangansiz?",
        "Какие темы вы уже изучали?",
        "Which topics have you already studied?",
        required=False,
        suggest_mode="add",
        suggestions=[
            ("Kompyuter savodxonligi", "Компьютерная грамотность", "Computer literacy"),
            ("Ingliz tili", "Английский язык", "English"),
            ("Buxgalteriya asoslari", "Основы бухгалтерии", "Accounting basics"),
            ("Canvada dizayn", "Дизайн в Canva", "Design in Canva"),
            ("Dasturlash asoslari", "Основы программирования", "Programming basics"),
            ("SMM", "SMM", "SMM"),
            ("Hozircha hech narsa", "Пока ничего", "Nothing yet"),
        ],
    ),
    _q(
        "hardest",
        "skills",
        QuestionType.TEXT,
        "Hozir sizga eng qiyini nima?",
        "Что вам сейчас кажется самым сложным?",
        "What feels hardest to you right now?",
        required=False,
        suggestions=[
            ("Vaqt yetishmaydi", "Не хватает времени", "I do not have enough time"),
            (
                "Nimadan boshlashni bilmayman",
                "Не знаю, с чего начать",
                "I do not know where to start",
            ),
            ("Ingliz tili", "Английский язык", "English"),
            ("Oʻzimga ishonchim yoʻq", "Не хватает уверенности в себе", "I lack confidence"),
            ("Maslahat beradigan odam yoʻq", "Не с кем посоветоваться", "I have nobody to ask"),
            ("Texnik mavzular", "Технические темы", "Technical subjects"),
            ("Oila bilan birga ulgurish", "Совмещать с семьёй", "Fitting it around my family"),
        ],
    ),
    _q(
        "practice",
        "skills",
        QuestionType.SINGLE,
        "Amaliy tajriba yoki loyihalaringiz bormi?",
        "Есть ли у вас практический опыт или проекты?",
        "Do you have practical experience or projects?",
        options=[
            _o("none", "Yoʻq", "Нет", "No"),
            _o("study", "Faqat oʻquv loyihalari", "Только учебные проекты", "Study projects only"),
            _o(
                "some",
                "Bir nechta amaliy ish",
                "Несколько практических работ",
                "A few practical pieces",
            ),
            _o(
                "commercial",
                "Tijoriy tajriba bor",
                "Есть коммерческий опыт",
                "Commercial experience",
            ),
        ],
    ),
    # --- 5. Format --------------------------------------------------------
    _q(
        "language",
        "format",
        # Most of the country is comfortable in more than one; accepting only
        # one narrowed the catalogue for no reason.
        QuestionType.MULTI,
        "Qaysi tilda oʻqish qulay?",
        "На каком языке вам удобнее учиться?",
        "Which language do you prefer to study in?",
        options=[
            _o("uz", "Oʻzbek", "Узбекский", "Uzbek"),
            _o("ru", "Rus", "Русский", "Russian"),
            _o("en", "Ingliz", "Английский", "English"),
        ],
    ),
]


def required_ids() -> set[str]:
    return {q["id"] for q in QUESTIONS if q["required"]}


def payload() -> dict[str, Any]:
    """What the client renders."""
    return {"version": VERSION, "sections": SECTIONS, "questions": QUESTIONS}
