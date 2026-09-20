"""Portfolio: her record, her projects, and the public page she may choose.

Thin by design — every rule lives in `services.portfolio`. What this module
owns is the boundary.

**Her record is hers.** Every personal route answers for the caller in the
token. There is no route that takes a user id, so there is no id to change: a
request for somebody else's portfolio is not refused, it cannot be expressed.

**A project is found through its owner.** Editing or deleting one looks it up
by `(id, caller)` together, so an id belonging to somebody else is a 404 —
indistinguishable from an id that does not exist.

**The public page is keyed by an opaque slug**, and every refusal looks the
same: private, withdrawn, a minor, a closed account and a slug nobody owns are
one 404, so the endpoint cannot be used to learn which links are real.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUserDep, DbSession
from app.schemas.portfolio import (
    PortfolioRead,
    PortfolioSettings,
    PortfolioSettingsIn,
    ProjectIn,
    ProjectRead,
    ProjectUpdate,
    PublicPortfolioRead,
)
from app.services import portfolio

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/me", response_model=PortfolioRead)
async def my_portfolio(user: CurrentUserDep, session: DbSession) -> PortfolioRead:
    """Everything she can show, private parts included, in one response."""
    return await portfolio.portfolio_for(session, uuid.UUID(user.id))


@router.patch("/me", response_model=PortfolioSettings)
async def update_settings(
    payload: PortfolioSettingsIn, user: CurrentUserDep, session: DbSession
) -> PortfolioSettings:
    """Who may see it. Publishing is refused for a minor or an unknown age,
    and every change of visibility is recorded as consent."""
    try:
        return await portfolio.update_settings(session, user_id=uuid.UUID(user.id), payload=payload)
    except portfolio.PortfolioError as exc:
        code = status.HTTP_403_FORBIDDEN if exc.reason == "minor" else 422
        raise HTTPException(
            status_code=code, detail={"reason": exc.reason, "fields": exc.detail}
        ) from exc


@router.post("/me/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectIn, response: Response, user: CurrentUserDep, session: DbSession
) -> ProjectRead:
    """Add a project. A retry with the same `client_ref` returns the first one.

    Skills must be ones the taxonomy knows; an unknown label is a 422 naming
    it, never a new skill. Saving records *self-reported* evidence — the
    project is her word, and nobody on the platform has checked it.
    """
    try:
        project, created = await portfolio.create_project(
            session, user_id=uuid.UUID(user.id), payload=payload
        )
    except portfolio.PortfolioError as exc:
        code = status.HTTP_409_CONFLICT if exc.reason == "too_many" else 422
        raise HTTPException(
            status_code=code, detail={"reason": exc.reason, "fields": exc.detail}
        ) from exc
    if not created:
        # The same save arriving twice: the project exists, nothing was made.
        response.status_code = status.HTTP_200_OK
    return portfolio.project_read(project, await portfolio.project_refs(session, project))


@router.patch("/me/projects/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, user: CurrentUserDep, session: DbSession
) -> ProjectRead:
    """Edit one of her projects. Its skill claims follow the new list."""
    project = await _own(session, user, project_id)
    try:
        await portfolio.update_project(session, project=project, payload=payload)
    except portfolio.PortfolioError as exc:
        raise HTTPException(
            status_code=422, detail={"reason": exc.reason, "fields": exc.detail}
        ) from exc
    return portfolio.project_read(project, await portfolio.project_refs(session, project))


@router.delete("/me/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> None:
    """Remove a project and withdraw the skill claims it made."""
    project = await _own(session, user, project_id)
    await portfolio.delete_project(session, project=project)


@router.get("/public/{slug}", response_model=PublicPortfolioRead)
async def public_portfolio(slug: str, session: DbSession) -> PublicPortfolioRead:
    """A portfolio its owner chose to publish. No account needed to read it."""
    found = await portfolio.public_portfolio(session, slug[:40])
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return found


async def _own(session: DbSession, user, project_id: uuid.UUID):
    """Her project, or a 404 that says nothing about whether it exists."""
    project = await portfolio.own_project(session, uuid.UUID(user.id), project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project
