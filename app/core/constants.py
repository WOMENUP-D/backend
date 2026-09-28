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
    """The assistant's surfaces.

    `coach` is the one that reads her whole WomanUP record — score, skills,
    courses, paths — and explains the step the deterministic engine already
    chose. The others answer a question; the coach answers *her*.
    """

    EDUCATION = "education"
    HEALTH = "health"
    DAILY = "daily"
    COACH = "coach"


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


class LessonKind(StrEnum):
    """What a lesson asks of her, which is what the card announces before she
    opens it: twenty minutes of reading and a practice task are different
    commitments."""

    VIDEO = "video"
    READING = "reading"
    PRACTICE = "practice"
    QUIZ = "quiz"
    PROJECT = "project"


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
    # Published on WomanUP by a member organisation. Applying shares her
    # profile with that organisation, inside the platform, under a consent
    # scoped to it — nothing is sent to an outside system.
    ORGANIZATION = "organization"


class OpportunityType(StrEnum):
    VACANCY = "vacancy"
    INTERNSHIP = "internship"
    GRANT = "grant"
    INVESTMENT = "investment"
    MENTORSHIP = "mentorship"
    MARKETPLACE = "marketplace"
    INTERNATIONAL_PROGRAM = "international_program"
    # Kinds an organisation can publish on WomanUP. No partner feed sends them.
    COMPETITION = "competition"
    TRAINING = "training"
    CONSULTATION = "consultation"
    # Events: something she attends at a time and a place (or online).
    WORKSHOP = "workshop"
    SEMINAR = "seminar"
    CONFERENCE = "conference"
    FORUM = "forum"
    NETWORKING = "networking"


#: Kinds that can be events. A listing is an event when it has a start time;
#: these five are only ever events, so they must have one. A competition, a
#: training or a consultation may be either — with a date it is an event, without
#: one it is a standing offer in the catalogue.
EVENT_ONLY_TYPES: frozenset[OpportunityType] = frozenset(
    {
        OpportunityType.WORKSHOP,
        OpportunityType.SEMINAR,
        OpportunityType.CONFERENCE,
        OpportunityType.FORUM,
        OpportunityType.NETWORKING,
    }
)
EVENT_TYPES: frozenset[OpportunityType] = EVENT_ONLY_TYPES | {
    OpportunityType.COMPETITION,
    OpportunityType.TRAINING,
    OpportunityType.CONSULTATION,
}


class EventFormat(StrEnum):
    """Where an event happens. Only what an organiser can say for certain."""

    ONLINE = "online"
    OFFLINE = "offline"


#: Listings that support a business of her own — what "For your business"
#: shows. A vacancy or an internship is work for somebody else.
BUSINESS_OPPORTUNITY_TYPES: frozenset[OpportunityType] = frozenset(
    {
        OpportunityType.GRANT,
        OpportunityType.INVESTMENT,
        OpportunityType.MENTORSHIP,
        OpportunityType.MARKETPLACE,
        OpportunityType.COMPETITION,
        OpportunityType.CONSULTATION,
    }
)


class OrganizationKind(StrEnum):
    """What an organisation on WomanUP is. Its members hold the existing roles:
    an employer's or an investor's people are PARTNER, an education
    provider's are TRAINER. No role is added for either."""

    EMPLOYER = "employer"
    EDUCATION_PROVIDER = "education_provider"
    INVESTOR = "investor"
    NGO = "ngo"
    GOVERNMENT = "government"


class OrgMemberRole(StrEnum):
    """What a member may do inside her organisation."""

    OWNER = "owner"  # edits the profile, manages listings and applications
    MEMBER = "member"  # manages listings and applications


class InvitationStatus(StrEnum):
    """An organisation's invitation to a candidate who opted into discovery."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class EligibilityStatus(StrEnum):
    """Whether she may apply to a listing, as the server can establish it.

    `unknown` is its own answer, never rounded up: a listing with an age rule
    and a woman whose age is not on record is not "eligible" — the platform
    simply cannot say, and the page tells her what would settle it.
    """

    ELIGIBLE = "eligible"
    UNKNOWN = "unknown"
    NOT_ELIGIBLE = "not_eligible"


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


class DimensionBand(StrEnum):
    """How a Development Score dimension reads, in words rather than a number.

    Thresholds live in `services.score_insights`. A band is a reading of her
    own score, not a verdict on her: it decides which dimension the cabinet
    opens on and which recommendations come first, and nothing else.
    """

    STRONG = "strong"
    DEVELOPING = "developing"
    FOCUS = "focus"


class NextStepKind(StrEnum):
    """What a recommended step asks her to do. The browser chooses the link."""

    TAKE_ASSESSMENT = "take_assessment"
    PLAN_ITEM = "plan_item"
    REVIEW_PLAN = "review_plan"
    CREATE_PLAN = "create_plan"
    CONTINUE_PROGRAM = "continue_program"
    START_PROGRAM = "start_program"
    # A route through several courses rather than one course. Offered when the
    # catalogue holds one for the area that needs her, because "where do I go
    # after this" is the question a single course cannot answer.
    CONTINUE_PATH = "continue_path"
    START_PATH = "start_path"
    # Work to do and be assessed on. Offered after the learning it applies —
    # practice is what you do with something you have been taught.
    PRACTISE_TASK = "practise_task"
    IMPROVE_TASK = "improve_task"
    # Record work she has already done. Last in the order on purpose: it never
    # displaces learning, it only shows up when there is something to show.
    ADD_PROJECT = "add_project"
    EXPLORE_OPPORTUNITIES = "explore_opportunities"


class RecommendationReason(StrEnum):
    """Why something is recommended.

    Sent as a key with the facts it quotes rather than as a sentence, so every
    suggestion carries an explanation (section 06) in whichever of the portal's
    languages she reads.
    """

    NO_ASSESSMENT = "no_assessment"
    IN_YOUR_PLAN = "in_your_plan"
    PLAN_AWAITING = "plan_awaiting"
    NO_PLAN = "no_plan"
    ENROLLED = "enrolled"
    IN_PROGRESS = "in_progress"
    FOCUS_DIMENSION = "focus_dimension"
    SKILLS_MATCH = "skills_match"
    # Somebody assessed her work and wrote down what to change. That is the
    # most concrete reason the platform can give for anything.
    NEEDS_IMPROVEMENT = "needs_improvement"
    # She has certificates or passed work, and nothing yet that describes what
    # she did with it.
    EVIDENCE_TO_SHOW = "evidence_to_show"
    # It teaches, practises or asks for a skill her chosen direction needs.
    CAREER_SKILL = "career_skill"


class AchievementType(StrEnum):
    """Something that actually happened, named by what backs it.

    Every type is a reading of a record the platform already holds — none is
    stored on its own. That is what keeps an achievement honest: it exists
    exactly as long as the enrollment, certificate, evaluation or confirmed
    outcome behind it does, and it cannot be awarded twice because its source
    cannot exist twice.
    """

    COURSE_COMPLETED = "course_completed"
    CERTIFICATE_EARNED = "certificate_earned"
    LEARNING_PATH_COMPLETED = "learning_path_completed"
    PRACTICAL_TASK_PASSED = "practical_task_passed"
    SKILL_VERIFIED = "skill_verified"
    # Her own record of work she did. Real, and hers — but nobody on the
    # platform has checked it, and it is labelled that way wherever it shows.
    PROJECT_COMPLETED = "project_completed"
    # Confirmed by a partner platform, never self-reported.
    WORK_EXPERIENCE = "work_experience"
    BUSINESS_MILESTONE = "business_milestone"


class PortfolioSection(StrEnum):
    """The parts of a portfolio she can choose to show publicly."""

    SKILLS = "skills"
    CERTIFICATES = "certificates"
    ACHIEVEMENTS = "achievements"
    PRACTICE = "practice"


class CareerCategory(StrEnum):
    """What kind of working life a career path prepares her for.

    Two, because that is the choice a woman actually makes first — working for
    somebody or working for herself — and every direction the catalogue holds
    falls on one side of it.
    """

    EMPLOYMENT = "employment"
    OWN_BUSINESS = "own_business"


class JourneyStage(StrEnum):
    """The stages a career path is read in, in the order she walks them.

    "You are here" — her skills against the ones the work asks for — comes
    before them and is not a stage: it is where she starts, not something to do.
    """

    LEARN = "learn"  # courses that teach what she is missing
    PRACTICE = "practice"  # practical tasks that exercise it
    BUILD = "build"  # evidence she can show: certificates, tasks, projects
    EXPLORE = "explore"  # open listings that ask for these skills


class StageStatus(StrEnum):
    """Where she stands on one stage — derived from her records, never ticked."""

    DONE = "done"
    IN_PROGRESS = "in_progress"
    TODO = "todo"
    # The catalogue holds nothing for this stage right now, or it is not open to
    # her yet. Carries a reason so the page can say which, and why.
    UNAVAILABLE = "unavailable"


class TaskSubmissionKind(StrEnum):
    """What a practical task asks her to hand in.

    Three forms, chosen because they are what the platform can actually accept
    today: there is object-storage *configuration* but no storage service, and
    a file upload that cannot store a file is worse than one that was never
    offered. A task that genuinely needs a document asks for a link to it.
    """

    TEXT = "text"  # one written answer
    LINK = "link"  # a URL to work hosted elsewhere
    FIELDS = "fields"  # several named answers, each with its own prompt


class TaskStatus(StrEnum):
    """Where one attempt at a practical task stands.

    `submitted` means handed in and waiting — never "passed". The platform says
    she passed only once an evaluation is recorded, because telling her
    otherwise would be the one thing a skills system must not do.
    """

    STARTED = "started"
    SUBMITTED = "submitted"
    PASSED = "passed"
    NEEDS_IMPROVEMENT = "needs_improvement"


class EvaluatorKind(StrEnum):
    """Who assessed an attempt.

    This decides what the evidence is worth, which is why it is recorded rather
    than inferred. A platform assessment says she can apply what she learned; a
    mentor or a partner organisation putting their name to it is a different
    and stronger claim. See `services.practice.EVALUATOR_EVIDENCE`.
    """

    AI = "ai"
    TRAINER = "trainer"
    MENTOR = "mentor"
    PARTNER = "partner"


class SkillCategory(StrEnum):
    """Where a skill belongs in the catalogue. Broad on purpose: the list of
    skills will grow into the hundreds, and a reader scans categories."""

    DIGITAL = "digital"
    PROFESSIONAL = "professional"
    BUSINESS = "business"
    FINANCE = "finance"
    LANGUAGE = "language"
    COMMUNICATION = "communication"
    CRAFT = "craft"
    WELLBEING = "wellbeing"
    CIVIC = "civic"


class ProficiencyLevel(StrEnum):
    """How far along a skill is.

    `beginner`, `intermediate` and `advanced` are the words the learning
    section already uses for course level (`lms.level.*` in the web app), kept
    so one vocabulary covers a course and the skill it teaches. The two extra
    steps give an assessment room to say more than "somewhere in the middle".
    """

    BEGINNER = "beginner"
    ELEMENTARY = "elementary"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class SkillStatus(StrEnum):
    """How well the platform knows she has a skill.

    The distinction the whole system turns on: **finishing a course is not a
    verified skill**. It is evidence that she was taught it. Verification means
    somebody outside the platform — a mentor, an employer, a real placement —
    put their name to it.
    """

    SELF_REPORTED = "self_reported"  # she listed it herself
    LEARNED = "learned"  # a course, a path or a certificate
    ASSESSED = "assessed"  # an assessment scored it
    VERIFIED = "verified"  # a person or a result outside the platform confirmed it


class EvidenceKind(StrEnum):
    """What backs a skill up.

    New kinds are added as the platform grows (practical tasks, employer
    reviews); each is mapped to the status it can support in
    `services.skills`, which is the only place that decides what a piece of
    evidence is worth.
    """

    SELF_REPORTED = "self_reported"
    COURSE_COMPLETION = "course_completion"
    LEARNING_PATH = "learning_path"
    CERTIFICATE = "certificate"
    PRACTICAL_PROJECT = "practical_project"
    AI_ASSESSMENT = "ai_assessment"
    FORMAL_ASSESSMENT = "formal_assessment"
    MENTOR_ASSESSMENT = "mentor_assessment"
    EMPLOYER_ASSESSMENT = "employer_assessment"
    INTERNSHIP = "internship"
    JOB_OUTCOME = "job_outcome"


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
    # Making her portfolio readable by anyone with its link. Recorded on every
    # change like any other consent, because publishing her record is a
    # data-sharing event even when nobody is paying for it.
    PUBLIC_PORTFOLIO = "public_portfolio"
    # Sharing her profile with one organisation on WomanUP — given when she
    # applies to its listing or accepts its invitation. Scoped: the consent
    # row names the organisation in `subject_ref`, and one organisation's
    # consent never opens her profile to another.
    SHARE_WITH_EMPLOYER = "share_with_employer"
    # Letting organisations find her, anonymously, by her skills. Opt-in and
    # off by default; they see no name, no contact and no id until she
    # accepts an invitation.
    CANDIDATE_DISCOVERY = "candidate_discovery"


#: Scopes that are always given for one subject (an organisation), never in
#: general. The generic consent endpoint refuses them.
SUBJECT_SCOPES: frozenset[ConsentScope] = frozenset({ConsentScope.SHARE_WITH_EMPLOYER})


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
    # A reminder she asked for, about one event. Its words are built from the
    # event record, not from a template.
    EVENT_REMINDER = "event_reminder"


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
