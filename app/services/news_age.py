"""Age relevance for the news feed — which article is *for* which reader.

The feed already has a safety boundary: `age_gate` decides whether adult
reproductive content may be shown at all, and `is_adult_only` withholds a post
from a minor and from a reader whose age we do not know. That boundary is not
what this module is about, and this module never widens or narrows it.

This is the editorial question underneath it. A fifteen-year-old and a
fifty-five-year-old are both allowed to read an article about osteoporosis;
only one of them came for it. So every post carries an `age_relevance` map —
one score from 0 to 100 per `AgeGroup` — and the personalised feed ranks by it.

Three rules hold the design together.

* **Ranking, never blocking.** A low score moves a post down the "For you"
  section. It never removes it from the feed, from a category, or from search:
  `list_news` is untouched by anything here. What a woman is *allowed* to read
  is `age_gate`'s decision and stays there.
* **Importance outranks demographics.** A treatment for breast cancer matters
  to women of every age, so a post that reads as a genuine breakthrough gets a
  floor under its scores (`_importance_floor`) and cannot be buried by a
  baseline. Section 05 of the brief asks for exactly this.
* **The table is a starting point.** `MEDICAL_BASELINE` encodes the priority
  table from the specification, and the AI editor may override it per post
  (`news_ai`). When the model is unavailable — or has never looked at a post —
  the derivation here stands on its own, which is the portal's usual
  degrade-don't-fail rule.
"""

from __future__ import annotations

from datetime import date

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory
from app.models.profile import Profile
from app.services.age_gate import age_from_profile, fold

# Ordered youngest to oldest. The order is meaningful: it is the column order
# of the priority table below and the order the preferences screen renders.
AGE_GROUPS: tuple[AgeGroup, ...] = (
    AgeGroup.TEEN,
    AgeGroup.YOUNG,
    AgeGroup.EARLY_ADULT,
    AgeGroup.MID_ADULT,
    AgeGroup.MATURE,
    AgeGroup.SENIOR,
)

# The specification states its priorities in words. These are the numbers
# behind them, so "High" means the same thing in every row of the table.
VERY_LOW, LOW, MEDIUM, HIGH, VERY_HIGH = 10, 30, 55, 80, 95


def group_for_age(age: int | None) -> AgeGroup | None:
    """The bracket an age falls in, or None when we do not know the age.

    Girls from ten may hold an account, and the youngest bracket the
    specification defines starts at thirteen. A ten-to-twelve-year-old is
    ranked as TEEN rather than given her own bracket: the difference between
    what interests an eleven- and a fourteen-year-old is not something this
    feed can tell, and the content she must not see is withheld by `age_gate`
    regardless of what happens here.

    None is *not* coerced to a bracket. An unknown age means the personalised
    feed ranks on importance alone, which is the honest answer — guessing a
    bracket would silently push articles at a reader on the strength of a
    number nobody supplied.
    """
    if age is None:
        return None
    if age < 18:
        return AgeGroup.TEEN
    if age < 25:
        return AgeGroup.YOUNG
    if age < 35:
        return AgeGroup.EARLY_ADULT
    if age < 45:
        return AgeGroup.MID_ADULT
    if age < 55:
        return AgeGroup.MATURE
    return AgeGroup.SENIOR


def group_for_profile(profile: Profile | None, *, today: date | None = None) -> AgeGroup | None:
    """Her bracket, from the birth date where there is one.

    `age_from_profile` already prefers `birth_date` over the stated
    `age_group` string, which is what the brief asks for: a date of birth
    stays true without her editing anything, a typed-in age does not.
    """
    return group_for_age(age_from_profile(profile, today=today))


# --- the priority table --------------------------------------------------
#
# Section 11 of the brief, as numbers. Columns are AGE_GROUPS in order:
# 13-17, 18-24, 25-34, 35-44, 45-54, 55+.
#
# The rows the specification does not name are filled in the same spirit and
# marked; they exist because the subtopic vocabulary in section 04 is wider
# than the table, and a detected topic with no baseline would silently fall
# back to the category default.
MEDICAL_BASELINE: dict[MedicalTopic, tuple[int, int, int, int, int, int]] = {
    # --- rows stated in the specification ---
    MedicalTopic.MENSTRUAL_HEALTH: (HIGH, HIGH, HIGH, MEDIUM, LOW, LOW),
    MedicalTopic.PCOS: (HIGH, HIGH, HIGH, MEDIUM, LOW, LOW),
    MedicalTopic.PREGNANCY: (LOW, MEDIUM, HIGH, MEDIUM, LOW, LOW),
    MedicalTopic.FERTILITY: (LOW, MEDIUM, HIGH, HIGH, MEDIUM, LOW),
    MedicalTopic.BREAST_HEALTH: (MEDIUM, MEDIUM, HIGH, HIGH, HIGH, HIGH),
    MedicalTopic.MENOPAUSE: (VERY_LOW, LOW, MEDIUM, HIGH, VERY_HIGH, HIGH),
    MedicalTopic.POSTMENOPAUSE: (VERY_LOW, VERY_LOW, LOW, MEDIUM, HIGH, VERY_HIGH),
    MedicalTopic.OSTEOPOROSIS: (LOW, LOW, LOW, MEDIUM, HIGH, VERY_HIGH),
    MedicalTopic.CARDIOVASCULAR: (LOW, MEDIUM, MEDIUM, HIGH, HIGH, VERY_HIGH),
    MedicalTopic.ADOLESCENT_HEALTH: (VERY_HIGH, HIGH, MEDIUM, LOW, LOW, LOW),
    # --- extended in the same spirit ---
    # PMS travels with the cycle, so it follows menstrual health exactly.
    MedicalTopic.PMS: (HIGH, HIGH, HIGH, MEDIUM, LOW, LOW),
    # Contraception is adult care. The low teen figure is editorial ranking
    # only — whether a minor may open such a post at all is `is_adult_only`.
    MedicalTopic.CONTRACEPTION: (VERY_LOW, MEDIUM, HIGH, HIGH, MEDIUM, VERY_LOW),
    MedicalTopic.POSTPARTUM: (VERY_LOW, MEDIUM, HIGH, MEDIUM, LOW, LOW),
    # Two audiences, one topic: HPV vaccination is a teenager's subject and
    # screening an adult's, which is why the teen figure is not the lowest.
    MedicalTopic.CERVICAL_HEALTH: (MEDIUM, HIGH, HIGH, HIGH, HIGH, MEDIUM),
    MedicalTopic.HORMONAL_HEALTH: (MEDIUM, MEDIUM, HIGH, HIGH, HIGH, MEDIUM),
    # Mental health is the least age-bound subject in the list.
    MedicalTopic.MENTAL_HEALTH: (HIGH, VERY_HIGH, HIGH, HIGH, HIGH, MEDIUM),
    MedicalTopic.NUTRITION: (HIGH, HIGH, HIGH, HIGH, HIGH, HIGH),
    MedicalTopic.PREVENTION: (MEDIUM, MEDIUM, HIGH, HIGH, VERY_HIGH, VERY_HIGH),
    MedicalTopic.PERIMENOPAUSE: (VERY_LOW, VERY_LOW, LOW, VERY_HIGH, HIGH, MEDIUM),
    MedicalTopic.AGE_RELATED_DISEASE: (VERY_LOW, LOW, LOW, MEDIUM, HIGH, VERY_HIGH),
}

# What a post is worth to each bracket when no medical subtopic is detected.
# A discovery or an award is interesting at any age — section 03 of the brief
# scores "a woman scientist wins an international prize" flat across the range
# — while a grant deadline or a first-job article is not equally addressed to
# a fourteen-year-old and a fifty-year-old.
CATEGORY_BASELINE: dict[NewsCategory, tuple[int, int, int, int, int, int]] = {
    NewsCategory.SCIENCE: (88, 88, 88, 88, 88, 88),
    NewsCategory.SUCCESS_STORY: (85, 85, 85, 85, 85, 85),
    NewsCategory.EDUCATION: (90, 92, 85, 75, 65, 55),
    NewsCategory.CAREER: (60, 85, 92, 88, 80, 65),
    NewsCategory.ANNOUNCEMENT: (70, 85, 90, 85, 75, 65),
    NewsCategory.HEALTH: (80, 80, 80, 80, 80, 80),
    NewsCategory.MEDICINE: (80, 80, 80, 80, 80, 80),
}

# Subjects that qualify an article without defining who it is for.
#
# Almost every health post is also about prevention, or nutrition, or living
# well, and those three have deliberately flat or rising curves. Taking the
# highest baseline across *all* detected topics therefore let them swamp the
# specific one: "folic acid in pregnancy" came out as prevention — 95 for a
# sixty-year-old — because prevention outscores pregnancy at that end.
#
# So they are held back. When an article carries a subject that genuinely
# names an audience, that subject decides the curve and these only fill in
# where nothing else did.
BROAD_TOPICS: frozenset[MedicalTopic] = frozenset(
    {
        MedicalTopic.PREVENTION,
        MedicalTopic.NUTRITION,
        MedicalTopic.MENTAL_HEALTH,
    }
)

# The sections where a health subtopic is what the post is *about*. Elsewhere
# — a discovery, a career piece, an announcement — it is usually a mention.
HEALTH_CATEGORIES: frozenset[NewsCategory] = frozenset({NewsCategory.HEALTH, NewsCategory.MEDICINE})

# Keywords that identify a subtopic, in the three languages the portal
# publishes in. Written folded (lowercase, apostrophes stripped) to match
# `fold` output, and kept to stems so that Russian and Uzbek inflection does
# not need a row of its own: "menopauza", "menopauzaning", "менопаузы" all
# match "menopauz".
TOPIC_KEYWORDS: dict[MedicalTopic, tuple[str, ...]] = {
    MedicalTopic.MENSTRUAL_HEALTH: (
        "hayz",
        "menstural",
        "menstrual",
        "menstruatsiya",
        "menstruatsiy",
        "менструа",
        "месячны",
        "menstruation",
        "period pain",
        "menstrual health",
    ),
    MedicalTopic.PMS: ("pms", "premenstrual", "предменструальн"),
    MedicalTopic.PCOS: (
        "pcos",
        "polikistoz",
        "поликистоз",
        "spkya",
        "polycystic",
    ),
    MedicalTopic.FERTILITY: (
        "bepushtlik",
        "farzand korish",
        "reproduktiv qobiliyat",
        "бесплоди",
        "фертильн",
        "экстракорпоральн",
        "fertilit",
        "infertilit",
        "ivf",
        "conceive",
    ),
    MedicalTopic.CONTRACEPTION: (
        "kontratsep",
        "контрацеп",
        "contracepti",
        "birth control",
    ),
    MedicalTopic.PREGNANCY: (
        "homilador",
        "homiladorlik",
        "tugruq",
        "беремен",
        "роды",
        "родах",
        "pregnan",
        "childbirth",
        "prenatal",
    ),
    MedicalTopic.POSTPARTUM: (
        "tugruqdan keyin",
        "emizish",
        "kokrak suti",
        "послеродов",
        "грудное вскармливание",
        "лактаци",
        "postpartum",
        "breastfeed",
        "postnatal",
    ),
    MedicalTopic.BREAST_HEALTH: (
        "kokrak bezi",
        "sut bezi",
        "mammograf",
        "молочной железы",
        "маммограф",
        "рак груди",
        "breast",
        "mammogra",
    ),
    MedicalTopic.CERVICAL_HEALTH: (
        "bachadon boyni",
        "hpv",
        "vpch",
        "шейки матки",
        "впч",
        "cervical",
        "cervix",
        "pap smear",
    ),
    MedicalTopic.HORMONAL_HEALTH: (
        "gormon",
        "qalqonsimon",
        "гормон",
        "щитовидн",
        "hormon",
        "thyroid",
        "endocrin",
    ),
    MedicalTopic.CARDIOVASCULAR: (
        "yurak",
        "qon bosim",
        "qon tomir",
        "insult",
        "сердц",
        "сердечн",
        "давление",
        "инсульт",
        "сосуд",
        "cardiovascular",
        "heart disease",
        "blood pressure",
        "stroke",
    ),
    MedicalTopic.MENTAL_HEALTH: (
        "ruhiy salomatlik",
        "depressiya",
        "xavotir",
        "stress",
        "charchoq",
        "uyqu",
        "психическ",
        "ментальн",
        "депресс",
        "тревожн",
        "выгорани",
        "mental health",
        "depression",
        "anxiety",
        "burnout",
        "sleep",
    ),
    MedicalTopic.NUTRITION: (
        "ovqatlanish",
        "kamqonlik",
        "temir tanqisligi",
        "vitamin",
        "питани",
        "анеми",
        "железодефицит",
        "nutrition",
        "diet",
        "anaemia",
        "anemia",
        "iron deficiency",
    ),
    MedicalTopic.PREVENTION: (
        "profilaktika",
        "skrining",
        "erta aniqlash",
        "emlash",
        "vaksina",
        "профилактик",
        "скрининг",
        "раннее выявление",
        "вакцин",
        "prevention",
        "screening",
        "vaccin",
        "early detection",
    ),
    MedicalTopic.ADOLESCENT_HEALTH: (
        "osmir",
        "balogat",
        "oquvchi qizlar",
        "подрост",
        "пубертат",
        "взрослени",
        "школьниц",
        "adolescen",
        "teenage",
        "puberty",
    ),
    MedicalTopic.PERIMENOPAUSE: (
        "perimenopauza",
        "menopauza oldi",
        "перименопауз",
        "перед менопауз",
        "perimenopaus",
    ),
    MedicalTopic.MENOPAUSE: (
        "menopauza",
        "klimaks",
        "менопауз",
        "климакс",
        "menopaus",
        "hot flash",
    ),
    MedicalTopic.POSTMENOPAUSE: (
        "postmenopauza",
        "menopauzadan keyin",
        "постменопауз",
        "после менопауз",
        "postmenopaus",
    ),
    MedicalTopic.OSTEOPOROSIS: (
        "osteoporoz",
        "suyak zichligi",
        "остеопороз",
        "плотность кости",
        "osteoporo",
        "bone density",
    ),
    MedicalTopic.AGE_RELATED_DISEASE: (
        "yoshga bogliq",
        "qarish",
        "demensiya",
        "alsgeymer",
        "возрастн",
        "старени",
        "деменци",
        "альцгеймер",
        "age-related",
        "ageing",
        "aging",
        "dementia",
        "alzheimer",
    ),
}

# A post that reads as a genuine advance is worth putting in front of every
# reader, whatever the demographics say — section 05 of the brief. These lift
# a floor under the scores rather than raise them all: a menopause study still
# ranks highest for the women it concerns, it simply stops being invisible to
# everyone else.
#
# Deliberately narrow. "A new treatment" and "a new drug" were here first and
# had to go: almost every medical headline says one of them, so the floor
# applied to nearly everything and flattened the age signal this module exists
# to produce. What is left is the vocabulary of an actual landmark — a
# discovery, a first, a Nobel, a WHO position.
IMPORTANCE_MARKERS: tuple[str, ...] = (
    # --- uz ---
    "kashfiyot",
    "birinchi marta",
    "nobel",
    "jahon sogliqni saqlash",
    # --- ru ---
    "открыти",
    "прорыв",
    "впервые",
    "нобелев",
    "всемирная организация здравоохранения",
    # --- en ---
    "breakthrough",
    "discover",
    "first time",
    "world health organization",
    "landmark study",
)

# How far an important post is lifted. Deliberately below the "High" band: it
# is a floor that keeps an article visible, not a promotion over the readers
# whose subject it actually is.
IMPORTANCE_FLOOR = 60


def detect_medical_topics(text: str) -> list[MedicalTopic]:
    """Which women's-health subtopics a piece of text is about.

    Substring matching on folded stems rather than tokenising: the feed
    publishes in Uzbek, Russian and English at once, and the three inflect too
    differently for a shared tokeniser to be worth its weight here.

    Returned in enum order so the result is stable — it is stored on the post
    and compared in tests.
    """
    folded = fold(text)
    return [
        topic
        for topic in MedicalTopic
        if any(keyword in folded for keyword in TOPIC_KEYWORDS.get(topic, ()))
    ]


def _importance_floor(text: str) -> int:
    """The floor a major advance puts under every bracket's score."""
    folded = fold(text)
    return IMPORTANCE_FLOOR if any(marker in folded for marker in IMPORTANCE_MARKERS) else 0


def searchable_text(
    title_i18n: dict | None,
    summary_i18n: dict | None,
    tags: list[str] | None = None,
) -> str:
    """Everything about a post worth matching keywords against, in one string.

    All locales together, on purpose: a post whose Uzbek summary says
    "menopauza" and whose Russian one says "климакс" is the same post, and
    detection should not depend on which translation an editor filled in first.
    """
    parts: list[str] = []
    for field in (title_i18n, summary_i18n):
        if field:
            parts.extend(str(value) for value in field.values() if value)
    parts.extend(tags or [])
    return " ".join(parts)


def derive_age_relevance(
    *,
    category: NewsCategory,
    title_i18n: dict | None = None,
    summary_i18n: dict | None = None,
    tags: list[str] | None = None,
    topics: list[MedicalTopic] | None = None,
) -> dict[str, int]:
    """The rule-based age-relevance map for a post.

    This is the fallback path and the default one: it runs whenever the AI
    editor has not scored a post, and whenever the model is unavailable. The
    portal's rule is that AI absence degrades quality, never function.

    A post carrying several *audience-defining* subtopics takes the highest
    baseline in each bracket rather than the average: an article covering both
    adolescent periods and perimenopause genuinely addresses both audiences,
    and averaging would leave it addressed to neither.

    The broad subjects in `BROAD_TOPICS` are excluded from that maximum
    whenever a specific one is present — see the note there for the article
    that made this necessary.
    """
    text = searchable_text(title_i18n, summary_i18n, tags)
    found = topics if topics is not None else detect_medical_topics(text)

    known = [topic for topic in found if topic in MEDICAL_BASELINE]
    specific = [topic for topic in known if topic not in BROAD_TOPICS]

    # A story filed under Science that happens to mention vaccination is a
    # science story, not a prevention advisory: the Nobel for mRNA was landing
    # on the prevention curve and losing half its score for a teenager. So a
    # broad subject on its own does not overrule where the editor filed the
    # post — only a subject that names an audience does, and inside Health and
    # Medicine, where "prevention" is the actual subject rather than a
    # passing mention.
    if not specific and known and category not in HEALTH_CATEGORIES:
        known = []

    rows = [MEDICAL_BASELINE[topic] for topic in (specific or known)]
    if rows:
        scores = [max(column) for column in zip(*rows, strict=True)]
    else:
        scores = list(CATEGORY_BASELINE.get(category, (80, 80, 80, 80, 80, 80)))

    floor = _importance_floor(text)
    return {
        group.value: max(floor, min(100, score))
        for group, score in zip(AGE_GROUPS, scores, strict=True)
    }


def relevance_for(age_relevance: dict | None, group: AgeGroup | None, *, default: int = 70) -> int:
    """One bracket's score out of a stored map.

    `default` is what an unranked post — or a reader whose age we do not know —
    is worth. Neutral on purpose: a post nobody has scored should neither be
    promoted over a scored one nor buried beneath it.
    """
    if group is None or not age_relevance:
        return default
    value = age_relevance.get(group.value)
    if not isinstance(value, int | float):
        return default
    return max(0, min(100, int(value)))
