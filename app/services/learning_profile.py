"""Turning questionnaire answers into a usable learning profile.

`derive` folds seventeen answers into the handful of facts the assistant
actually reasons over — the field she is aiming at, why, how much time she has,
and how far along she says she is. Raw answers are kept, but nothing downstream
reads them directly: the summary is the contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import LearningProfile, Profile
from app.services import questionnaire

# Weekly hours, as a single number the planner can multiply.
HOURS = {"under_3": 2, "3_5": 4, "5_10": 7, "10_20": 15, "over_20": 25}

# Years of experience, as a band.
EXPERIENCE_RANK = {"none": 0, "under_1": 1, "1_3": 2, "3_5": 3, "over_5": 4}


def derive(answers: dict[str, Any]) -> dict[str, Any]:
    """The short summary everything downstream reads instead of raw answers."""
    experience = str(answers.get("experience") or "none")
    try:
        self_level = int(answers.get("self_level") or 0)
    except (TypeError, ValueError):
        self_level = 0

    def as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            # Free-text lists are comma-separated in the form.
            return [part.strip() for part in value.split(",") if part.strip()]
        return []

    def as_options(value: Any) -> list[str]:
        """Chosen option values.

        A plain string is wrapped rather than split: answers saved before these
        questions accepted more than one must keep meaning what they meant, and
        an option value is never a comma-separated list.
        """
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    occupations = as_options(answers.get("occupation"))
    educations = as_options(answers.get("education"))
    goals = as_options(answers.get("goal"))
    target = (answers.get("target_field") or "").strip() or None
    # She may read comfortably in more than one language. The level test asks
    # for exactly one, so the first pick leads and the rest widen what the
    # catalogue may offer her.
    languages = as_options(answers.get("language")) or ["uz"]

    return {
        "occupations": occupations,
        "educations": educations,
        "current_field": (answers.get("field") or "").strip() or None,
        "target_field": target,
        "goals": goals,
        "goal_3_6": (answers.get("goal_3_6") or "").strip() or None,
        "experience": experience,
        "experience_rank": EXPERIENCE_RANK.get(experience, 0),
        "hours_per_week": HOURS.get(str(answers.get("hours_per_week")), 4),
        "speed_priority": answers.get("speed"),
        # "Which areas interest you?" is gone — it asked the same thing as the
        # field she wants to learn. Older answer sets still carry it.
        "interests": as_list(answers.get("interest_areas")) or ([target] if target else []),
        "task_style": as_list(answers.get("task_style")),
        "skills": as_list(answers.get("current_skills")),
        "tools": as_list(answers.get("tools")),
        "self_level": self_level,
        "studied": (answers.get("studied") or "").strip() or None,
        "hardest": (answers.get("hardest") or "").strip() or None,
        "practice": answers.get("practice"),
        "language": languages[0],
        "languages": languages,
    }


def missing_required(answers: dict[str, Any]) -> list[str]:
    """Required ids with no usable answer. Empty means the run is complete."""

    def blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, list):
            return not value
        return False

    return [qid for qid in sorted(questionnaire.required_ids()) if blank(answers.get(qid))]


async def get(session: AsyncSession, user_id: uuid.UUID) -> LearningProfile | None:
    return await session.scalar(select(LearningProfile).where(LearningProfile.user_id == user_id))


# Whole years, for the profile's numeric field. The questionnaire asks for a
# band, so this is the bottom of it — understating experience is the safe
# direction when the number ends up in front of an employer.
EXPERIENCE_YEARS = {"none": 0, "under_1": 0, "1_3": 1, "3_5": 3, "over_5": 5}


def _first(value: Any) -> str | None:
    """The multi-choice answers arrive as lists; the profile holds one value."""
    if isinstance(value, list):
        return str(value[0]) if value else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _phrases(value: Any) -> list[str]:
    """A free-text answer like "Excel, 1C, немного Python" becomes three tags."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str):
        return []
    parts = [part.strip(" .;") for chunk in value.split(";") for part in chunk.split(",")]
    return [p for p in parts if p][:12]


async def fill_profile_from_answers(
    session: AsyncSession, user_id: uuid.UUID, answers: dict[str, Any]
) -> list[str]:
    """Copy what the questionnaire already asked into the profile.

    She answers seventeen questions about her education, her work and what she
    can already do — and until now every one of those answers stopped at the
    learning profile. Her actual profile stayed empty, so the cabinet showed her
    a blank card and asked her to type the same facts a second time.

    Blank fields only. Anything she has edited by hand outranks a questionnaire
    she may have run months ago, and re-taking the assessment must never quietly
    rewrite her profile. Values are stored as the questionnaire's own codes
    rather than as sentences, so they stay language-neutral and are translated
    where they are displayed.

    Returns the field names it filled, for the caller to log or report.
    """
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    if profile is None:
        profile = Profile(user_id=user_id)
        session.add(profile)
        await session.flush()

    filled: list[str] = []

    def put(field: str, value: Any) -> None:
        if value in (None, "", [], 0) and field != "years_of_experience":
            return
        if getattr(profile, field):
            return
        setattr(profile, field, value)
        filled.append(field)

    put("employment_status", _first(answers.get("occupation")))
    put("education_level", _first(answers.get("education")))
    put("education_field", _first(answers.get("field")))
    put("profession", _first(answers.get("target_field")))
    put("languages", _phrases(answers.get("language")))

    # Skills come from two questions — what she can do and what she can use.
    skills = _phrases(answers.get("current_skills")) + _phrases(answers.get("tools"))
    seen: set[str] = set()
    unique = [s for s in skills if not (s.lower() in seen or seen.add(s.lower()))]
    put("skills", unique)

    band = _first(answers.get("experience"))
    if band in EXPERIENCE_YEARS and profile.years_of_experience is None:
        profile.years_of_experience = EXPERIENCE_YEARS[band]
        filled.append("years_of_experience")

    if filled:
        await session.flush()
    return filled


async def save(
    session: AsyncSession, user_id: uuid.UUID, answers: dict[str, Any]
) -> LearningProfile:
    """Upsert the run. Complete only when nothing required is left blank."""
    record = await get(session, user_id)
    if record is None:
        record = LearningProfile(user_id=user_id)
        session.add(record)

    record.version = questionnaire.VERSION
    record.answers = answers
    record.derived = derive(answers)
    if not missing_required(answers):
        record.completed_at = datetime.now(UTC)

    await session.flush()
    # What she just told the questionnaire is what the profile was missing.
    await fill_profile_from_answers(session, user_id, answers)
    return record
