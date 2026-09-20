"""Learning paths: an ordered route through courses that already exist.

This layer adds no learning. Every course, module, lesson, enrollment,
certificate and skill a path touches is the one the learning section already
owns, and this module only says in which order to take them and how far along
that leaves her.

Three rules hold the design together:

**Progress is never stored.** It is counted from her enrollments each time it
is asked for, so a path can never claim she is further along than the courses
it is made of. `services.learning` remains the only thing that computes a
course's progress; this reads the number it wrote.

**Only required steps count.** An optional step widens a route rather than
lengthening it, so finishing everything required reads as finished, not 80%.

**A lock is a reading, not a gate on the catalogue.** A step is locked when an
earlier required course is unfinished — but never when she is already enrolled
in it. `/dasturlar` stays open: a path recommends an order, it does not take a
course away from a woman who found it herself.

Finishing a path writes skill evidence exactly once, guarded by `completed_at`
the way a course completion is guarded by its own. It is `learning_path`
evidence, which the skill layer maps to **learned** — a route through courses
is still teaching, and nobody outside the platform has checked what she can do
with it. Verification is a different thing and stays a different thing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EnrollmentStatus,
    EvidenceKind,
    ProficiencyLevel,
    ScoreDimension,
)
from app.models.learning_path import LearningPath, LearningPathItem, UserLearningPath
from app.models.program import Enrollment
from app.schemas.learning_path import (
    LearningPathDetail,
    LearningPathItemRead,
    LearningPathProgress,
    LearningPathRead,
    PathItemStatus,
    PathStatus,
)
from app.schemas.program import ProgramRead
from app.services import skills as skill_service

#: A catalogue read is bounded like every other one in the platform.
CATALOGUE_LIMIT = 100


# ---------------------------------------------------------------------------
# Reading paths out of the database
# ---------------------------------------------------------------------------


async def catalogue(
    session: AsyncSession,
    *,
    dimension: ScoreDimension | None = None,
    level: ProficiencyLevel | None = None,
    search: str | None = None,
    published_only: bool = True,
    limit: int = CATALOGUE_LIMIT,
) -> list[LearningPath]:
    """Published paths, in curation order. Items and their courses come with them."""
    stmt = select(LearningPath)
    if published_only:
        stmt = stmt.where(LearningPath.is_published.is_(True))
    if dimension is not None:
        stmt = stmt.where(LearningPath.dimension == dimension)
    if level is not None:
        stmt = stmt.where(LearningPath.level == level)
    if search:
        # The same reach the programme catalogue gives a search box: a woman
        # typing "biznes" is describing where she wants to get to, not quoting
        # a title.
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                *(
                    func.lower(column.op("->>")(language)).like(pattern)
                    for column in (LearningPath.title_i18n, LearningPath.description_i18n)
                    for language in ("uz", "ru", "en")
                ),
                func.lower(LearningPath.slug).like(pattern),
            )
        )

    rows = await session.execute(
        stmt.order_by(LearningPath.order_index, LearningPath.slug).limit(limit)
    )
    return list(rows.scalars())


async def by_slug(
    session: AsyncSession, slug: str, *, published_only: bool = True
) -> LearningPath | None:
    stmt = select(LearningPath).where(LearningPath.slug == slug)
    if published_only:
        stmt = stmt.where(LearningPath.is_published.is_(True))
    return await session.scalar(stmt)


async def paths_with_program(
    session: AsyncSession, program_id: uuid.UUID, *, published_only: bool = True
) -> list[LearningPath]:
    """Every path a course belongs to. The same course may sit in several."""
    stmt = (
        select(LearningPath)
        .join(LearningPathItem, LearningPathItem.path_id == LearningPath.id)
        .where(LearningPathItem.program_id == program_id)
    )
    if published_only:
        stmt = stmt.where(LearningPath.is_published.is_(True))
    return list((await session.execute(stmt.order_by(LearningPath.order_index))).scalars())


async def records_for(
    session: AsyncSession, user_id: uuid.UUID, path_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, UserLearningPath]:
    """The paths she has started, by path id."""
    stmt = select(UserLearningPath).where(UserLearningPath.user_id == user_id)
    ids = list(path_ids) if path_ids is not None else None
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(UserLearningPath.path_id.in_(ids))
    rows = await session.execute(stmt)
    return {record.path_id: record for record in rows.scalars()}


async def enrollments_for(
    session: AsyncSession, user_id: uuid.UUID, program_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Enrollment]:
    """Her enrollments in the courses a path is made of, by programme id."""
    ids = list(program_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(Enrollment).where(Enrollment.user_id == user_id, Enrollment.program_id.in_(ids))
    )
    return {enrollment.program_id: enrollment for enrollment in rows.scalars()}


# ---------------------------------------------------------------------------
# What her enrollments make of a path
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ItemState:
    """One step of a path, read against her record."""

    item: LearningPathItem
    status: PathItemStatus
    enrollment: Enrollment | None


def item_states(
    items: Sequence[LearningPathItem], enrollments: dict[uuid.UUID, Enrollment]
) -> list[ItemState]:
    """Each step's state, in path order.

    Walks the route once, carrying one fact: whether a required course before
    this point is still unfinished. That is the whole prerequisite rule, and
    keeping it to a single pass is what makes it the same rule on the course
    list, on the detail page and at the endpoint that refuses to enrol her.
    """
    states: list[ItemState] = []
    blocked = False

    for item in sorted(items, key=lambda row: row.order_index):
        enrollment = enrollments.get(item.program_id)
        if enrollment is not None and enrollment.status == EnrollmentStatus.COMPLETED:
            status = PathItemStatus.COMPLETED
        elif enrollment is not None:
            # Already studying it. A route may not lock a woman out of a course
            # she has already begun — she found it in the catalogue, which is
            # open, and a path is advice about order.
            status = PathItemStatus.IN_PROGRESS
        elif blocked:
            status = PathItemStatus.LOCKED
        else:
            status = PathItemStatus.AVAILABLE

        states.append(ItemState(item=item, status=status, enrollment=enrollment))
        if item.is_required and status != PathItemStatus.COMPLETED:
            blocked = True

    return states


def _percent(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


def progress_of(
    states: Sequence[ItemState], record: UserLearningPath | None = None
) -> LearningPathProgress:
    """How far along the route she is, counted from her enrollments.

    The required steps are the denominator. A path made entirely of optional
    steps falls back to all of them, because a progress bar with nothing in the
    denominator is the one reading that cannot be right.
    """
    required = [state for state in states if state.item.is_required]
    counted = required or list(states)
    done = sum(1 for state in counted if state.status == PathItemStatus.COMPLETED)

    current = next((state for state in states if state.status == PathItemStatus.IN_PROGRESS), None)
    upcoming = next((state for state in states if state.status == PathItemStatus.AVAILABLE), None)

    if counted and done >= len(counted):
        status = PathStatus.COMPLETED
    elif current is not None or done > 0:
        status = PathStatus.IN_PROGRESS
    else:
        status = PathStatus.NOT_STARTED

    return LearningPathProgress(
        status=status,
        percent=_percent(done, len(counted)),
        completed_items=done,
        required_items=len(required),
        total_items=len(states),
        started_at=record.started_at if record else None,
        completed_at=record.completed_at if record else None,
        current_program_id=current.item.program_id if current else None,
        next_program_id=upcoming.item.program_id if upcoming else None,
    )


def path_labels(path: LearningPath, *, required_only: bool = False) -> list[str]:
    """What the path teaches, as its courses wrote it: in order, no repeats.

    Never a curated second list. A path that named its own skills would drift
    from the courses the moment one of them was edited, and the skills a course
    teaches are already a column on that course.
    """
    seen: set[str] = set()
    labels: list[str] = []
    for item in sorted(path.items, key=lambda row: row.order_index):
        if required_only and not item.is_required:
            continue
        for label in item.program.skills_taught or []:
            key = skill_service.normalise(label)
            if not key or key in seen:
                continue
            seen.add(key)
            labels.append(label)
    return labels


def _totals(path: LearningPath) -> tuple[float | None, int | None]:
    """The route's length, summed off its courses. None when none of them says."""
    hours = [item.program.duration_hours for item in path.items if item.program.duration_hours]
    weeks = [item.program.duration_weeks for item in path.items if item.program.duration_weeks]
    return (round(sum(hours), 1) if hours else None, sum(weeks) if weeks else None)


# ---------------------------------------------------------------------------
# Composing what the API returns
# ---------------------------------------------------------------------------


async def index_for(
    session: AsyncSession, paths: Iterable[LearningPath]
) -> skill_service.SkillIndex:
    """One taxonomy lookup covering every skill every path in a list teaches."""
    labels = {
        label
        for path in paths
        for item in path.items
        for label in (item.program.skills_taught or [])
    }
    return await skill_service.SkillIndex.load(session, labels)


def read(
    path: LearningPath,
    *,
    index: skill_service.SkillIndex,
    states: Sequence[ItemState] | None = None,
    record: UserLearningPath | None = None,
    held: set[str] | None = None,
) -> LearningPathRead:
    """A path as the catalogue lists it, with her progress when there is any."""
    hours, weeks = _totals(path)
    labels = path_labels(path)
    skills = index.refs(labels)
    return LearningPathRead(
        id=path.id,
        slug=path.slug,
        title_i18n=path.title_i18n,
        description_i18n=path.description_i18n,
        dimension=path.dimension,
        level=path.level,
        program_count=len(path.items),
        required_count=sum(1 for item in path.items if item.is_required),
        total_hours=hours,
        total_weeks=weeks,
        skills=skills,
        new_skills=([ref for ref in skills if index.key(ref.label) not in held] if held else []),
        progress=(progress_of(states, record) if states is not None else LearningPathProgress()),
    )


def detail(
    path: LearningPath,
    *,
    index: skill_service.SkillIndex,
    states: Sequence[ItemState],
    record: UserLearningPath | None = None,
    held: set[str] | None = None,
) -> LearningPathDetail:
    """The path opened: the same summary, plus every step in order."""
    summary = read(path, index=index, states=states, record=record, held=held)
    return LearningPathDetail(
        **summary.model_dump(),
        items=[
            LearningPathItemRead(
                program=ProgramRead.model_validate(state.item.program),
                order_index=state.item.order_index,
                is_required=state.item.is_required,
                skills=index.refs(state.item.program.skills_taught or []),
                status=state.status,
                progress_percent=(state.enrollment.progress_percent if state.enrollment else None),
                enrollment_id=state.enrollment.id if state.enrollment else None,
            )
            for state in states
        ],
    )


# ---------------------------------------------------------------------------
# Starting one, and what finishing it means
# ---------------------------------------------------------------------------


async def start(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    path: LearningPath,
    now: datetime | None = None,
) -> UserLearningPath:
    """Record that she took this path on. Idempotent, and enrols her in nothing.

    Starting a path is a decision about direction, not an enrollment in five
    courses at once. She enrols in each one when she reaches it, through the
    enrollment system that already exists — which is also why starting twice
    cannot produce a duplicate enrollment: it produces no enrollment at all.
    """
    now = now or datetime.now(UTC)
    record = await session.scalar(
        select(UserLearningPath).where(
            UserLearningPath.user_id == user_id, UserLearningPath.path_id == path.id
        )
    )
    if record is None:
        record = UserLearningPath(user_id=user_id, path_id=path.id, started_at=now)
        session.add(record)
        await session.flush()

    # She may have finished the courses already, having found them one at a
    # time. Then the path is complete the moment she names it.
    await refresh(session, user_id=user_id, path=path, record=record, now=now)
    return record


async def refresh(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    path: LearningPath,
    record: UserLearningPath | None = None,
    now: datetime | None = None,
) -> UserLearningPath | None:
    """Re-read where she stands and close the path if every required step is done.

    Nothing is written unless the answer changed. A path she has not started is
    left alone: the route is still hers to take, and the platform does not
    award her a path she never said she was on.
    """
    if record is None:
        record = await session.scalar(
            select(UserLearningPath).where(
                UserLearningPath.user_id == user_id, UserLearningPath.path_id == path.id
            )
        )
    if record is None or record.completed_at is not None:
        return record

    enrollments = await enrollments_for(session, user_id, (item.program_id for item in path.items))
    states = item_states(path.items, enrollments)
    if progress_of(states, record).status != PathStatus.COMPLETED:
        return record

    await complete(session, user_id=user_id, path=path, record=record, now=now)
    return record


async def complete(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    path: LearningPath,
    record: UserLearningPath,
    now: datetime | None = None,
) -> None:
    """Everything finishing a path does, exactly once.

    `completed_at` is the guard, as it is for a course. No certificate is
    issued here: the courses already issued theirs, and a second document for
    the same work would mean less, not more.
    """
    if record.completed_at is not None:
        return

    now = now or datetime.now(UTC)
    record.completed_at = now

    # What the required courses taught, recorded once more against the route —
    # at the level the route is pitched at, and still only as *learned*.
    labels = path_labels(path, required_only=True) or path_labels(path)
    index = await skill_service.SkillIndex.load(session, labels)
    for label in labels:
        await skill_service.record_evidence(
            session,
            user_id=user_id,
            label=label,
            kind=EvidenceKind.LEARNING_PATH,
            source_type="learning_path",
            source_id=str(path.id),
            level=path.level,
            occurred_at=now,
            index=index,
        )
    await session.flush()


async def on_program_completed(
    session: AsyncSession, *, user_id: uuid.UUID, program_id: uuid.UUID, now: datetime | None = None
) -> list[LearningPath]:
    """Called when a course completes: close any path that step finished.

    The one place a path can become complete, so completion always follows a
    real course completion rather than a button. Paths she has not started are
    skipped inside `refresh`.
    """
    # The completion that triggered this may still be pending in the session,
    # and this session does not autoflush.
    await session.flush()

    closed: list[LearningPath] = []
    for path in await paths_with_program(session, program_id):
        record = await refresh(session, user_id=user_id, path=path, now=now)
        if record is not None and record.completed_at is not None:
            closed.append(path)
    return closed


# ---------------------------------------------------------------------------
# What other features ask
# ---------------------------------------------------------------------------


async def unlocked(
    session: AsyncSession, *, user_id: uuid.UUID, path: LearningPath, program_id: uuid.UUID
) -> bool:
    """Whether she may take this step of this path yet.

    The backend's answer, not the button's. A step is open when every required
    course before it is finished — or when she is already enrolled in it.
    """
    enrollments = await enrollments_for(session, user_id, (item.program_id for item in path.items))
    return any(
        state.item.program_id == program_id and state.status != PathItemStatus.LOCKED
        for state in item_states(path.items, enrollments)
    )


async def program_ids(session: AsyncSession, path_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Every course reachable through a set of paths."""
    ids = list(path_ids)
    if not ids:
        return set()
    rows = await session.execute(
        select(LearningPathItem.program_id).where(LearningPathItem.path_id.in_(ids))
    )
    return set(rows.scalars())


async def user_paths(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[LearningPath, list[ItemState], UserLearningPath | None]]:
    """Every path she is on: the ones she started, and the ones she is already
    walking without having named them.

    Enrolling in a course from the catalogue puts her on every path that course
    belongs to, as far as the facts go — the progress is real and computed the
    same way. Leaving those out would mean her own record disagreed with the
    course list beside it. The paths she actually started come first.
    """
    started = await records_for(session, user_id)

    enrolled_rows = await session.execute(
        select(Enrollment.program_id).where(Enrollment.user_id == user_id)
    )
    enrolled = set(enrolled_rows.scalars())

    wanted = set(started)
    if enrolled:
        reached = await session.execute(
            select(LearningPathItem.path_id)
            .where(LearningPathItem.program_id.in_(enrolled))
            .distinct()
        )
        wanted |= set(reached.scalars())
    if not wanted:
        return []

    rows = await session.execute(
        select(LearningPath).where(LearningPath.id.in_(wanted), LearningPath.is_published.is_(True))
    )
    paths = list(rows.scalars())

    enrollments = await enrollments_for(
        session, user_id, {item.program_id for path in paths for item in path.items}
    )

    out = [(path, item_states(path.items, enrollments), started.get(path.id)) for path in paths]
    # Started first, then furthest along, then curation order.
    out.sort(
        key=lambda row: (
            0 if row[2] is not None else 1,
            -progress_of(row[1], row[2]).percent,
            row[0].order_index,
        )
    )
    return out
