"""Courses for what she wants, chosen from what she has — the diagnostic's recommendation.

The version-3 diagnostic ends with a profile: her situation, her goals (the
main one first), the fields that draw her, how far she already is in them,
what stands in her way and how she likes to learn. This module turns that
profile into courses she can actually take, in four steps that stay
inspectable:

1. **Needs.** Each goal, interest and difficulty names the taxonomy skills
   it calls for (`GOAL_SKILLS`, `INTEREST_SKILLS`, `DIFFICULTY_SKILLS`); a
   weak Development Score dimension adds the skills the taxonomy files under
   it. Every need carries a weight and the reason it is there.
2. **Gaps.** What she holds — skills on record, plus a field she rated at
   work level or above — is subtracted. What remains is the gap.
3. **Candidates.** The catalogue is searched for published courses tagged
   with any spelling of a gap skill (the taxonomy knows "ingliz tili",
   "английский язык" and "english" as one), fit for her age and region, not
   already finished. They are ranked by how much of the gap they close,
   weighted by why the gap matters, then by language, level fit, length
   against the hours she has, and whether the course is ours.
4. **Words.** The model explains the top candidates and lays them out as a
   roadmap — but it can only point at a candidate by number, never name a
   course of its own, and every word it writes is checked against the
   schema. Off or unavailable, the rules write the explanation themselves
   from the course's own description and the gap it closes.

Nothing here invents a course or a claim: the outcome text is the course's
description or outcomes, and a course that teaches none of her gaps is not
offered to fill a slot. When the profile says too little — no interests, no
usable goal — the answer is a follow-up question, not a generic list.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import operators

from app.core.constants import Language, ProficiencyLevel, ScoreDimension
from app.models.assessment import Assessment
from app.models.audit import AiInteraction
from app.models.profile import Profile
from app.models.program import Enrollment, Program
from app.models.skill import Skill
from app.models.user import User
from app.services import catalogue_rules
from app.services import recommendation as engine
from app.services import skills as skill_service
from app.services.age_gate import age_from_profile
from app.services.diagnostic import GOAL_LEGACY
from app.services.llm_gateway import LlmGateway, LlmUnavailableError, llm_gateway

logger = logging.getLogger(__name__)

FEATURE = "diagnostic_courses"
PROMPT_VERSION = "course-match-1"
RECOMMENDATION_VERSION = 1
MAX_CANDIDATES = 24
MAX_COURSES = 8
#: The model explains the best of them; the rest keep the rules' words.
MODEL_COURSES = 6
MODEL_MAX_TOKENS = 4000
#: How many times the worker lets the model fail on one attempt.
MODEL_TRIES = 2
CANDIDATE_POOL = 400

# ---------------------------------------------------------------------------
# What a goal, an interest or a difficulty asks for — taxonomy slugs
# ---------------------------------------------------------------------------

GOAL_SKILLS: dict[str, tuple[str, ...]] = {
    "first_job": ("cv-writing", "job-interviews", "job-search", "communication", "portfolio"),
    "change_direction": ("job-search", "cv-writing", "portfolio"),
    "promotion": (
        "leadership",
        "project-management",
        "communication",
        "presentation",
        "negotiation",
    ),
    "improve_skills": (),
    "start_business": (
        "entrepreneurship",
        "business-plan",
        "marketing",
        "financial-planning",
        "sales",
        "pricing",
    ),
    "grow_business": ("marketing", "sales", "financial-model", "ecommerce", "smm", "hr"),
    "new_profession": ("job-search", "portfolio"),
    "financial_independence": (
        "financial-literacy",
        "budgeting",
        "savings",
        "investing",
        "entrepreneurship",
    ),
    "communication_leadership": (
        "communication",
        "public-speaking",
        "leadership",
        "negotiation",
        "teamwork",
    ),
    "international": ("english", "ielts", "motivation-letter", "communication"),
    "academic": ("english", "statistics", "data-analysis", "presentation"),
    "discover": (
        "computer-literacy",
        "financial-literacy",
        "communication",
        "cv-writing",
        "job-search",
    ),
}
#: Goals whose direction is the field she is drawn to: there, her interests
#: carry the goal's own weight.
DIRECTION_GOALS = {"change_direction", "new_profession", "improve_skills", "discover", "academic"}
INTEREST_SKILLS: dict[str, tuple[str, ...]] = {
    "it_programming": ("programming", "html-css", "javascript", "sql", "git", "django"),
    "ai": ("machine-learning", "data-analysis", "programming", "ai-tools"),
    "data": ("data-analysis", "sql", "excel", "statistics"),
    "business": ("entrepreneurship", "business-plan", "marketing", "project-management"),
    "marketing_smm": ("marketing", "smm", "content-marketing", "copywriting", "branding"),
    "design": ("design", "canva", "photoshop"),
    "finance_economics": (
        "financial-literacy",
        "accounting",
        "tax",
        "investing",
        "financial-planning",
    ),
    "management": ("project-management", "leadership", "teamwork", "hr"),
    "languages": ("english", "russian", "ielts"),
    "education": ("teaching", "psychology", "presentation"),
    "science": ("statistics", "data-analysis", "english"),
}
DIFFICULTY_SKILLS: dict[str, tuple[str, ...]] = {
    "cv": ("cv-writing", "job-interviews"),
    "confidence": ("public-speaking", "communication"),
    "finding_opportunities": ("job-search",),
    "lack_experience": ("portfolio",),
}
BUSINESS_CHALLENGE_SKILLS: dict[str, tuple[str, ...]] = {
    "clients_marketing": ("marketing", "smm", "sales"),
    "finance_accounting": ("accounting", "financial-model", "tax"),
    "legal": ("contracts", "labour-law"),
    "team": ("hr", "leadership", "teamwork"),
    "online_sales": ("ecommerce", "marketplaces", "product-listing"),
    "scaling": ("financial-model", "project-management"),
}
#: A field she rated at work level or above (3 of 0–4) counts as held.
FIELD_SKILLS: dict[str, tuple[str, ...]] = {
    "it": ("programming", "html-css", "javascript", "sql", "git"),
    "marketing": ("marketing", "smm", "sales", "content-marketing"),
    "design": ("design", "canva", "photoshop"),
    "english": ("english",),
    "finance": ("financial-literacy", "accounting", "budgeting"),
}
#: Which self-rated field a skill belongs to, for the level check.
SKILL_FIELD: dict[str, str] = {
    slug: field_name for field_name, slugs in FIELD_SKILLS.items() for slug in slugs
}
HELD_LEVEL = 3
WEIGHT = {"goal": 3.0, "goal_other": 2.0, "interest": 2.0, "difficulty": 2.0, "dimension": 1.0}
#: A goal's skills are listed core first: the first two carry full weight.
GOAL_POSITION_WEIGHT = (3.0, 3.0, 2.5, 2.5, 2.0, 2.0)
#: A course closing a main-goal skill of this weight or more is a high priority.
HIGH_FROM = 2.5
#: What a weak dimension asks for: its foundations, not every skill filed under it.
DIMENSION_FOUNDATIONS: dict[ScoreDimension, tuple[str, ...]] = {
    ScoreDimension.EMPLOYMENT: ("cv-writing", "job-search", "job-interviews", "communication"),
    ScoreDimension.DIGITAL_SKILLS: ("computer-literacy", "digital-literacy", "excel", "ai-tools"),
    ScoreDimension.FINANCIAL_LITERACY: ("financial-literacy", "budgeting", "savings"),
    ScoreDimension.ENTREPRENEURSHIP: ("entrepreneurship", "business-plan", "marketing"),
    ScoreDimension.LEADERSHIP: ("leadership", "communication", "public-speaking", "teamwork"),
    ScoreDimension.EDUCATION_SKILLS: ("english", "presentation", "computer-literacy"),
    ScoreDimension.HEALTHY_LIFESTYLE: ("health", "nutrition", "stress-management"),
    ScoreDimension.FAMILY_PARENTING: ("parenting", "psychology", "legal-literacy"),
}
#: Resolved tags outside her needs, her holdings and her fields count against a course.
OFF_TOPIC_CAP = 3
#: Hours a week → the longest course that still fits a few months.
HOURS_CAP = {"lt2": 20, "h2_5": 50, "h5_10": 120, "h10_plus": None}
SCORE_FOCUS_BELOW = 50


@dataclass(slots=True)
class Need:
    slug: str
    weight: float
    why: str  # goal | goal_other | interest | difficulty | dimension
    source: str  # the goal / interest / difficulty / dimension it came from


@dataclass(slots=True)
class Candidate:
    program: Program
    covers: list[Need]
    score: float
    index: int = 0


@dataclass(slots=True)
class Learner:
    user_id: uuid.UUID
    profile: dict
    language: Language | None
    held: set[str]
    skill_levels: dict[str, int]
    weak_dimensions: list[ScoreDimension]
    finished: set[uuid.UUID]
    started: set[uuid.UUID]
    age: int | None
    region: str | None
    goal_text: str = ""
    needs: list[Need] = field(default_factory=list)
    gaps: list[Need] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Steps 1 and 2: needs and gaps
# ---------------------------------------------------------------------------


def needs_for(profile: dict, weak: list[ScoreDimension]) -> list[Need]:
    """Every skill her profile calls for, with why. One entry per skill, the
    strongest reason kept."""
    found: dict[str, Need] = {}

    def add(slug: str, why: str, source: str, weight: float | None = None) -> None:
        weight = WEIGHT[why] if weight is None else weight
        current = found.get(slug)
        if current is None or weight > current.weight:
            found[slug] = Need(slug, weight, why, source)

    goals = list(profile.get("goals") or [])
    wish = profile.get("career_wish")
    if wish == "own_business" and "start_business" not in goals:
        goals.append("start_business")
    if wish == "change_field" and "change_direction" not in goals:
        goals.append("change_direction")
    if profile.get("business_stage") == "idea":
        add("business-plan", "goal_other", "business_stage")
    for position, goal in enumerate(goals):
        for rank, slug in enumerate(GOAL_SKILLS.get(goal, ())):
            graded = GOAL_POSITION_WEIGHT[min(rank, len(GOAL_POSITION_WEIGHT) - 1)]
            if position == 0:
                add(slug, "goal", goal, graded)
            else:
                add(slug, "goal_other", goal, min(graded, WEIGHT["goal_other"]))
    # Where the goal is "the field I am drawn to", the field is the goal.
    interest_weight = (
        WEIGHT["goal"] if goals and goals[0] in DIRECTION_GOALS else WEIGHT["interest"]
    )
    for interest in profile.get("interests") or []:
        for slug in INTEREST_SKILLS.get(interest, ()):
            add(slug, "interest", interest, interest_weight)
    for difficulty in profile.get("difficulties") or []:
        for slug in DIFFICULTY_SKILLS.get(difficulty, ()):
            add(slug, "difficulty", difficulty)
    for challenge in profile.get("business_challenges") or []:
        for slug in BUSINESS_CHALLENGE_SKILLS.get(challenge, ()):
            add(slug, "difficulty", challenge)
    for dimension in weak:
        for slug in DIMENSION_FOUNDATIONS.get(dimension, ()):
            add(slug, "dimension", dimension.value)
    return sorted(found.values(), key=lambda n: (-n.weight, n.slug))


def held_from(profile: dict, recorded: set[str]) -> set[str]:
    held = set(recorded)
    for field_name, level in (profile.get("skill_levels") or {}).items():
        if int(level) >= HELD_LEVEL:
            held.update(FIELD_SKILLS.get(field_name, ()))
    return held


def gaps_for(needs: list[Need], held: set[str]) -> list[Need]:
    return [need for need in needs if need.slug not in held]


def fields_of(profile: dict) -> set[str]:
    """Every skill in a field she chose or rated: a tag there is not off-topic."""
    out: set[str] = set()
    for interest in profile.get("interests") or []:
        out.update(INTEREST_SKILLS.get(interest, ()))
    for field_name, level in (profile.get("skill_levels") or {}).items():
        if int(level) >= 1:
            out.update(FIELD_SKILLS.get(field_name, ()))
    for goal in profile.get("goals") or []:
        out.update(GOAL_SKILLS.get(goal, ()))
    return out


# ---------------------------------------------------------------------------
# Step 3: candidates
# ---------------------------------------------------------------------------


def _pattern(label: str) -> str:
    return label.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def spellings_for(session: AsyncSession, slugs: list[str]) -> dict[str, list[str]]:
    """Every tag that names each skill: its slug, names and aliases."""
    if not slugs:
        return {}
    rows = await session.execute(
        select(Skill).where(Skill.slug.in_(slugs), Skill.is_active.is_(True))
    )
    out: dict[str, list[str]] = {}
    for skill in rows.scalars():
        words = {skill.slug, *(str(n) for n in skill.name_i18n.values()), *skill.aliases}
        out[skill.slug] = sorted(w for w in words if w and w.strip())
    return out


def level_band(learner: Learner, need: Need) -> int | None:
    """Her self-rated level in the field a skill belongs to, 0–4, or None."""
    field_name = SKILL_FIELD.get(need.slug)
    if field_name is None:
        return None
    level = learner.skill_levels.get(field_name)
    return int(level) if level is not None else None


def level_fits(program: Program, band: int | None) -> int:
    """+1 when the course level suits her, -3 when it plainly does not, 0 unknown."""
    if program.level is None or band is None:
        return 0
    advanced = program.level in (ProficiencyLevel.ADVANCED, ProficiencyLevel.EXPERT)
    starter = program.level in (ProficiencyLevel.BEGINNER, ProficiencyLevel.ELEMENTARY)
    if advanced and band <= 1:
        return -3
    if starter and band >= 4:
        return -3
    if (starter and band <= 1) or (program.level == ProficiencyLevel.INTERMEDIATE and band == 2):
        return 1
    return 0


def length_fits(program: Program, hours: str | None) -> int:
    cap = HOURS_CAP.get(hours or "")
    if cap is None or program.duration_hours is None:
        return 0
    return 1 if program.duration_hours <= cap else -1


def score(program: Program, covers: list[Need], off_topic: int, learner: Learner) -> float:
    """How well one course serves her: what it closes (the two strongest needs
    in full, the rest by half), discounted when it is mostly about other
    things, then her language, its level and length, and whether it is ours."""
    weights = sorted((need.weight for need in covers), reverse=True)
    coverage = sum(weights[:2]) + 0.5 * sum(weights[2:])
    matched = len(covers)
    precision = (matched + 1) / (matched + min(off_topic, OFF_TOPIC_CAP) + 1)
    value = coverage * precision
    value += {0: 2.0, 1: 1.0, 2: 0.0}[engine.language_distance(program, learner.language)]
    value += sum(level_fits(program, level_band(learner, need)) for need in covers[:1])
    value += length_fits(program, learner.profile.get("hours"))
    if program.external_url is None:
        value += 1.0  # ours: lessons, progress and a certificate she can show
    if program.id in learner.started:
        value += 1.5  # a course she has already opened comes before a new one
    return round(value, 2)


def spread(
    candidates: list[Candidate], gaps: list[Need], per_skill: int = 2, limit: int = MAX_COURSES
) -> list[Candidate]:
    """The courses the rules offer: at most two for the same first skill, so
    eight English courses do not crowd out the one on interviews; the rest
    of the places go to the next best."""
    taken: dict[str, int] = {}
    chosen: list[Candidate] = []
    for candidate in candidates:
        slug = candidate.covers[0].slug
        if taken.get(slug, 0) >= per_skill:
            continue
        taken[slug] = taken.get(slug, 0) + 1
        chosen.append(candidate)
        if len(chosen) >= limit:
            return chosen
    for candidate in candidates:
        if len(chosen) >= limit:
            break
        if candidate not in chosen:
            chosen.append(candidate)
    return chosen


def suits(program: Program, learner: Learner, covers: list[Need]) -> bool:
    """Whether a course is a fair offer for her level.

    A course for working professionals is offered only when she rated
    herself at work level (3 of 0–4) in the field it belongs to.
    """
    title = " ".join(str(t) for t in (program.title_i18n or {}).values())
    if not catalogue_rules.PROFESSIONAL_ONLY.search(title):
        return True
    bands = [level_band(learner, need) for need in covers]
    return any(band is not None and band >= HELD_LEVEL for band in bands)


async def candidates_for(session: AsyncSession, learner: Learner) -> list[Candidate]:
    gaps = learner.gaps
    if not gaps:
        return []
    spellings = await spellings_for(session, [g.slug for g in gaps])
    clauses = [
        Program.skills_taught.any(_pattern(word), operator=operators.ilike_op)
        for slug in spellings
        for word in spellings[slug]
    ]
    if not clauses:
        return []
    # Her language first, then a stable order: the pool is capped, and an
    # order Postgres may shuffle made the cap an arbitrary sample.
    language_first = (
        case((Program.language == learner.language, 0), else_=1)
        if learner.language is not None
        else literal(0)
    )
    rows = await session.execute(
        select(Program)
        .where(Program.is_published.is_(True), or_(*clauses))
        .order_by(language_first, Program.published_at.desc().nullslast(), Program.id)
        .limit(CANDIDATE_POOL)
    )
    programs = [
        p
        for p in rows.scalars()
        if p.id not in learner.finished
        and engine.program_fits(p, age=learner.age, region=learner.region)
    ]
    index = await skill_service.SkillIndex.load(
        session, [t for p in programs for t in (p.skills_taught or [])]
    )
    by_slug = {g.slug: g for g in gaps}
    on_topic = learner.held | fields_of(learner.profile) | set(by_slug)
    out: list[Candidate] = []
    seen_titles: set[str] = set()
    for program in programs:
        keys = {index.key(t) for t in (program.skills_taught or []) if t and t.strip()}
        covers = [by_slug[k] for k in keys if k in by_slug]
        if not covers or not suits(program, learner, covers):
            continue
        off_topic = sum(1 for k in keys if not k.startswith("label:") and k not in on_topic)
        # The same course under two spellings of its title is one course.
        title = (program.title_i18n or {}).get("ru") or next(
            iter((program.title_i18n or {}).values()), ""
        )
        stem = title.casefold()[:40]
        if stem in seen_titles:
            continue
        seen_titles.add(stem)
        covers.sort(key=lambda n: -n.weight)
        out.append(Candidate(program, covers, score(program, covers, off_topic, learner)))
    # The course that serves her strongest need comes first; among those, the
    # better fit. A course that merely touches her main goal does not outrank
    # one built for it because it also covers three other things.
    out.sort(key=lambda c: (-c.covers[0].weight, -c.score))
    out = spread(out, gaps, per_skill=4, limit=MAX_CANDIDATES)
    for position, candidate in enumerate(out[:MAX_CANDIDATES], start=1):
        candidate.index = position
    return out[:MAX_CANDIDATES]


def priority_of(candidate: Candidate) -> str:
    weight = max(need.weight for need in candidate.covers)
    if weight >= HIGH_FROM:
        return "high"
    if weight >= WEIGHT["interest"]:
        return "medium"
    return "additional"


# ---------------------------------------------------------------------------
# Step 4: words — the model's, or the rules'
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You write the course recommendation at the end of a development diagnostic on
WomanUP, a portal for women and girls in Uzbekistan. You receive her profile
and a numbered list of candidate courses chosen by rules from the real
catalogue. Your job is to explain, in her terms, which of these courses serve
her goal and in what order — not to find courses.

Rules you must follow:
- Refer to a course only by its candidate number. Never name, invent or
  imply a course that is not in the list.
- Say what a course gives her only from its own description, outcomes and
  tags. Do not claim results its description does not support. If a
  description says little, say that plainly.
- Choose only courses that genuinely serve her goal or close a gap — at
  most six, and two relevant courses are better than five. Mark each high
  (directly serves the main goal), medium (an important secondary skill)
  or additional.
- Do not offer a beginner course to someone at a professional level, or an
  advanced one to a beginner, without saying why.
- The roadmap is 2–4 stages in a sensible order; it may include non-course
  activities (a CV, a portfolio project, practice, applying, finding a
  mentor) where the catalogue has nothing for a need — say so rather than
  pretending a course exists.
- If the profile says too little to recommend well, ask one specific
  follow-up question in `follow_up` and recommend only what is safe.
- Write every text in all three languages: Uzbek in the reformed Latin
  alphabet (ö, ğ, ş, ç), Russian, English. Warm, concrete, second person,
  no flattery, no promises about jobs or income. Keep each text short.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "object",
            "properties": {
                "goal_uz": {"type": "string"},
                "goal_ru": {"type": "string"},
                "goal_en": {"type": "string"},
                "situation_uz": {"type": "string"},
                "situation_ru": {"type": "string"},
                "situation_en": {"type": "string"},
                "gaps_uz": {"type": "array", "items": {"type": "string"}},
                "gaps_ru": {"type": "array", "items": {"type": "string"}},
                "gaps_en": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "goal_uz",
                "goal_ru",
                "goal_en",
                "situation_uz",
                "situation_ru",
                "situation_en",
                "gaps_uz",
                "gaps_ru",
                "gaps_en",
            ],
            "additionalProperties": False,
        },
        "courses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate": {"type": "integer"},
                    "priority": {"type": "string", "enum": ["high", "medium", "additional"]},
                    "why_uz": {"type": "string"},
                    "why_ru": {"type": "string"},
                    "why_en": {"type": "string"},
                    "outcome_uz": {"type": "string"},
                    "outcome_ru": {"type": "string"},
                    "outcome_en": {"type": "string"},
                },
                "required": [
                    "candidate",
                    "priority",
                    "why_uz",
                    "why_ru",
                    "why_en",
                    "outcome_uz",
                    "outcome_ru",
                    "outcome_en",
                ],
                "additionalProperties": False,
            },
        },
        "roadmap": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title_uz": {"type": "string"},
                    "title_ru": {"type": "string"},
                    "title_en": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["course", "activity"]},
                                "candidate": {"type": "integer"},
                                "activity": {
                                    "type": "string",
                                    "enum": [
                                        "",
                                        "cv",
                                        "portfolio",
                                        "practice",
                                        "apply",
                                        "mentor",
                                        "network",
                                        "other",
                                    ],
                                },
                                "text_uz": {"type": "string"},
                                "text_ru": {"type": "string"},
                                "text_en": {"type": "string"},
                            },
                            "required": [
                                "kind",
                                "candidate",
                                "activity",
                                "text_uz",
                                "text_ru",
                                "text_en",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title_uz", "title_ru", "title_en", "items"],
                "additionalProperties": False,
            },
        },
        "next_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text_uz": {"type": "string"},
                    "text_ru": {"type": "string"},
                    "text_en": {"type": "string"},
                },
                "required": ["text_uz", "text_ru", "text_en"],
                "additionalProperties": False,
            },
        },
        "follow_up": {
            "type": "object",
            "properties": {
                "text_uz": {"type": "string"},
                "text_ru": {"type": "string"},
                "text_en": {"type": "string"},
            },
            "required": ["text_uz", "text_ru", "text_en"],
            "additionalProperties": False,
        },
    },
    "required": ["summary", "courses", "roadmap", "next_steps", "follow_up"],
    "additionalProperties": False,
}

LANGS = ("uz", "ru", "en")


def _i18n(payload: dict, prefix: str) -> dict[str, str]:
    return {lang: str(payload.get(f"{prefix}_{lang}") or "").strip() for lang in LANGS}


def _program_line(candidate: Candidate) -> str:
    p = candidate.program
    title = (
        (p.title_i18n or {}).get("ru")
        or (p.title_i18n or {}).get("en")
        or (p.title_i18n or {}).get("uz")
        or p.slug
    )
    desc = (
        (p.description_i18n or {}).get("ru")
        or (p.description_i18n or {}).get("en")
        or (p.description_i18n or {}).get("uz")
        or ""
    )
    outcomes = "; ".join(
        (o.get("ru") or o.get("en") or o.get("uz") or "")
        for o in (p.learning_outcomes or [])
        if isinstance(o, dict)
    )
    bits = [
        f"{candidate.index}. {title}",
        f"provider: {p.provider or 'WomanUP'}",
        f"language: {p.language.value if p.language else '?'}",
        f"level: {p.level.value if p.level else 'unknown'}",
        f"hours: {p.duration_hours if p.duration_hours is not None else 'unknown'}",
        f"tags: {', '.join(p.skills_taught or [])}",
        f"closes: {', '.join(f'{n.slug}({n.why})' for n in candidate.covers)}",
        "own course with lessons: "
        + ("yes" if p.external_url is None else f"no, on {p.external_url}"),
        f"description: {desc[:300]}",
    ]
    if outcomes:
        bits.append(f"outcomes: {outcomes[:300]}")
    return "\n   ".join(bits)


def user_message(learner: Learner, candidates: list[Candidate]) -> str:
    profile = learner.profile
    lines = [
        "PROFILE",
        f"situation: {profile.get('situation')}"
        + (f" ({profile.get('situation_other')})" if profile.get("situation_other") else ""),
        f"main goal: {profile.get('goal_primary')}; "
        f"other goals: {', '.join(profile.get('goals_other') or []) or 'none'}",
        f"in her words (6-12 months): {profile.get('goal_text') or '-'}",
        f"interests: {', '.join(profile.get('interests') or []) or 'none chosen'}",
        f"self-rated levels (0 none .. 4 professional): {profile.get('skill_levels') or {}}",
        "situation details: "
        + ", ".join(
            f"{k}={profile[k]}"
            for k in (
                "study_field",
                "practice",
                "first_job_when",
                "position",
                "career_wish",
                "experience",
                "business_stage",
                "business_challenges",
                "job_search_duration",
            )
            if profile.get(k)
        ),
        f"difficulties: {', '.join(profile.get('difficulties') or [])}",
        f"learning: formats={profile.get('formats') or []}, "
        f"hours/week={profile.get('hours')}, language={profile.get('language')}",
        "weak Development Score dimensions: "
        + (", ".join(d.value for d in learner.weak_dimensions) or "none"),
        f"skills she already holds: {', '.join(sorted(learner.held)) or 'none on record'}",
        f"gaps (skill: why): {', '.join(f'{g.slug}: {g.why}/{g.source}' for g in learner.gaps)}",
        "",
        "CANDIDATE COURSES (refer to them by number only)",
    ]
    lines += [_program_line(c) for c in candidates]
    return "\n".join(lines)


@dataclass(slots=True)
class Recommendation:
    mode: str  # ai | rules | insufficient
    summary: dict[str, Any]
    courses: list[dict[str, Any]]
    roadmap: list[dict[str, Any]]
    next_steps: list[dict[str, str]]
    follow_up: dict[str, str] | None
    gaps: list[dict[str, Any]]
    trace: dict[str, Any] | None = None
    #: The model has yet to write its words (the worker will), and how often it tried.
    ai_pending: bool = False
    ai_tries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": RECOMMENDATION_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": self.mode,
            "summary": self.summary,
            "courses": self.courses,
            "roadmap": self.roadmap,
            "next_steps": self.next_steps,
            "follow_up": self.follow_up,
            "gaps": self.gaps,
            "trace": self.trace,
            "ai_pending": self.ai_pending,
            "ai_tries": self.ai_tries,
        }


def _course_record(
    candidate: Candidate, priority: str, why: dict[str, str], outcome: dict[str, str]
) -> dict[str, Any]:
    return {
        "program_id": str(candidate.program.id),
        "candidate": candidate.index,
        "priority": priority,
        "why_i18n": why,
        "outcome_i18n": outcome,
        "covers": [need.slug for need in candidate.covers],
    }


# --- the rules' own words, when the model is off ---------------------------------

T = {
    "why_goal": {
        "uz": "Asosiy maqsadingiz uçun kerak: {skills}.",
        "ru": "Нужно для вашей главной цели: {skills}.",
        "en": "Needed for your main goal: {skills}.",
    },
    "why_goal_other": {
        "uz": "Qöşimça maqsadingiz uçun foydali: {skills}.",
        "ru": "Пригодится для вашей дополнительной цели: {skills}.",
        "en": "Useful for your secondary goal: {skills}.",
    },
    "why_interest": {
        "uz": "Qiziqqan sohangiz böyiça: {skills}.",
        "ru": "По интересной вам сфере: {skills}.",
        "en": "In a field that interests you: {skills}.",
    },
    "why_difficulty": {
        "uz": "Siz aytgan qiyinçilikni yengişga yordam beradi: {skills}.",
        "ru": "Поможет с тем, что вы назвали трудностью: {skills}.",
        "en": "Helps with a difficulty you named: {skills}.",
    },
    "why_dimension": {
        "uz": "Development Score böyiça kuçsiz yönalişni kuçaytiradi: {skills}.",
        "ru": "Укрепит слабое направление по Development Score: {skills}.",
        "en": "Strengthens a weak Development Score area: {skills}.",
    },
    "outcome_none": {
        "uz": "Kurs tavsifi natijani aniq aytmaydi; dastur sahifasida tekşiring.",
        "ru": "Описание курса не называет результат; проверьте на странице курса.",
        "en": "The course description does not state an outcome; check the course page.",
    },
    "stage_core": {
        "uz": "Asos: maqsad uçun kerakli könikmalar",
        "ru": "Основа: навыки для цели",
        "en": "Foundation: the skills your goal needs",
    },
    "stage_field": {
        "uz": "Kengaytiriş: qiziqqan sohangiz",
        "ru": "Расширение: ваша сфера интересов",
        "en": "Broadening: your field of interest",
    },
    "stage_apply": {
        "uz": "Amalda qöllaş",
        "ru": "Применение на практике",
        "en": "Putting it to use",
    },
    "act_cv": {
        "uz": "Rezyumeni yangilang va portfolio tayyorlang.",
        "ru": "Обновите резюме и соберите портфолио.",
        "en": "Update your CV and put together a portfolio.",
    },
    "act_apply": {
        "uz": "Imkoniyatlar bölimidan 2–3 ta eʼlonga javob bering.",
        "ru": "Откликнитесь на 2–3 объявления из раздела «Возможности».",
        "en": "Apply to two or three listings from Opportunities.",
    },
    "act_practice": {
        "uz": "Amaliy topşiriqni bajaring va natijani portfolioga qöşing.",
        "ru": "Выполните практическое задание и добавьте результат в портфолио.",
        "en": "Complete a practical task and add the result to your portfolio.",
    },
    "act_business": {
        "uz": "Biznes-rejaning qoralamasini yozing: mijoz, narx, oylik xarajatlar.",
        "ru": "Напишите черновик бизнес-плана: клиент, цена, расходы в месяц.",
        "en": "Draft a business plan: the customer, the price, monthly costs.",
    },
    "act_mentor": {
        "uz": "Mentor toping: savollaringizni yozib qöying.",
        "ru": "Найдите наставника: запишите свои вопросы.",
        "en": "Find a mentor: write down your questions.",
    },
    "next_first": {
        "uz": "Birinçi kursni boşlang: {title}.",
        "ru": "Начните с первого курса: {title}.",
        "en": "Start with the first course: {title}.",
    },
    "follow_up": {
        "uz": (
            "Tavsiyalar aniqroq bölişi uçun qiziqqan sohangizni va 6–12 oylik "
            "maqsadingizni öz sözlaringiz bilan yozing."
        ),
        "ru": (
            "Чтобы рекомендации стали точнее, отметьте интересующие сферы и опишите "
            "цель на 6–12 месяцев своими словами."
        ),
        "en": (
            "For sharper recommendations, pick the fields that interest you and "
            "describe your 6–12 month goal in your own words."
        ),
    },
    "no_courses": {
        "uz": "Katalogda bu könikmalar böyiça kurs hali yöq: {skills}.",
        "ru": "В каталоге пока нет курсов по этим навыкам: {skills}.",
        "en": "The catalogue has no course yet for these skills: {skills}.",
    },
}


def _fill(key: str, **values: str) -> dict[str, str]:
    return {lang: text.format(**values) for lang, text in T[key].items()}


def _skill_names(slugs: list[str], names: dict[str, dict[str, str]]) -> dict[str, str]:
    return {lang: ", ".join((names.get(s) or {}).get(lang) or s for s in slugs) for lang in LANGS}


def _course_title(program: Program) -> str:
    return (program.title_i18n or {}).get("ru") or next(
        iter((program.title_i18n or {}).values()), program.slug
    )


def rules_words(
    learner: Learner, candidates: list[Candidate], names: dict[str, dict[str, str]]
) -> Recommendation:
    """The explanation the rules write when the model is off or silent."""
    chosen = spread(candidates, learner.gaps)
    courses = []
    for candidate in chosen:
        top = candidate.covers[0]
        skills = _skill_names([n.slug for n in candidate.covers[:3]], names)
        why = {lang: T[f"why_{top.why}"][lang].format(skills=skills[lang]) for lang in LANGS}
        p = candidate.program
        desc = {lang: ((p.description_i18n or {}).get(lang) or "").strip()[:240] for lang in LANGS}
        outcome = {
            lang: desc[lang] or desc["ru"] or desc["en"] or T["outcome_none"][lang]
            for lang in LANGS
        }
        courses.append(_course_record(candidate, priority_of(candidate), why, outcome))
    summary = {
        "goal_i18n": dict.fromkeys(LANGS, ""),
        "situation_i18n": dict.fromkeys(LANGS, ""),
        "gaps_i18n": {
            lang: [(names.get(g.slug) or {}).get(lang) or g.slug for g in learner.gaps[:6]]
            for lang in LANGS
        },
    }
    high = [c for c in courses if c["priority"] == "high"]
    rest = [c for c in courses if c["priority"] != "high"]
    roadmap = []
    if high:
        roadmap.append(
            {
                "title_i18n": T["stage_core"],
                "items": [{"kind": "course", "program_id": c["program_id"]} for c in high[:3]],
            }
        )
    if rest:
        roadmap.append(
            {
                "title_i18n": T["stage_field"],
                "items": [{"kind": "course", "program_id": c["program_id"]} for c in rest[:3]],
            }
        )
    goals = set(learner.profile.get("goals") or [])
    applying = [{"kind": "activity", "activity": "practice", "text_i18n": T["act_practice"]}]
    if goals & {"first_job", "change_direction", "new_profession", "promotion"}:
        applying += [
            {"kind": "activity", "activity": "cv", "text_i18n": T["act_cv"]},
            {"kind": "activity", "activity": "apply", "text_i18n": T["act_apply"]},
        ]
    if goals & {"start_business", "grow_business"}:
        applying.append({"kind": "activity", "activity": "other", "text_i18n": T["act_business"]})
    if {"mentorship", "confidence"} & set(learner.profile.get("difficulties") or []):
        applying.append({"kind": "activity", "activity": "mentor", "text_i18n": T["act_mentor"]})
    roadmap.append({"title_i18n": T["stage_apply"], "items": applying})
    next_steps = []
    if chosen:
        next_steps.append(_fill("next_first", title=_course_title(chosen[0].program)))
    uncovered = [g.slug for g in learner.gaps if not any(g.slug in c["covers"] for c in courses)]
    if uncovered:
        missing = _skill_names(uncovered[:4], names)
        next_steps.append(
            {lang: T["no_courses"][lang].format(skills=missing[lang]) for lang in LANGS}
        )
    follow_up = (
        T["follow_up"] if not (learner.profile.get("interests") or learner.goal_text) else None
    )
    mode = "rules" if chosen else "insufficient"
    return Recommendation(
        mode,
        summary,
        courses,
        roadmap,
        next_steps,
        follow_up,
        [
            {"slug": g.slug, "why": g.why, "source": g.source, "name_i18n": names.get(g.slug) or {}}
            for g in learner.gaps
        ],
        None,
    )


def parse_model(
    payload: dict[str, Any],
    candidates: list[Candidate],
    names: dict[str, dict[str, str]],
    learner: Learner,
) -> Recommendation | None:
    """The model's answer, checked: only candidate numbers it was given, each
    course once, priorities from the allowed set, empty texts refused."""
    by_index = {c.index: c for c in candidates}
    courses: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in payload.get("courses") or []:
        number = item.get("candidate")
        candidate = by_index.get(number) if isinstance(number, int) else None
        if candidate is None or number in seen:
            continue
        why, outcome = _i18n(item, "why"), _i18n(item, "outcome")
        if not all(why.values()) or not all(outcome.values()):
            continue
        priority = (
            item.get("priority")
            if item.get("priority") in ("high", "medium", "additional")
            else priority_of(candidate)
        )
        seen.add(number)
        courses.append(_course_record(candidate, priority, why, outcome))
        if len(courses) >= MAX_COURSES:
            break
    if not courses:
        return None
    allowed = {c["candidate"]: c["program_id"] for c in courses}
    roadmap = []
    for stage in (payload.get("roadmap") or [])[:5]:
        items = []
        for entry in (stage.get("items") or [])[:6]:
            if entry.get("kind") == "course":
                program_id = allowed.get(entry.get("candidate"))
                if program_id:
                    items.append({"kind": "course", "program_id": program_id})
            else:
                text = _i18n(entry, "text")
                if all(text.values()):
                    items.append(
                        {
                            "kind": "activity",
                            "activity": entry.get("activity") or "other",
                            "text_i18n": text,
                        }
                    )
        title = _i18n(stage, "title")
        if items and all(title.values()):
            roadmap.append({"title_i18n": title, "items": items})
    summary_raw = payload.get("summary") or {}
    summary = {
        "goal_i18n": _i18n(summary_raw, "goal"),
        "situation_i18n": _i18n(summary_raw, "situation"),
        "gaps_i18n": {
            lang: [str(g) for g in (summary_raw.get(f"gaps_{lang}") or [])][:6] for lang in LANGS
        },
    }
    next_steps = [
        t
        for t in (_i18n(s, "text") for s in (payload.get("next_steps") or [])[:5])
        if all(t.values())
    ]
    follow = _i18n(payload.get("follow_up") or {}, "text")
    follow_up = follow if all(follow.values()) else None
    return Recommendation(
        "ai",
        summary,
        courses,
        roadmap,
        next_steps,
        follow_up,
        [
            {"slug": g.slug, "why": g.why, "source": g.source, "name_i18n": names.get(g.slug) or {}}
            for g in learner.gaps
        ],
        None,
    )


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


async def skill_names(session: AsyncSession, slugs: list[str]) -> dict[str, dict[str, str]]:
    if not slugs:
        return {}
    rows = await session.execute(select(Skill.slug, Skill.name_i18n).where(Skill.slug.in_(slugs)))
    return {slug: dict(names) for slug, names in rows}


def goals_from_legacy(codes: list[str]) -> list[str]:
    """Version-3 goals standing for version-2 goal codes, for attempts
    taken before the profile block existed."""
    out: list[str] = []
    for code in codes:
        for goal, legacy in GOAL_LEGACY.items():
            if code in legacy and goal not in out:
                out.append(goal)
                break
    return out


async def load_learner(session: AsyncSession, attempt: Assessment) -> Learner:
    profile = dict(attempt.profile or {})
    if not profile.get("goals") and attempt.goals:
        profile["goals"] = goals_from_legacy(list(attempt.goals))
        profile.setdefault("goal_primary", (profile["goals"] or [None])[0])
    user_id = attempt.user_id
    language = await session.scalar(select(User.language).where(User.id == user_id))
    preferred = profile.get("language")
    if preferred in ("uz", "ru", "en"):
        language = Language(preferred)
    region = await session.scalar(select(User.region).where(User.id == user_id))
    her = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    ctx_age = age_from_profile(her)
    recorded = await skill_service.skill_keys(session, user_id)
    rows = await session.execute(select(Enrollment).where(Enrollment.user_id == user_id))
    finished: set[uuid.UUID] = set()
    started: set[uuid.UUID] = set()
    for enrollment in rows.scalars():
        (finished if enrollment.status.value == "completed" else started).add(enrollment.program_id)
    weak = sorted(
        (
            ScoreDimension(d)
            for d, v in (attempt.dimension_scores or {}).items()
            if int(v) < SCORE_FOCUS_BELOW
        ),
        key=lambda d: int((attempt.dimension_scores or {})[d.value]),
    )[:3]
    learner = Learner(
        user_id=user_id,
        profile=profile,
        language=language,
        held=held_from(profile, recorded),
        skill_levels={k: int(v) for k, v in (profile.get("skill_levels") or {}).items()},
        weak_dimensions=weak,
        finished=finished,
        started=started,
        age=ctx_age,
        region=region.value if hasattr(region, "value") else region,
        goal_text=str(profile.get("goal_text") or ""),
    )
    learner.needs = needs_for(profile, weak)
    learner.gaps = gaps_for(learner.needs, learner.held)
    return learner


async def recommend(
    session: AsyncSession,
    attempt: Assessment,
    *,
    gateway: LlmGateway | None = None,
    inline_model: bool = False,
) -> Recommendation:
    """Build the recommendation for one attempt and store it on the attempt.

    The rules answer at once. When the model is on, the answer is marked
    pending and the worker asks the model for its words shortly after — the
    page shows the rules' words meanwhile. `inline_model` asks the model
    here and now instead (tests, scripts). The caller commits."""
    gateway = gateway or llm_gateway
    learner = await load_learner(session, attempt)
    candidates = await candidates_for(session, learner)
    names = await skill_names(session, [g.slug for g in learner.gaps])
    result = rules_words(learner, candidates, names)
    result.ai_pending = bool(candidates) and gateway.enabled
    attempt.recommendation = result.as_dict()
    await session.flush()
    if result.ai_pending and inline_model:
        return await explain(session, attempt, gateway=gateway)
    return result


async def explain(
    session: AsyncSession, attempt: Assessment, *, gateway: LlmGateway | None = None
) -> Recommendation:
    """Ask the model for its words on the rules' candidates and store them.

    The candidates are rebuilt the same deterministic way, so the model sees
    exactly what the rules chose. A refusal, an outage or an unusable answer
    leaves the rules' words in place; after `MODEL_TRIES` the attempt stops
    being pending. Adds the trace row; the caller commits."""
    gateway = gateway or llm_gateway
    stored = dict(attempt.recommendation or {})
    tries = int(stored.get("ai_tries") or 0) + 1
    learner = await load_learner(session, attempt)
    candidates = await candidates_for(session, learner)
    names = await skill_names(session, [g.slug for g in learner.gaps])
    result = rules_words(learner, candidates, names)
    result.ai_tries = tries
    result.ai_pending = False
    if candidates and gateway.enabled:
        shown = candidates[:MODEL_COURSES]
        prompt = user_message(learner, shown)
        response = None
        try:
            response = await gateway.complete(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                json_schema=SCHEMA,
                max_tokens=MODEL_MAX_TOKENS,
                effort="low",
            )
        except LlmUnavailableError as exc:
            logger.warning("course explanation unavailable, keeping the rules: %s", exc)
            result.ai_pending = tries < MODEL_TRIES
        if response is not None:
            parsed = None
            if not response.refused and response.parsed is not None:
                parsed = parse_model(response.parsed, shown, names, learner)
            if parsed is not None:
                parsed.ai_tries = tries
                result = parsed
                result.trace = {
                    "model": response.model,
                    "prompt_version": response.prompt_version,
                    "trace_id": response.trace_id,
                }
            session.add(
                AiInteraction(
                    user_id=attempt.user_id,
                    feature=FEATURE,
                    model_version=response.model,
                    prompt_version=response.prompt_version,
                    trace_id=response.trace_id,
                    prompt_excerpt=prompt[:1000],
                    response_excerpt=LlmGateway.sanitise(response.text)[:1000],
                    retrieved_sources=[
                        {"program_id": str(c.program.id), "slug": c.program.slug} for c in shown
                    ],
                    refused=response.refused,
                    latency_ms=response.latency_ms,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
            )
    attempt.recommendation = result.as_dict()
    await session.flush()
    return result


async def explain_pending(
    session: AsyncSession, *, gateway: LlmGateway | None = None, limit: int = 5
) -> int:
    """The worker's job: give the model its turn on attempts still waiting for
    it, oldest first. Returns how many were handled."""
    gateway = gateway or llm_gateway
    if not gateway.enabled:
        return 0
    rows = await session.execute(
        select(Assessment)
        .where(
            Assessment.recommendation.is_not(None),
            Assessment.recommendation["ai_pending"].as_boolean().is_(True),
        )
        .order_by(Assessment.completed_at.asc().nullsfirst())
        .limit(limit)
    )
    handled = 0
    for attempt in rows.scalars():
        try:
            await explain(session, attempt, gateway=gateway)
        except Exception:  # one bad attempt must not block the others
            logger.exception("course explanation failed for attempt %s", attempt.id)
            stored = dict(attempt.recommendation or {})
            stored["ai_tries"] = int(stored.get("ai_tries") or 0) + 1
            stored["ai_pending"] = stored["ai_tries"] < MODEL_TRIES
            attempt.recommendation = stored
            await session.flush()
        handled += 1
    return handled


async def programs_in(session: AsyncSession, recommendation: dict[str, Any]) -> dict[str, Program]:
    ids = {c["program_id"] for c in recommendation.get("courses") or []}
    for stage in recommendation.get("roadmap") or []:
        for item in stage.get("items") or []:
            if item.get("kind") == "course" and item.get("program_id"):
                ids.add(item["program_id"])
    if not ids:
        return {}
    rows = await session.execute(select(Program).where(Program.id.in_([uuid.UUID(i) for i in ids])))
    return {str(p.id): p for p in rows.scalars()}
