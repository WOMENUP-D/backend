"""How full a profile is, counted over what the portal actually asks for.

The indicator used to count eight profile columns, five of which no form on
the site offers (district, education, employment, profession, skills). A
woman who had answered everything she was ever asked saw 60% and a button
telling her to fill in her profile, and nothing she could do moved it. The
count now covers the questions the sign-up and the onboarding put to her:
her name, her date of birth, her region, her interests and a goal. When the
portal starts asking for more, add the field here and the number follows.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Goal, Profile
from app.models.user import User

#: In the order the portal asks for them. Each is worth the same share.
FIELDS = ("full_name", "birth_date", "region", "interests", "goal")


async def completeness(session: AsyncSession, profile: Profile) -> int:
    region = await session.scalar(select(User.region).where(User.id == profile.user_id))
    has_goal = await session.scalar(select(exists().where(Goal.user_id == profile.user_id)))
    filled = sum(
        (
            bool(profile.full_name),
            bool(profile.birth_date),
            bool(region),
            bool(profile.interests),
            bool(has_goal),
        )
    )
    return round(filled * 100 / len(FIELDS))


async def refresh(session: AsyncSession, profile: Profile) -> int:
    """Recompute and store the figure; returns it."""
    profile.completeness_percent = await completeness(session, profile)
    return profile.completeness_percent
