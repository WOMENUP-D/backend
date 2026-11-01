"""The vacancy marketplace: real listings from the configured job board.

Open to everyone. Nothing here is about the reader: the board is asked for
what she typed and ticked, and answers are the board's own, in the portal's
provider-independent shape (`services.vacancies.base`). The one personal act,
applying, happens on the board through the listing's link; this API takes no
applications and sends the board nothing.

A board that is down or not configured is a 503 with a reason the page can
read, never a page of invented listings.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.deps import DbSession
from app.schemas.vacancy import (
    ProviderInfo,
    RoleGroupRead,
    VacancyCoursesRead,
    VacancyDetailRead,
    VacancyMetaRead,
    VacancyPageRead,
    VacancyRead,
)
from app.services import vacancy_courses
from app.services.vacancies import (
    ProviderError,
    ProviderUnconfigured,
    get_provider,
)
from app.services.vacancies.base import (
    EMPLOYMENT,
    EXPERIENCE,
    PICKS,
    SCHEDULE,
    SORT,
    WORK_FORMAT,
    VacancyQuery,
)

router = APIRouter(prefix="/vacancies", tags=["vacancies"])

ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"


def _unavailable(exc: ProviderError) -> HTTPException:
    reason = (
        "provider_unconfigured" if isinstance(exc, ProviderUnconfigured) else "provider_unavailable"
    )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"reason": reason, "message": str(exc)},
    )


def _one_of(value: str | None, allowed: tuple[str, ...], name: str) -> str | None:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{name} must be one of {', '.join(allowed)}",
        )
    return value


@router.get("/meta", response_model=VacancyMetaRead)
async def meta() -> VacancyMetaRead:
    """What the filters can offer: the board behind the page, the regions it
    can filter on, the fixed vocabularies, and its professional categories."""
    provider = get_provider()
    try:
        regions = await provider.regions()
        groups = await provider.role_groups()
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    return VacancyMetaRead(
        provider=ProviderInfo(name=provider.name, label=provider.label, site_url=provider.site_url),
        regions=list(regions),
        experience=list(EXPERIENCE),
        employment=list(EMPLOYMENT),
        schedule=list(SCHEDULE),
        work_format=list(WORK_FORMAT),
        sort=list(SORT),
        picks=list(PICKS),
        categories=[
            RoleGroupRead(id=group.id, name=group.name, roles=len(group.role_ids))
            for group in groups
        ],
    )


@router.get("", response_model=VacancyPageRead)
async def search(
    q: str | None = Query(default=None, max_length=200),
    region: str | None = Query(default=None, max_length=40),
    salary_from: int | None = Query(default=None, ge=0, le=1_000_000_000),
    only_with_salary: bool = False,
    experience: str | None = None,
    employment: str | None = None,
    schedule: str | None = None,
    work_format: str | None = None,
    category: list[str] = Query(default=[]),
    sort: str | None = None,
    pick: list[str] = Query(default=[]),
    page: int = Query(default=1, ge=1, le=100),
    per_page: int = Query(default=20, ge=1, le=50),
) -> VacancyPageRead:
    """A page of real listings matching what she asked for.

    `category` is a board professional category (see `/meta`); its roles are
    passed to the board. `pick` is one of WomanUP's shortcuts — no
    experience, internship, remote, part-time, junior — folded into the
    filters without overriding one she set herself.
    """
    provider = get_provider()
    if len(category) > 10 or len(pick) > len(PICKS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="too many categories or picks",
        )
    role_ids: list[str] = []
    if category:
        try:
            groups = {group.id: group for group in await provider.role_groups()}
        except ProviderError as exc:
            raise _unavailable(exc) from exc
        for category_id in category:
            group = groups.get(category_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"unknown category {category_id!r}",
                )
            role_ids.extend(group.role_ids)

    query = VacancyQuery(
        text=(q or "").strip() or None,
        region=region or None,
        salary_from=salary_from or None,
        only_with_salary=only_with_salary,
        experience=_one_of(experience, EXPERIENCE, "experience"),
        employment=_one_of(employment, EMPLOYMENT, "employment"),
        schedule=_one_of(schedule, SCHEDULE, "schedule"),
        work_format=_one_of(work_format, WORK_FORMAT, "work_format"),
        professional_roles=role_ids,
        sort=_one_of(sort, SORT, "sort") or "relevance",  # type: ignore[arg-type]
        picks=[_one_of(value, PICKS, "pick") or "" for value in pick if value],
        page=page,
        per_page=per_page,
    )
    try:
        result = await provider.search(query)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    return VacancyPageRead(
        items=[VacancyRead.model_validate(item, from_attributes=True) for item in result.items],
        total=result.total,
        page=result.page,
        pages=result.pages,
        per_page=result.per_page,
    )


@router.get("/{vacancy_id}", response_model=VacancyDetailRead)
async def read(vacancy_id: str = Path(pattern=ID_PATTERN)) -> VacancyDetailRead:
    """One listing, in full and as the board publishes it. A listing the
    board has archived still opens, marked closed; one it has deleted is 404."""
    provider = get_provider()
    try:
        detail = await provider.get(vacancy_id)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vacancy not found")
    return VacancyDetailRead.model_validate(detail, from_attributes=True)


@router.get("/{vacancy_id}/courses", response_model=VacancyCoursesRead)
async def courses(
    session: DbSession, vacancy_id: str = Path(pattern=ID_PATTERN)
) -> VacancyCoursesRead:
    """The portal's courses that teach what this listing asks for — chosen by
    the model when the AI layer is on, by skill overlap otherwise, and kept
    per listing so the choice is made once (`services.vacancy_courses`)."""
    provider = get_provider()
    try:
        detail = await provider.get(vacancy_id)
    except ProviderError as exc:
        raise _unavailable(exc) from exc
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vacancy not found")
    return await vacancy_courses.suggestions_for_vacancy(
        session,
        provider=provider.name,
        vacancy_id=detail.id,
        listing=vacancy_courses.Requirement(
            title=detail.title,
            skills=list(detail.skills),
            description=detail.description_text,
        ),
    )
