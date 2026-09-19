"""The skill catalogue, and the skills one woman holds.

The catalogue is public: it is the vocabulary courses and listings are written
in, and a visitor deciding whether to register is owed the words the platform
uses. Everything personal is behind `/skills/me` and is only ever the caller's
own — a skill profile is not shown to another user or to an organisation here,
and the future employer view will ask for explicit consent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import ContentDep, CurrentUserDep, DbSession
from app.core.constants import DataClassification, ScoreDimension, SkillCategory
from app.models.skill import Skill
from app.schemas.common import Page, PaginationParams
from app.schemas.skill import (
    SkillCreate,
    SkillProfileRead,
    SkillRead,
    SkillUpdate,
)
from app.services import skills as skill_service
from app.services.audit_service import record_audit
from app.services.recommendation import skills_to_improve

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=Page[SkillRead])
async def list_skills(
    session: DbSession,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(default=None, max_length=100),
    category: SkillCategory | None = None,
    dimension: ScoreDimension | None = None,
    curated_only: bool = True,
) -> Page[SkillRead]:
    """The vocabulary, filterable the way the catalogue screens need it.

    Uncurated entries — minted from a label nobody has named yet — are held
    back by default, so the public list reads as a vocabulary rather than as a
    pile of whatever partners have sent.
    """
    stmt = select(Skill).where(Skill.is_active.is_(True))
    if curated_only:
        stmt = stmt.where(Skill.is_curated.is_(True))
    if category:
        stmt = stmt.where(Skill.category == category)
    if dimension:
        stmt = stmt.where(Skill.dimensions.any(dimension.value))
    if search:
        # A reader searches in her own language, and an author may have written
        # the label in another: names and spellings are both matched.
        pattern = f"%{search.lower()}%"
        key = skill_service.normalise(search)
        stmt = stmt.where(
            or_(
                func.lower(Skill.name_i18n.op("->>")("uz")).like(pattern),
                func.lower(Skill.name_i18n.op("->>")("ru")).like(pattern),
                func.lower(Skill.name_i18n.op("->>")("en")).like(pattern),
                Skill.slug.like(pattern),
                Skill.aliases.any(key),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(Skill.category, Skill.slug).offset(pagination.offset).limit(pagination.size)
    )
    return Page[SkillRead](
        items=[SkillRead.model_validate(skill) for skill in rows.scalars()],
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


@router.get("/me", response_model=SkillProfileRead)
async def my_skills(user: CurrentUserDep, session: DbSession) -> SkillProfileRead:
    """Her skills, how well each is backed, and what to build next.

    Two lists on purpose. What she holds is a record of evidence; what she is
    missing is drawn from the live offer, so every gap named is one the
    platform can actually close.
    """
    user_id = uuid.UUID(user.id)
    return SkillProfileRead(
        skills=await skill_service.skill_profile(session, user_id),
        improve=await skills_to_improve(session, user_id),
    )


@router.get("/{slug}", response_model=SkillRead)
async def read_skill(slug: str, session: DbSession) -> Skill:
    skill = await session.scalar(select(Skill).where(Skill.slug == slug))
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return skill


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
async def create_skill(payload: SkillCreate, user: ContentDep, session: DbSession) -> Skill:
    """Add a skill to the vocabulary. Trainers, moderators and admins only."""
    exists = await session.scalar(select(Skill.id).where(Skill.slug == payload.slug))
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Skill '{payload.slug}' already exists",
        )

    skill = Skill(
        slug=payload.slug,
        name_i18n=payload.name_i18n,
        category=payload.category,
        dimensions=[dimension.value for dimension in payload.dimensions],
        aliases=_aliases(payload.aliases, payload.name_i18n, payload.slug),
        is_curated=True,
        is_active=payload.is_active,
    )
    session.add(skill)
    await session.flush()

    await record_audit(
        session,
        action="skill.create",
        entity_type="skill",
        entity_id=str(skill.id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.PUBLIC,
        changes={"slug": skill.slug},
    )
    return skill


@router.patch("/{slug}", response_model=SkillRead)
async def update_skill(
    slug: str, payload: SkillUpdate, user: ContentDep, session: DbSession
) -> Skill:
    """Name a skill properly, widen its spellings, or retire it.

    The slug never changes: rows already point at it. Editing an uncurated
    entry is what turns a label a partner sent into a named skill.
    """
    skill = await session.scalar(select(Skill).where(Skill.slug == slug))
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    changes = payload.model_dump(exclude_unset=True)
    if "name_i18n" in changes and changes["name_i18n"]:
        skill.name_i18n = changes["name_i18n"]
    if "category" in changes and changes["category"]:
        skill.category = changes["category"]
    if "dimensions" in changes and changes["dimensions"] is not None:
        skill.dimensions = [ScoreDimension(d).value for d in changes["dimensions"]]
    if "aliases" in changes and changes["aliases"] is not None:
        skill.aliases = _aliases(changes["aliases"], skill.name_i18n, skill.slug)
    if "is_active" in changes and changes["is_active"] is not None:
        skill.is_active = changes["is_active"]
    skill.is_curated = True

    await record_audit(
        session,
        action="skill.update",
        entity_type="skill",
        entity_id=str(skill.id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.PUBLIC,
        changes={"slug": skill.slug, "fields": sorted(changes)},
    )
    await session.flush()
    return skill


def _aliases(written: list[str], name_i18n: dict, slug: str) -> list[str]:
    """Stored normalised, with the names and the slug folded in: a label
    written the way it is displayed has to resolve without being listed twice."""
    keys = {skill_service.normalise(alias) for alias in written}
    keys.update(skill_service.normalise(str(value)) for value in name_i18n.values())
    keys.add(skill_service.normalise(slug))
    return sorted(key for key in keys if key)
