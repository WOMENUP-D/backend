"""The AI layer reads the canonical skill system, not `profile.skills`.

`profile.skills` is what a woman once typed about herself. It is one kind of
evidence and the weakest of them, and an assistant that treats it as the truth
will tell her she can do things nobody has checked — and will miss what a
course actually taught her. These pin the migration: the persona every
assistant surface runs on, and the roadmap prompt, both read `user_skills`.

The column itself is untouched. It is still what she typed, it is still what
`sync_self_reported` mirrors into the canonical layer, and the partner payload
still sends it. Only the reasoning moved.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.core.constants import (
    ConsentScope,
    EvidenceKind,
    GoalHorizon,
    Language,
    ProficiencyLevel,
    ScoreDimension,
    SkillCategory,
)
from app.models.consent import ConsentLog
from app.models.profile import Profile
from app.models.skill import Skill
from app.models.user import User
from app.services import ai_assistant, skills
from app.services.plan_service import _build_prompt


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _woman(session, user_id: uuid.UUID, *, typed: list[str]) -> None:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    session.add(Profile(user_id=user_id, full_name="Dilnoza", skills=typed))
    session.add(
        ConsentLog(
            user_id=user_id,
            scope=ConsentScope.AI_PERSONALISATION,
            accepted=True,
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _skill(session, slug: str, label: str) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.DIGITAL,
        dimensions=[],
        aliases=[slug, label.lower()],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


@pytest.mark.asyncio
async def test_the_persona_reads_evidence_and_not_the_typed_column(session, user_id):
    await _woman(session, user_id, typed=["Something she typed once"])
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session,
        user_id=user_id,
        labels=["Excel"],
        enrollment_id=uuid.uuid4(),
        level=ProficiencyLevel.BEGINNER,
    )
    await session.flush()

    persona = await ai_assistant.build_persona(session, user_id)
    prompt = persona.as_prompt()

    assert [note.label for note in persona.skills] == ["Excel"]
    assert "Excel" in prompt
    assert "Something she typed once" not in prompt


@pytest.mark.asyncio
async def test_the_persona_tells_the_model_what_learned_and_verified_mean(session, user_id):
    await _woman(session, user_id, typed=[])
    taught = await _skill(session, "excel", "Excel")
    vouched = await _skill(session, "sotuv", "Sotuv")
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=taught,
        kind=EvidenceKind.COURSE_COMPLETION,
        source_type="enrollment",
        source_id=uuid.uuid4().hex,
        level=ProficiencyLevel.BEGINNER,
    )
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=vouched,
        kind=EvidenceKind.MENTOR_ASSESSMENT,
        source_type="mentor_session",
        source_id=uuid.uuid4().hex,
        level=ProficiencyLevel.ADVANCED,
    )
    await session.flush()

    prompt = (await ai_assistant.build_persona(session, user_id)).as_prompt()

    assert "Excel — learned — beginner" in prompt
    assert "Sotuv — verified — advanced" in prompt
    # And the rule is stated, not left to be inferred from two adjectives.
    assert "Finishing a course never" in prompt
    assert "makes a skill verified" in prompt


@pytest.mark.asyncio
async def test_a_woman_with_no_evidence_has_no_skill_line_rather_than_a_false_one(session, user_id):
    await _woman(session, user_id, typed=["Excel", "1C"])

    persona = await ai_assistant.build_persona(session, user_id)

    assert persona.skills == []
    assert "skills WomanUP records for her" not in persona.as_prompt()


@pytest.mark.asyncio
async def test_without_consent_the_persona_carries_no_skills_at_all(session, user_id):
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    session.add(Profile(user_id=user_id, skills=["Excel"]))
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await session.flush()

    persona = await ai_assistant.build_persona(session, user_id)

    assert persona.personalised is False
    assert persona.skills == []


def test_the_roadmap_prompt_carries_recorded_skills_with_their_status():
    """The plan is built on what the platform can evidence, not on free text."""
    profile = Profile(user_id=uuid.uuid4(), skills=["Something she typed once"])
    notes = [
        skills.SkillNote("Excel", None, None),
        skills.SkillNote("Sotuv", None, ProficiencyLevel.ADVANCED),
    ]

    prompt = _build_prompt(
        profile,
        {ScoreDimension.EMPLOYMENT: 30.0},
        [],
        [ScoreDimension.EMPLOYMENT],
        GoalHorizon.M6,
        [],
        "uz",
        notes,
    )

    assert "RECORDED SKILLS" in prompt
    assert "- Excel" in prompt
    assert "Sotuv — advanced" in prompt
    assert "Something she typed once" not in prompt


def test_the_roadmap_prompt_says_nothing_is_recorded_rather_than_nothing_exists():
    profile = Profile(user_id=uuid.uuid4(), skills=[])

    prompt = _build_prompt(
        profile,
        {},
        [],
        [],
        GoalHorizon.M6,
        [],
        "uz",
        [],
    )

    assert "(none recorded yet)" in prompt
