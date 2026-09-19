"""Learning paths: the catalogue, one route, and where she stands on it.

Thin by design. Every decision — what a step's status is, what a path teaches,
whether she may take the next course — is made in `services.learning_path`, so
the same answer reaches the browser, the recommendation engine and anything
built on paths later.

Reading the catalogue is open, like the programme catalogue it is built from:
the landing page advertises these routes, and a visitor deciding whether the
platform is for her is owed a look at them. Everything personal — her progress,
her paths, starting one, enrolling through one — answers for the caller in the
token and for nobody else.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUserDep, DbSession, OptionalUserDep
from app.core.constants import ProficiencyLevel, ScoreDimension
from app.models.learning_path import LearningPath
from app.schemas.learning_path import LearningPathDetail, LearningPathRead
from app.schemas.program import EnrollmentRead
from app.services import learning, learning_path, skills

router = APIRouter(prefix="/learning-paths", tags=["learning-paths"])


@router.get("", response_model=list[LearningPathRead])
async def list_paths(
    session: DbSession,
    user: OptionalUserDep,
    dimension: ScoreDimension | None = None,
    level: ProficiencyLevel | None = None,
    skill: str | None = Query(default=None, max_length=100),
    search: str | None = Query(default=None, max_length=100),
) -> list[LearningPathRead]:
    """Published paths, filtered the way a woman looks for one.

    `skill` accepts whatever she typed or clicked — a taxonomy slug, a Russian
    spelling, an Uzbek one — and is resolved through the skill index rather
    than matched as text, so "бухгалтерия" finds the path whose course teaches
    "buxgalteriya".
    """
    paths = await learning_path.catalogue(session, dimension=dimension, level=level, search=search)
    index = await learning_path.index_for(session, paths)

    if skill:
        wanted = index.key(skill)
        paths = [
            path
            for path in paths
            if any(index.key(label) == wanted for label in learning_path.path_labels(path))
        ]

    if user is None:
        return [learning_path.read(path, index=index) for path in paths]

    user_id = uuid.UUID(user.id)
    held = await skills.skill_keys(session, user_id)
    records = await learning_path.records_for(session, user_id, [path.id for path in paths])
    enrollments = await learning_path.enrollments_for(
        session, user_id, {item.program_id for path in paths for item in path.items}
    )
    return [
        learning_path.read(
            path,
            index=index,
            states=learning_path.item_states(path.items, enrollments),
            record=records.get(path.id),
            held=held,
        )
        for path in paths
    ]


@router.get("/me", response_model=list[LearningPathDetail])
async def my_paths(user: CurrentUserDep, session: DbSession) -> list[LearningPathDetail]:
    """The paths she is on, furthest along first.

    Declared before `/{slug}` so "me" is never read as the name of a path.
    """
    user_id = uuid.UUID(user.id)
    rows = await learning_path.user_paths(session, user_id)
    index = await learning_path.index_for(session, [path for path, _, _ in rows])
    held = await skills.skill_keys(session, user_id)
    return [
        learning_path.detail(path, index=index, states=states, record=record, held=held)
        for path, states, record in rows
    ]


@router.get("/{slug}", response_model=LearningPathDetail)
async def read_path(slug: str, session: DbSession, user: OptionalUserDep) -> LearningPathDetail:
    """One route, with its steps in order and her state on each of them."""
    path = await _published(session, slug)
    index = await learning_path.index_for(session, [path])

    if user is None:
        return learning_path.detail(
            path, index=index, states=learning_path.item_states(path.items, {})
        )

    user_id = uuid.UUID(user.id)
    enrollments = await learning_path.enrollments_for(
        session, user_id, (item.program_id for item in path.items)
    )
    records = await learning_path.records_for(session, user_id, [path.id])
    return learning_path.detail(
        path,
        index=index,
        states=learning_path.item_states(path.items, enrollments),
        record=records.get(path.id),
        held=await skills.skill_keys(session, user_id),
    )


@router.post("/{slug}/start", response_model=LearningPathDetail)
async def start_path(slug: str, user: CurrentUserDep, session: DbSession) -> LearningPathDetail:
    """Take a path on. Idempotent, and enrols her in nothing.

    Starting twice returns the same record and creates no enrollment, so there
    is no way for this to duplicate one. Each course is enrolled in when she
    reaches it.
    """
    path = await _published(session, slug)
    user_id = uuid.UUID(user.id)
    record = await learning_path.start(session, user_id=user_id, path=path)

    enrollments = await learning_path.enrollments_for(
        session, user_id, (item.program_id for item in path.items)
    )
    return learning_path.detail(
        path,
        index=await learning_path.index_for(session, [path]),
        states=learning_path.item_states(path.items, enrollments),
        record=record,
        held=await skills.skill_keys(session, user_id),
    )


@router.post(
    "/{slug}/programs/{program_id}/enroll",
    response_model=EnrollmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_through_path(
    slug: str,
    program_id: uuid.UUID,
    user: CurrentUserDep,
    session: DbSession,
) -> EnrollmentRead:
    """Enrol in a step of a path, in the order the path sets.

    The prerequisite is enforced here rather than by a disabled button: a step
    whose required predecessors are unfinished is a 409, whatever the browser
    sent. The enrollment itself is the existing one — same table, same
    idempotency, same progress — so a course entered through a path and the
    same course entered from the catalogue are one enrollment, not two.
    """
    path = await _published(session, slug)
    user_id = uuid.UUID(user.id)

    if not any(item.program_id == program_id for item in path.items):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Program is not part of this path"
        )
    if not await learning_path.unlocked(session, user_id=user_id, path=path, program_id=program_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finish the earlier required programmes in this path first",
        )

    enrollment = await learning.enroll(session, user_id=user_id, program_id=program_id)
    if enrollment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    # Naming the path she is walking is part of enrolling through it.
    await learning_path.start(session, user_id=user_id, path=path)
    return EnrollmentRead.model_validate(enrollment)


async def _published(session: DbSession, slug: str) -> LearningPath:
    path = await learning_path.by_slug(session, slug)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning path not found")
    return path
