"""Finishing a course closes the roadmap step that *is* that course.

A plan step bound to a programme carries no information the platform does not
already hold: the enrollment knows which modules are ticked and closes itself at
a hundred per cent. Making her then open the roadmap and confirm it a second
time asks her to restate a fact the system just recorded, and leaves the step
looking outstanding until she does.

What these tests pin down is the *narrowness* of that automation, because an
automatic status change is only safe while it cannot overwrite a decision she
made: it touches steps in the active plan only, bound to that one programme
only, and never moves a step she has already closed or skipped.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    GoalHorizon,
    Language,
    PlanItemStatus,
    ProgramCategory,
    ProgramFormat,
)
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.program import Program
from app.models.user import User
from app.services.plan_service import close_plan_items_for_program


async def _user(session: AsyncSession) -> uuid.UUID:
    """Plans hang off a real user row, so the foreign key needs one."""
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user.id


async def _program(session: AsyncSession) -> uuid.UUID:
    """A step's programme is a foreign key, so it needs a real programme."""
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs"},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={"uz": "Tavsif"},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        duration_hours=10,
        duration_weeks=2,
        learning_outcomes=[{"uz": "a"}],
        skills_taught=["test"],
        is_published=True,
    )
    session.add(program)
    await session.flush()
    return program.id


async def _plan(session: AsyncSession, user_id: uuid.UUID, *, active: bool) -> DevelopmentPlan:
    plan = DevelopmentPlan(
        user_id=user_id,
        horizon=GoalHorizon.M6,
        title="Roadmap",
        is_active=active,
        generated_by_ai=False,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _item(
    session: AsyncSession,
    plan: DevelopmentPlan,
    *,
    program_id: uuid.UUID | None,
    status: PlanItemStatus = PlanItemStatus.PLANNED,
    order: int = 0,
) -> PlanItem:
    item = PlanItem(
        plan_id=plan.id,
        order_index=order,
        action="Finish the course",
        program_id=program_id,
        status=status,
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.asyncio
async def test_the_step_for_that_course_is_closed(session: AsyncSession) -> None:
    user_id, program_id = await _user(session), await _program(session)
    plan = await _plan(session, user_id, active=True)
    item = await _item(session, plan, program_id=program_id)

    changed = await close_plan_items_for_program(session, user_id, program_id)

    assert [c.id for c in changed] == [item.id]
    assert item.status == PlanItemStatus.DONE
    assert item.completed_at is not None


@pytest.mark.asyncio
async def test_a_step_with_no_programme_is_left_alone(session: AsyncSession) -> None:
    """Nothing in the system knows whether she updated her CV, so that stays
    hers to mark. Only steps that are courses can be closed by a course."""
    user_id, program_id = await _user(session), await _program(session)
    plan = await _plan(session, user_id, active=True)
    free = await _item(session, plan, program_id=None)

    await close_plan_items_for_program(session, user_id, program_id)

    assert free.status == PlanItemStatus.PLANNED


@pytest.mark.asyncio
async def test_other_programmes_are_untouched(session: AsyncSession) -> None:
    user_id = await _user(session)
    finished, other = await _program(session), await _program(session)
    plan = await _plan(session, user_id, active=True)
    a = await _item(session, plan, program_id=finished, order=0)
    b = await _item(session, plan, program_id=other, order=1)

    await close_plan_items_for_program(session, user_id, finished)

    assert a.status == PlanItemStatus.DONE
    assert b.status == PlanItemStatus.PLANNED


@pytest.mark.asyncio
async def test_a_skipped_step_stays_skipped(session: AsyncSession) -> None:
    """Skipping is a decision she made when accepting the plan. Completing the
    course must not quietly reverse it and put the step back in her roadmap."""
    user_id, program_id = await _user(session), await _program(session)
    plan = await _plan(session, user_id, active=True)
    skipped = await _item(session, plan, program_id=program_id, status=PlanItemStatus.SKIPPED)

    changed = await close_plan_items_for_program(session, user_id, program_id)

    assert changed == []
    assert skipped.status == PlanItemStatus.SKIPPED


@pytest.mark.asyncio
async def test_an_inactive_plan_is_not_revived(session: AsyncSession) -> None:
    """Superseded roadmaps are history. Writing into one would change a record
    of what she was working on at the time."""
    user_id, program_id = await _user(session), await _program(session)
    old = await _plan(session, user_id, active=False)
    item = await _item(session, old, program_id=program_id)

    changed = await close_plan_items_for_program(session, user_id, program_id)

    assert changed == []
    assert item.status == PlanItemStatus.PLANNED


@pytest.mark.asyncio
async def test_another_womans_plan_is_never_touched(session: AsyncSession) -> None:
    her, someone_else = await _user(session), await _user(session)
    program_id = await _program(session)
    hers = await _plan(session, her, active=True)
    theirs = await _plan(session, someone_else, active=True)
    mine = await _item(session, hers, program_id=program_id)
    yours = await _item(session, theirs, program_id=program_id)

    await close_plan_items_for_program(session, her, program_id)

    assert mine.status == PlanItemStatus.DONE
    assert yours.status == PlanItemStatus.PLANNED


@pytest.mark.asyncio
async def test_the_completion_time_is_not_rewritten(session: AsyncSession) -> None:
    """Re-running it must be a no-op, because module progress can be toggled and
    the endpoint may reach the hundred-per-cent branch more than once."""
    user_id, program_id = await _user(session), await _program(session)
    plan = await _plan(session, user_id, active=True)
    item = await _item(session, plan, program_id=program_id)

    await close_plan_items_for_program(session, user_id, program_id)
    first = item.completed_at
    await close_plan_items_for_program(session, user_id, program_id)

    assert item.completed_at == first
