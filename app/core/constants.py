"""Domain vocabularies shared across models, schemas and services.

These enums encode the taxonomies fixed by the technical specification
(roles, score dimensions, regions, programme categories). Persisted values are
the string members — renaming one is a migration, not a refactor.
"""

from datetime import date
from enum import StrEnum


class AgeBand(StrEnum):
    """Which health and guidance content a user may be shown.

    The portal serves girls from 10 upwards, so this is a safety boundary, not
    a personalisation nicety: adult reproductive content must never reach a
    child. UNKNOWN is deliberately *not* a synonym for adult — when we do not
    know the age we assume the more protective band.
    """

    CHILD = "child"  # 10-12
    TEEN = "teen"  # 13-17
    ADULT = "adult"  # 18+
    UNKNOWN = "unknown"  # age not given — treated as TEEN for anything sensitive


class AssistantSection(StrEnum):
    """The assistant's three surfaces."""

    EDUCATION = "education"
    HEALTH = "health"
    DAILY = "daily"


class Role(StrEnum):
    """RBAC roles, section 02 of the specification."""

    USER = "user"  # ayol / qiz foydalanuvchi
    MOTHER = "mother"  # ona
    MENTOR = "mentor"
    TRAINER = "trainer"  # trener / kontent muallifi
    REGIONAL_COORDINATOR = "regional_coordinator"
    ADMIN = "admin"  # respublika administrator
    PARTNER = "partner"  # hamkor tashkilot
    MODERATOR = "moderator"


class Permission(StrEnum):
    """Granular permissions: view / create / edit / approve / export / delete / assign / publish."""

    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    APPROVE = "approve"
    EXPORT = "export"
    DELETE = "delete"
    ASSIGN = "assign"
    PUBLISH = "publish"


# Role -> permissions granted on the resources that role owns.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.USER: frozenset({Permission.VIEW, Permission.CREATE, Permission.EDIT}),
    Role.MOTHER: frozenset({Permission.VIEW, Permission.CREATE, Permission.EDIT}),
    Role.MENTOR: frozenset({Permission.VIEW, Permission.CREATE, Permission.EDIT}),
    Role.TRAINER: frozenset(
        {Permission.VIEW, Permission.CREATE, Permission.EDIT, Permission.PUBLISH}
    ),
    Role.REGIONAL_COORDINATOR: frozenset({Permission.VIEW, Permission.EXPORT, Permission.ASSIGN}),
    Role.PARTNER: frozenset({Permission.VIEW, Permission.CREATE, Permission.EDIT}),
    Role.MODERATOR: frozenset(
        {Permission.VIEW, Permission.EDIT, Permission.APPROVE, Permission.DELETE}
    ),
    Role.ADMIN: frozenset(Permission),
}


class UserStatus(StrEnum):
    PENDING = "pending"  # registered, onboarding unfinished
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"  # soft delete; PII anonymised


class Language(StrEnum):
    UZ = "uz"
    RU = "ru"
    EN = "en"


# The portal has no content below this age; the account is not for them. The
# upper bound only rejects a typo — a birth year of 1890 is a slipped digit, not
# a centenarian signing up.
#
# They live here rather than in `services.age_gate` so a Pydantic schema can
# import them without a schema depending on a service: nothing under
# `app/schemas` imports `app/services` today, and `age_gate` itself imports
# `app.models.profile`, so reaching for it from a schema would invert the
# layering for two integers.
MIN_SUPPORTED_AGE = 10
MAX_SUPPORTED_AGE = 100


def years_between(born: date, today: date) -> int:
    """Whole years from `born` to `today`, never negative.

    The one place this arithmetic is written. It looks trivial and is not: the
    month/day comparison is what stops someone born in December being counted a
    year older for the eleven months before her birthday, which on this portal
    is the difference between a teenager and an adult at the health gate.
    """
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return max(0, years)


def normalise_language(value: str | None) -> str | None:
    """A locale tag from a client, reduced to a language the portal writes in.

    The browser has four locales but only three written languages: `uz-Cyrl` is
    Uzbek in another script, transliterated in the browser, so it folds to `uz`
    rather than becoming a fourth prompt language.

    Returns `None` for anything unrecognised so the caller falls through to the
    stored language instead of silently writing Uzbek — "she asked for this" and
    "we defaulted" are different facts, and only one of them is her choice.
    """
    if not value:
        return None
    # Only the primary subtag decides the language: `uz-Cyrl` and `uz-Latn` are
    # both Uzbek, `ru-RU` is Russian. Matching on a prefix instead would fold
    # any string merely beginning with "uz" into Uzbek.
    head = value.strip().lower().split("-", 1)[0]
    return head if head in {member.value for member in Language} else None


class Region(StrEnum):
    """14 hudud — Uzbekistan regions used for coverage KPI."""

    TASHKENT_CITY = "tashkent_city"
    TASHKENT_REGION = "tashkent_region"
    ANDIJAN = "andijan"
    BUKHARA = "bukhara"
    FERGANA = "fergana"
    JIZZAKH = "jizzakh"
    KARAKALPAKSTAN = "karakalpakstan"
    KASHKADARYA = "kashkadarya"
    KHOREZM = "khorezm"
    NAMANGAN = "namangan"
    NAVOI = "navoi"
    SAMARKAND = "samarkand"
    SIRDARYA = "sirdarya"
    SURKHANDARYA = "surkhandarya"


class ScoreDimension(StrEnum):
    """The 8 dimensions of the WomanUP Development Score (0-100 composite)."""

    EDUCATION_SKILLS = "education_skills"
    EMPLOYMENT = "employment"
    ENTREPRENEURSHIP = "entrepreneurship"
    FINANCIAL_LITERACY = "financial_literacy"
    HEALTHY_LIFESTYLE = "healthy_lifestyle"
    FAMILY_PARENTING = "family_parenting"
    SOCIAL_ACTIVITY = "social_activity"
    INTERNATIONAL_INTEGRATION = "international_integration"


class ProgramCategory(StrEnum):
    """The 12 core development programmes, section 04."""

    VOCATIONAL_SKILLS = "vocational_skills"
    ETHICS_CULTURE = "ethics_culture"
    HEALTH = "health"
    INTERNATIONAL = "international"
    PARENTING = "parenting"
    FINANCIAL_LITERACY = "financial_literacy"
    ENTREPRENEURSHIP = "entrepreneurship"
    LEADERSHIP = "leadership"
    LEGAL_LITERACY = "legal_literacy"
    DIGITAL_SAFETY = "digital_safety"
    MENTORSHIP_NETWORKING = "mentorship_networking"
    VOLUNTEERING = "volunteering"


class NewsCategory(StrEnum):
    """Sections of the news and announcements feed.

    The feed is the first screen a woman sees after she registers, so the
    vocabulary is deliberately about *her world* rather than about the portal:
    what medicine now knows, what women discovered, what opened this week.
    """

    HEALTH = "health"  # sogʻliq — everyday wellbeing
    MEDICINE = "medicine"  # tibbiyot — screening, prevention, care
    SCIENCE = "science"  # ayollar ilm-fanda — discoveries and research
    EDUCATION = "education"
    CAREER = "career"  # work, entrepreneurship, money
    SUCCESS_STORY = "success_story"
    ANNOUNCEMENT = "announcement"  # eʼlonlar — deadlines, openings, events


class AgeGroup(StrEnum):
    """The six brackets the feed personalises against.

    Distinct from `AgeBand`, deliberately. `AgeBand` is the *safety* boundary:
    four coarse values, one of them UNKNOWN, deciding whether adult
    reproductive content may be shown at all. `AgeGroup` is an *editorial*
    one — it withholds nothing, it only decides what a woman of that age most
    likely came for. A thirty-year-old and a fifty-year-old are both ADULT to
    the gate and are looking for different articles.

    The values are the bracket itself because people read them: they appear in
    the API, on the news-preferences screen, and as the keys of the
    age-relevance map stored on every post.
    """

    TEEN = "13-17"
    YOUNG = "18-24"
    EARLY_ADULT = "25-34"
    MID_ADULT = "35-44"
    MATURE = "45-54"
    SENIOR = "55+"


class NewsTopic(StrEnum):
    """Subjects a reader may subscribe to on the news-preferences screen.

    Broader than `NewsCategory`: a category is where an editor files a post, a
    topic is what a reader says she is interested in. "Women in STEM" is not a
    section of the feed, it is a reason to open one.
    """

    SCIENCE = "science"
    MEDICINE = "medicine"
    TECHNOLOGY = "technology"
    CAREER = "career"
    EDUCATION = "education"
    WOMEN_IN_STEM = "women_in_stem"
    MENTAL_HEALTH = "mental_health"
    REPRODUCTIVE_HEALTH = "reproductive_health"
    PREVENTION = "prevention"
    NUTRITION = "nutrition"
    MENOPAUSE = "menopause"
    FAMILY = "family"


class MedicalTopic(StrEnum):
    """Women's-health subtopics — the axis age relevance actually turns on.

    A single `health` category cannot separate an article about adolescent
    periods from one about osteoporosis, and those two sit at opposite ends of
    the age range. Each subtopic carries a per-age-group baseline in
    `services.news_age`; the topics themselves are either detected from the
    text of a post or supplied by the AI news editor.
    """

    # --- reproductive health ---
    MENSTRUAL_HEALTH = "menstrual_health"
    PMS = "pms"
    PCOS = "pcos"
    FERTILITY = "fertility"
    CONTRACEPTION = "contraception"
    PREGNANCY = "pregnancy"
    POSTPARTUM = "postpartum"
    # --- women's health ---
    BREAST_HEALTH = "breast_health"
    CERVICAL_HEALTH = "cervical_health"
    HORMONAL_HEALTH = "hormonal_health"
    CARDIOVASCULAR = "cardiovascular"
    MENTAL_HEALTH = "mental_health"
    NUTRITION = "nutrition"
    PREVENTION = "prevention"
    # --- age-related ---
    ADOLESCENT_HEALTH = "adolescent_health"
    PERIMENOPAUSE = "perimenopause"
    MENOPAUSE = "menopause"
    POSTMENOPAUSE = "postmenopause"
    OSTEOPOROSIS = "osteoporosis"
    AGE_RELATED_DISEASE = "age_related_disease"


class ProgramFormat(StrEnum):
    VIDEO = "video"
    TEXT = "text"
    AUDIO = "audio"
    LIVE = "live"
    OFFLINE = "offline"
    BLENDED = "blended"


class EnrollmentStatus(StrEnum):
    ENROLLED = "enrolled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DROPPED = "dropped"


class OpportunitySource(StrEnum):
    """Which external platform an opportunity came from."""

    EDU_JOB = "edu_job"
    INVEST_HUB = "invest_hub"
    COMMERCE = "commerce"
    INTERNAL = "internal"


class OpportunityType(StrEnum):
    VACANCY = "vacancy"
    INTERNSHIP = "internship"
    GRANT = "grant"
    INVESTMENT = "investment"
    MENTORSHIP = "mentorship"
    MARKETPLACE = "marketplace"
    INTERNATIONAL_PROGRAM = "international_program"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class GoalHorizon(StrEnum):
    """Goal horizons offered in the personal cabinet: 3 / 6 / 12 / 36 months."""

    M3 = "3m"
    M6 = "6m"
    M12 = "12m"
    M36 = "36m"


class PlanItemStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: The edition of the privacy policy currently published at /maxfiylik.
#:
#: Consent is recorded against a version, because "she agreed" is only a
#: meaningful record if it says *what* she agreed to. Bump this whenever the
#: published text changes in substance, and existing consents stop counting as
#: consent to the new text — which is the point.
PRIVACY_POLICY_VERSION = "1.0"


class ConsentScope(StrEnum):
    """Each data-sharing destination the user consents to separately."""

    TERMS_OF_USE = "terms_of_use"
    PRIVACY_POLICY = "privacy_policy"
    PROFILE_ANALYTICS = "profile_analytics"
    SHARE_EDU_JOB = "share_edu_job"
    SHARE_INVEST_HUB = "share_invest_hub"
    SHARE_COMMERCE = "share_commerce"
    AI_PERSONALISATION = "ai_personalisation"
    MARKETING_COMMUNICATION = "marketing_communication"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


class NotificationTrigger(StrEnum):
    """Engagement triggers listed in section 11."""

    ONBOARDING_INCOMPLETE = "onboarding_incomplete"
    COURSE_START = "course_start"
    DEADLINE = "deadline"
    SKILL_GAP = "skill_gap"
    PROGRESS = "progress"
    INACTIVITY = "inactivity"


class DataClassification(StrEnum):
    """Section 08 data tiers — drives access scope and retention."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class IntegrationSystem(StrEnum):
    EDU_JOB = "edu_job"
    INVEST_HUB = "invest_hub"
    COMMERCE = "commerce"


class IntegrationEventStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class RiskFlagType(StrEnum):
    """AI risk flags. They notify a coordinator; they never trigger sanctions."""

    INACTIVITY = "inactivity"
    DROPOUT_RISK = "dropout_risk"
    STALLED_PLAN = "stalled_plan"
    SAFETY_ESCALATION = "safety_escalation"


# Score dimensions are weighted into the 0-100 composite. Weights sum to 1.0.
SCORE_WEIGHTS: dict[ScoreDimension, float] = {
    ScoreDimension.EDUCATION_SKILLS: 0.18,
    ScoreDimension.EMPLOYMENT: 0.16,
    ScoreDimension.ENTREPRENEURSHIP: 0.14,
    ScoreDimension.FINANCIAL_LITERACY: 0.12,
    ScoreDimension.HEALTHY_LIFESTYLE: 0.12,
    ScoreDimension.FAMILY_PARENTING: 0.12,
    ScoreDimension.SOCIAL_ACTIVITY: 0.08,
    ScoreDimension.INTERNATIONAL_INTEGRATION: 0.08,
}
