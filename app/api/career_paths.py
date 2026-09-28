"""Career paths: the catalogue, one direction, and the one she chose.

Thin by design. What a stage's status is, which courses, tasks and listings
belong to a direction and what she should do next are all decided in
`services.career_path`, which reads them off the recommendation engine's own
context.

Reading the catalogue and a direction is open, like the programme and
learning-path catalogues it is built from: a visitor deciding whether the
platform is for her is owed a look at where it can lead. Everything personal —
her fit, her journey, her chosen direction — answers for the caller in the
token and for nobody else. There is no route that takes a user id.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUserDep, DbSession, OptionalUserDep
from app.core.constants import CareerCategory
from app.models.career_path import CareerPath
from app.schemas.career_path import CareerChoiceIn, CareerPathDetail, CareerPathRead
from app.services import career_path

router = APIRouter(prefix="/career-paths", tags=["career-paths"])


@router.get("", response_model=list[CareerPathRead])
async def list_career_paths(
    session: DbSession, user: OptionalUserDep, category: CareerCategory | None = None
) -> list[CareerPathRead]:
    """Published directions, with her fit on each when she is signed in."""
    user_id = uuid.UUID(user.id) if user else None
    return await career_path.catalogue_for(session, user_id, category=category)


@router.get("/me", response_model=CareerPathDetail | None)
async def my_career_path(user: CurrentUserDep, session: DbSession) -> CareerPathDetail | None:
    """Her chosen direction, read in stages — or null when she has not chosen.

    Declared before `/{slug}` so "me" is never read as the name of a path.
    """
    return await career_path.my_direction(session, uuid.UUID(user.id))


@router.put("/me", response_model=CareerPathDetail)
async def choose_career_path(
    payload: CareerChoiceIn, user: CurrentUserDep, session: DbSession
) -> CareerPathDetail:
    """Make a direction hers. Idempotent, and starts or enrols her in nothing."""
    path = await _published(session, payload.slug)
    user_id = uuid.UUID(user.id)
    await career_path.choose(session, user_id=user_id, path=path)
    return await career_path.detail_for(session, path, user_id)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def clear_career_path(user: CurrentUserDep, session: DbSession) -> None:
    """Forget her direction. Her courses, tasks and portfolio stay as they are."""
    await career_path.clear(session, user_id=uuid.UUID(user.id))


@router.get("/{slug}", response_model=CareerPathDetail)
async def read_career_path(
    slug: str, session: DbSession, user: OptionalUserDep
) -> CareerPathDetail:
    """One direction in stages, with her journey on it when she is signed in."""
    path = await _published(session, slug)
    return await career_path.detail_for(session, path, uuid.UUID(user.id) if user else None)


async def _published(session: DbSession, slug: str) -> CareerPath:
    path = await career_path.by_slug(session, slug[:160])
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Career path not found")
    return path
