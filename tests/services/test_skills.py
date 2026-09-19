"""The skill layer: one vocabulary, and evidence that means what it says.

The promises pinned here are the ones every later feature will lean on.
Spellings and languages fold together, so a woman is matched on the skill and
not on the word. Evidence is idempotent, so a backfill cannot double-count.
And the rule the whole system turns on: **finishing a course is not a verified
skill** — only a person or a result from outside the platform verifies.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EnrollmentStatus,
    EvidenceKind,
    Language,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    ScoreDimension,
    SkillCategory,
    SkillStatus,
)
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.services import skills


async def _user(session: AsyncSession) -> uuid.UUID:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user.id


async def _skill(
    session: AsyncSession,
    slug: str,
    *,
    aliases: list[str],
    names: tuple[str, str, str] = ("Buxgalteriya", "Бухгалтерия", "Accounting"),
    dimensions: list[ScoreDimension] | None = None,
) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": names[0], "ru": names[1], "en": names[2]},
        category=SkillCategory.PROFESSIONAL,
        dimensions=[d.value for d in dimensions or []],
        aliases=[skills.normalise(alias) for alias in aliases],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _program(session: AsyncSession, *, taught: list[str]) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "ru": "Курс", "en": "Course"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=taught,
        is_published=True,
    )
    session.add(program)
    await session.flush()
    return program


async def _enrollment(
    session: AsyncSession,
    user_id: uuid.UUID,
    program: Program,
    *,
    status: EnrollmentStatus,
    progress: int = 0,
) -> Enrollment:
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=status,
        progress_percent=progress,
        completed_at=datetime.now(UTC) if status is EnrollmentStatus.COMPLETED else None,
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


async def _state(session: AsyncSession, user_id: uuid.UUID, slug: str) -> UserSkill | None:
    return await session.scalar(
        select(UserSkill)
        .join(Skill, Skill.id == UserSkill.skill_id)
        .where(UserSkill.user_id == user_id, Skill.slug == slug)
    )


# --- the vocabulary ---------------------------------------------------------


def test_spelling_noise_is_folded_away():
    """The same word is written half a dozen ways; they are one skill."""
    assert skills.normalise("Jamgʻarma") == skills.normalise("jamgarma")
    assert skills.normalise("  Biznes-reja ") == skills.normalise("Biznes reja")
    assert skills.normalise("Excel") == "excel"


def test_a_slug_is_ascii_even_for_a_cyrillic_label():
    assert skills.slug_for("Бухгалтерия") == "buhgalteriya"
    assert skills.slug_for("Ish suhbati") == "ish-suhbati"
    # Nothing transliterable left, but the label still needs a stable id.
    assert skills.slug_for("★").startswith("skill-")


@pytest.mark.asyncio
async def test_one_skill_however_it_is_written(session):
    await _skill(session, "accounting", aliases=["buxgalteriya", "бухгалтерия", "accounting"])

    index = await skills.SkillIndex.load(session, ["Buxgalteriya", "Бухгалтерия", "accounting"])

    assert index.key("Buxgalteriya") == "accounting"
    assert index.key("Бухгалтерия") == "accounting"
    assert index.ref("Бухгалтерия").name_i18n["uz"] == "Buxgalteriya"


@pytest.mark.asyncio
async def test_an_unknown_label_is_still_shown_as_written(session):
    index = await skills.SkillIndex.load(session, ["macramé weaving"])

    ref = index.ref("macramé weaving")
    assert ref.slug is None
    assert ref.label == "macramé weaving"
    # It keys on itself, so matching still works before anyone curates it.
    assert index.key("Macramé Weaving") == index.key("macramé weaving")


@pytest.mark.asyncio
async def test_an_unseen_label_is_minted_once(session):
    first = await skills.ensure_skill(session, "Pazandachilik")
    again = await skills.ensure_skill(session, "pazandachilik")

    assert first.id == again.id
    assert first.is_curated is False
    assert first.slug == "pazandachilik"
    assert first.name_i18n["ru"] == "Pazandachilik"


# --- what evidence is worth -------------------------------------------------


@pytest.mark.asyncio
async def test_finishing_a_course_is_learned_never_verified(session):
    user_id = await _user(session)
    await _skill(session, "excel", aliases=["excel"])

    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )

    state = await _state(session, user_id, "excel")
    assert state.status is SkillStatus.LEARNED
    assert state.level is ProficiencyLevel.BEGINNER


@pytest.mark.asyncio
async def test_a_mentor_verifies_and_sets_the_level(session):
    user_id = await _user(session)
    skill = await _skill(session, "excel", aliases=["excel"])

    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.MENTOR_ASSESSMENT,
        source_type="mentor_session",
        source_id=str(uuid.uuid4()),
        level=ProficiencyLevel.ADVANCED,
    )

    state = await _state(session, user_id, "excel")
    assert state.status is SkillStatus.VERIFIED
    assert state.level is ProficiencyLevel.ADVANCED


@pytest.mark.asyncio
async def test_the_strongest_evidence_decides_and_withdrawal_recomputes(session):
    user_id = await _user(session)
    skill = await _skill(session, "excel", aliases=["excel"])
    mentor_source = str(uuid.uuid4())

    await skills.sync_self_reported(session, user_id, ["Excel"])
    assert (await _state(session, user_id, "excel")).status is SkillStatus.SELF_REPORTED

    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    assert (await _state(session, user_id, "excel")).status is SkillStatus.LEARNED

    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.MENTOR_ASSESSMENT,
        source_type="mentor_session",
        source_id=mentor_source,
    )
    assert (await _state(session, user_id, "excel")).status is SkillStatus.VERIFIED

    # A verification withdrawn falls back to what is left standing, and the
    # withdrawn row stays on the record.
    await skills.revoke_evidence(
        session,
        user_id=user_id,
        kind=EvidenceKind.MENTOR_ASSESSMENT,
        source_type="mentor_session",
        source_id=mentor_source,
    )
    assert (await _state(session, user_id, "excel")).status is SkillStatus.LEARNED
    kept = await session.execute(select(SkillEvidence).where(SkillEvidence.revoked_at.is_not(None)))
    assert len(list(kept.scalars())) == 1


@pytest.mark.asyncio
async def test_the_same_completion_counts_once(session):
    user_id = await _user(session)
    await _skill(session, "excel", aliases=["excel"])
    enrollment_id = uuid.uuid4()

    for _ in range(3):
        await skills.record_course_completion(
            session, user_id=user_id, labels=["Excel"], enrollment_id=enrollment_id
        )

    state = await _state(session, user_id, "excel")
    assert len(state.evidence) == 1


# --- what she says about herself --------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_skill_from_the_profile_withdraws_only_her_own_claim(session):
    user_id = await _user(session)
    await _skill(session, "excel", aliases=["excel"])
    await _skill(session, "1c", aliases=["1c", "1с"], names=("1C", "1С", "1C"))

    await skills.sync_self_reported(session, user_id, ["Excel", "1C"])
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )

    await skills.sync_self_reported(session, user_id, [])

    # The course she finished is a fact about her; her own claim is not.
    assert (await _state(session, user_id, "excel")).status is SkillStatus.LEARNED
    assert (await _state(session, user_id, "1c")).status is None
    assert "1c" not in await skills.skill_keys(session, user_id)


@pytest.mark.asyncio
async def test_a_claim_written_in_another_language_lands_on_the_same_skill(session):
    user_id = await _user(session)
    await _skill(session, "accounting", aliases=["buxgalteriya", "бухгалтерия"])

    await skills.sync_self_reported(session, user_id, ["Бухгалтерия"])

    assert await skills.skill_keys(session, user_id) == {"accounting"}


# --- reading it back --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_profile_names_the_course_and_shows_what_is_in_progress(session):
    user_id = await _user(session)
    await _skill(session, "excel", aliases=["excel"])
    await _skill(session, "1c", aliases=["1c"], names=("1C", "1С", "1C"))
    finished = await _program(session, taught=["Excel"])
    studying = await _program(session, taught=["1C"])
    done = await _enrollment(session, user_id, finished, status=EnrollmentStatus.COMPLETED)
    await _enrollment(session, user_id, studying, status=EnrollmentStatus.IN_PROGRESS, progress=40)

    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=done.id
    )
    await skills.sync_self_reported(session, user_id, ["1C"])

    profile = {item.skill.slug: item for item in await skills.skill_profile(session, user_id)}

    excel = profile["excel"]
    assert excel.status is SkillStatus.LEARNED
    assert excel.evidence_count == 1
    assert excel.evidence[0].title_i18n["en"] == "Course"
    assert excel.evidence[0].supports is SkillStatus.LEARNED
    # The course she is part-way through is progress, not evidence.
    assert profile["1c"].progress_percent == 40
    assert profile["1c"].status is SkillStatus.SELF_REPORTED


@pytest.mark.asyncio
async def test_a_certificate_is_recorded_against_what_it_covers(session):
    user_id = await _user(session)
    await _skill(session, "excel", aliases=["excel"])
    program = await _program(session, taught=["Excel"])
    enrollment = await _enrollment(session, user_id, program, status=EnrollmentStatus.COMPLETED)
    certificate = Certificate(
        enrollment_id=enrollment.id,
        user_id=user_id,
        serial_number=f"WU-{uuid.uuid4().hex[:8]}",
        issued_at=datetime.now(UTC),
        verification_code=uuid.uuid4().hex,
    )
    session.add(certificate)
    await session.flush()

    await skills.record_certificate(
        session, user_id=user_id, labels=["Excel"], certificate_id=certificate.id
    )

    [item] = await skills.skill_profile(session, user_id)
    assert item.evidence[0].kind is EvidenceKind.CERTIFICATE
    assert item.evidence[0].title_i18n["uz"] == "Kurs"
    # A certificate says she was taught it, not that anyone tested her.
    assert item.status is SkillStatus.LEARNED


@pytest.mark.asyncio
async def test_a_profile_row_is_not_needed_for_skills_to_work(session):
    """Nothing here depends on the legacy free-text column being populated."""
    user_id = await _user(session)
    session.add(Profile(user_id=user_id, skills=[]))
    await session.flush()

    await skills.record_course_completion(
        session, user_id=user_id, labels=["Pazandachilik"], enrollment_id=uuid.uuid4()
    )

    assert await skills.skill_keys(session, user_id) == {"pazandachilik"}
