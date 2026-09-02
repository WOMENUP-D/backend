"""Opportunity matching and skill-gap analysis.

Skill overlap is computed in code — it is deterministic, explainable and free.
The model is used only to phrase the explanation and to break ties.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity import Opportunity
from app.models.profile import Profile
from app.models.program import Program
from app.schemas.opportunity import SkillGap


def _normalise(skill: str) -> str:
    return skill.strip().casefold()


def skill_overlap(
    user_skills: list[str], required_skills: list[str]
) -> tuple[list[str], list[str], float]:
    """Return (matched, missing, coverage)."""
    if not required_skills:
        return [], [], 1.0
    user_set = {_normalise(s) for s in user_skills}
    matched, missing = [], []
    for skill in required_skills:
        (matched if _normalise(skill) in user_set else missing).append(skill)
    return matched, missing, round(len(matched) / len(required_skills), 3)


async def match_opportunities(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = 10,
    region: str | None = None,
) -> list[tuple[Opportunity, list[str], list[str], float]]:
    """Rank active opportunities by skill coverage, most relevant first."""
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    user_skills = profile.skills if profile else []

    stmt = select(Opportunity).where(Opportunity.is_active.is_(True))
    if region:
        stmt = stmt.where(Opportunity.region == region)

    opportunities = list((await session.execute(stmt.limit(200))).scalars())

    scored = []
    for opportunity in opportunities:
        matched, missing, coverage = skill_overlap(user_skills, opportunity.required_skills)
        scored.append((opportunity, matched, missing, coverage))

    scored.sort(key=lambda row: row[3], reverse=True)
    return scored[:limit]


async def analyse_skill_gap(
    session: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID
) -> SkillGap | None:
    """Compare a user's skills to one vacancy and suggest closing courses."""
    opportunity = await session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    matched, missing, coverage = skill_overlap(
        profile.skills if profile else [], opportunity.required_skills
    )

    recommended: list[uuid.UUID] = []
    if missing:
        programs = list(
            (await session.execute(select(Program).where(Program.is_published.is_(True)))).scalars()
        )
        missing_set = {_normalise(s) for s in missing}
        for program in programs:
            if missing_set & {_normalise(s) for s in program.skills_taught}:
                recommended.append(program.id)

    return SkillGap(
        opportunity_id=opportunity_id,
        matched_skills=matched,
        missing_skills=missing,
        coverage=coverage,
        recommended_program_ids=recommended[:5],
    )
