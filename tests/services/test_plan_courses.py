"""The plan names real courses from the catalogue, chosen for her.

It used to take the first sixty published rows in whatever order Postgres
returned them — out of some eight hundred — and then the first one in the right
category, so the plan was a lottery and two weak dimensions that share a
category got the same course twice. What is pinned here: what she told the
questionnaire she wants to learn is lifted to the top, no course fills two
steps, and a step bound to someone else's course carries the link to it.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import Language, ProgramCategory, ProgramFormat, ScoreDimension
from app.models.program import Program
from app.models.user import User
from app.schemas.plan import PlanRead
from app.services import learning_profile
from app.services.plan_service import _interest_terms, _relevance, generate_plan


class _Offline:
    """The model is down: the rule-based path is the one under test."""

    enabled = False


def _course(title: str, *, url: str | None = None) -> Program:
    return Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": title},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={"uz": "Tavsif"},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=[],
        external_url=url,
        source="stepik" if url else None,
        is_published=True,
    )


def test_her_words_become_stems_that_find_their_forms():
    stems = _interest_terms({"target_field": "Dizayn va marketing", "interests": ["Excel"]})
    # "va" is too short to mean anything and would match every title.
    assert stems == ["dizay", "marke", "excel"]
    assert _relevance(_course("Grafik dizayner kasbi"), stems) == 1
    assert _relevance(_course("Kurs"), stems) == 0


def test_nothing_said_means_nothing_is_lifted():
    assert _interest_terms({}) == []
    assert _relevance(_course("Dizayn"), []) == 0


async def test_the_course_she_asked_for_comes_first_and_none_repeats(
    session: AsyncSession,
) -> None:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    other = _course("Kurs")
    wanted = _course("Buxgalteriya asoslari", url="https://stepik.org/course/1")
    session.add_all([user, other, wanted])
    await session.flush()
    await learning_profile.save(session, user.id, {"target_field": "Buxgalteriya"})

    # Both dimensions are built by vocational courses, so both steps draw on
    # the same pool — the case that used to name one course twice.
    plan = await generate_plan(
        session,
        user_id=user.id,
        focus_dimensions=[ScoreDimension.EDUCATION_SKILLS, ScoreDimension.EMPLOYMENT],
        language="uz",
        gateway=_Offline(),  # type: ignore[arg-type]
    )

    items = sorted(plan.items, key=lambda item: item.order_index)
    assert items[0].program_id == wanted.id
    assert items[1].program_id is not None
    assert items[1].program_id != items[0].program_id

    read = PlanRead.model_validate(plan)
    first = next(item for item in read.items if item.program_id == wanted.id)
    assert first.program_url == "https://stepik.org/course/1"
    # The portal's own course has no site of its own; the card is the way in.
    assert all(item.program_url is None for item in read.items if item.program_id == other.id)
