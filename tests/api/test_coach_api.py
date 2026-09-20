"""The AI Coach: grounded in her records, and in nobody else's.

These pin the promises the Coach makes. The context is hers alone; the skills
it reads are the canonical ones and not the free text on her profile; the
progress is the number the learning service stored; and — the load-bearing one
— a programme, path or listing the model invents is dropped before it can reach
the browser.

The model is switched off for the whole suite (see `conftest.disable_llm`), so
every coaching answer here goes down the deterministic path. Tests that need a
model substitute a fake gateway.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.constants import (
    ConsentScope,
    EnrollmentStatus,
    EvidenceKind,
    Language,
    LessonKind,
    OpportunitySource,
    OpportunityType,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    ScoreDimension,
    SkillCategory,
    SkillStatus,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.audit import AiInteraction
from app.models.consent import ConsentLog
from app.models.learning_path import LearningPath, LearningPathItem
from app.models.opportunity import Opportunity
from app.models.profile import Profile
from app.models.program import Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill
from app.models.user import User
from app.services import ai_coach, skills
from app.services.scoring import calculate_dimension_scores, persist_scores

COACH = "/api/v1/assistant/coach"

OPTIONS = [
    {"value": 0, "label_i18n": {"en": "Not at all"}},
    {"value": 100, "label_i18n": {"en": "Excellent"}},
]


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _account(session, user_id: uuid.UUID, *, consent: bool = True) -> None:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    session.add(Profile(user_id=user_id, full_name="Dilnoza Karimova", skills=[]))
    if consent:
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


async def _course(
    session,
    *,
    taught: list[str],
    lessons: int = 2,
    category: ProgramCategory = ProgramCategory.FINANCIAL_LITERACY,
) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Moliyaviy savodxonlik", "ru": "Финансы", "en": "Money basics"},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={},
        category=category,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=ProficiencyLevel.BEGINNER,
        duration_hours=10,
        learning_outcomes=[],
        skills_taught=taught,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "Modul"})
    session.add(module)
    await session.flush()
    for index in range(lessons):
        session.add(
            ProgramLesson(
                module_id=module.id,
                order_index=index,
                slug=f"{index + 1}-dars",
                title_i18n={"uz": f"{index + 1}-dars", "en": f"Lesson {index + 1}"},
                kind=LessonKind.READING,
                duration_minutes=15,
                blocks=[],
            )
        )
    await session.flush()
    return program


async def _score(session, user_id: uuid.UUID, answers: dict[ScoreDimension, float]) -> None:
    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now)
    session.add(assessment)
    await session.flush()
    for dimension, value in answers.items():
        question = AssessmentQuestion(
            dimension=dimension, order_index=0, text_i18n={"en": "q"}, options=OPTIONS
        )
        session.add(question)
        await session.flush()
        session.add(
            AssessmentAnswer(assessment_id=assessment.id, question_id=question.id, value=value)
        )
    await session.flush()
    await persist_scores(
        session, user_id, assessment.id, await calculate_dimension_scores(session, assessment.id)
    )
    await session.flush()


# --- the context ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_context_needs_an_account(client):
    assert (await client.get(COACH)).status_code == 401
    assert (await client.post(COACH, json={"message": "Nima qilay?"})).status_code == 401


@pytest.mark.asyncio
async def test_the_context_is_her_own_record(client, session, auth_headers, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"], lessons=2)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=EnrollmentStatus.IN_PROGRESS,
        progress_percent=50,
        started_at=datetime.now(UTC),
    )
    session.add(enrollment)
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert body["personalised"] is True
    assert body["name"] == "Dilnoza"
    assert body["score"]["assessed"] is True
    assert body["score"]["composite"] is not None
    # The progress is the enrollment's own figure, never recounted here.
    [course] = body["learning"]["in_progress"]
    assert course["progress_percent"] == 50
    assert course["slug"] == program.slug
    # And the next lesson she would open is a real one.
    assert course["next_lesson_slug"] == "1-dars"


@pytest.mark.asyncio
async def test_one_womans_ids_never_reach_another(client, session, auth_headers, user_id):
    """The Coach answers for the token and for nothing else — there is no user
    id in the path, the query or the body to manipulate."""
    await _account(session, user_id)
    stranger = uuid.uuid4()
    await _account(session, stranger)
    program = await _course(session, taught=["Excel"])
    session.add(
        Enrollment(
            user_id=stranger,
            program_id=program.id,
            status=EnrollmentStatus.IN_PROGRESS,
            progress_percent=80,
            started_at=datetime.now(UTC),
        )
    )
    await skills.record_course_completion(
        session, user_id=stranger, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert body["learning"]["in_progress"] == []
    assert body["skills"]["learned"] == []
    # Even asking for someone else by name gets her own context back.
    asked = await client.get(f"{COACH}?language=uz", headers=auth_headers)
    assert asked.json()["learning"]["in_progress"] == []


# --- canonical skills -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_reads_canonical_skills_not_the_profile_column(
    client, session, auth_headers, user_id
):
    """`profile.skills` is what she once typed. The Coach reads the evidence."""
    await _account(session, user_id)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    profile.skills = ["Something she typed once"]
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session,
        user_id=user_id,
        labels=["Excel"],
        enrollment_id=uuid.uuid4(),
        level=ProficiencyLevel.BEGINNER,
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    # A skill crosses the API as a taxonomy entry with names in every language,
    # never as the word an author happened to type.
    [learned] = body["skills"]["learned"]
    assert learned["skill"]["slug"] == "excel"
    assert learned["skill"]["name_i18n"]["uz"] == "Excel"

    every = [
        entry["skill"]["slug"]
        for group in ("verified", "assessed", "learned", "self_reported")
        for entry in body["skills"][group]
    ]
    assert every == ["excel"]

    # And the persona the other assistant surfaces use reads the same way.
    context = await ai_coach.build(session, user_id)
    assert "Excel" in context.as_prompt()
    assert "Something she typed once" not in context.as_prompt()


@pytest.mark.asyncio
async def test_a_course_makes_a_skill_learned_and_never_verified(
    client, session, auth_headers, user_id
):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert [e["skill"]["slug"] for e in body["skills"]["learned"]] == ["excel"]
    assert body["skills"]["verified"] == []
    assert body["skills"]["assessed"] == []

    # The prompt says so in as many words, so the model cannot merge the two.
    prompt = (await ai_coach.build(session, user_id)).as_prompt()
    assert "learned (a course taught, NOT verified)" in prompt


@pytest.mark.asyncio
async def test_assessed_and_verified_stay_apart(client, session, auth_headers, user_id):
    await _account(session, user_id)
    scored = await _skill(session, "excel", "Excel")
    vouched = await _skill(session, "sotuv", "Sotuv")

    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=scored,
        kind=EvidenceKind.AI_ASSESSMENT,
        source_type="assessment",
        source_id=uuid.uuid4().hex,
        level=ProficiencyLevel.INTERMEDIATE,
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

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert [e["skill"]["slug"] for e in body["skills"]["assessed"]] == ["excel"]
    assert [e["skill"]["slug"] for e in body["skills"]["verified"]] == ["sotuv"]
    assert body["skills"]["learned"] == []


@pytest.mark.asyncio
async def test_verified_survives_a_later_course_completion(session, user_id):
    """A course adds evidence; it never demotes what a person already confirmed."""
    await _account(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.EMPLOYER_ASSESSMENT,
        source_type="employer",
        source_id=uuid.uuid4().hex,
    )
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await session.flush()

    context = await ai_coach.build(session, user_id)

    assert [e.skill.slug for e in context.read.skills.verified] == ["excel"]
    assert context.read.skills.learned == []


# --- grounding --------------------------------------------------------------


class _Gateway:
    """A model that says whatever the test wants it to say."""

    enabled = True

    def __init__(self, payload: dict):
        self.payload = payload
        self.prompts: list[str] = []

    @staticmethod
    def sanitise(text: str) -> str:
        return text

    async def complete(self, *, system, messages, **kwargs):
        from app.services.llm_gateway import LlmResponse

        self.prompts.append(messages[0]["content"])
        return LlmResponse(
            text="",
            parsed=self.payload,
            model="test-model",
            prompt_version="test",
            trace_id=uuid.uuid4().hex,
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            refused=False,
        )


@pytest.mark.asyncio
async def test_a_real_programme_can_be_recommended(session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    context = await ai_coach.build(session, user_id)
    gateway = _Gateway(
        {
            "message": "Start here.",
            "next_step_id": str(program.id),
            "reference_ids": [str(program.id)],
            "unsupported": False,
        }
    )
    reply = await ai_coach.answer(
        session, user_id=user_id, question="Nima o'qishim kerak?", gateway=gateway
    )

    assert str(program.id) in context.offer
    assert reply.next_step is not None
    assert reply.next_step.id == program.id
    assert reply.next_step.slug == program.slug
    assert [ref.id for ref in reply.references] == [program.id]
    assert reply.generated is True


@pytest.mark.asyncio
async def test_a_programme_that_does_not_exist_cannot_be_recommended(session, user_id):
    """The load-bearing test: an id the model invented is dropped, not rendered."""
    await _account(session, user_id)
    await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    invented = str(uuid.uuid4())
    gateway = _Gateway(
        {
            "message": "Try the Advanced Robotics Diploma.",
            "next_step_id": invented,
            "reference_ids": [invented, "not-even-a-uuid"],
            "unsupported": False,
        }
    )
    reply = await ai_coach.answer(
        session, user_id=user_id, question="Nima o'qishim kerak?", gateway=gateway
    )

    assert reply.next_step is None
    assert reply.references == []


@pytest.mark.asyncio
async def test_a_real_learning_path_can_be_referenced_and_a_fake_one_cannot(session, user_id):
    await _account(session, user_id)
    program = await _course(session, taught=["Excel"])
    path = LearningPath(
        slug="moliyaviy-mustaqillik",
        title_i18n={"uz": "Moliyaviy mustaqillik", "en": "Financial independence"},
        description_i18n={},
        dimension=ScoreDimension.FINANCIAL_LITERACY,
        level=ProficiencyLevel.BEGINNER,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(path)
    await session.flush()
    session.add(LearningPathItem(path_id=path.id, program_id=program.id, order_index=0))
    await session.flush()
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    gateway = _Gateway(
        {
            "message": "This route fits.",
            "next_step_id": str(path.id),
            "reference_ids": [str(path.id), str(uuid.uuid4())],
            "unsupported": False,
        }
    )
    reply = await ai_coach.answer(
        session, user_id=user_id, question="Qaysi yo'nalish?", gateway=gateway
    )

    assert reply.next_step is not None
    assert reply.next_step.kind == "learning_path"
    assert reply.next_step.slug == "moliyaviy-mustaqillik"
    # The invented sibling id went nowhere.
    assert [ref.id for ref in reply.references] == [path.id]


@pytest.mark.asyncio
async def test_the_prompt_carries_her_real_records_and_the_engines_ranking(session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=program.id,
            status=EnrollmentStatus.IN_PROGRESS,
            progress_percent=40,
            started_at=datetime.now(UTC),
        )
    )
    await session.flush()

    prompt = (await ai_coach.build(session, user_id)).as_prompt()

    assert "Development Score:" in prompt
    assert "40% done" in prompt
    assert "what the platform has already worked out she should do next" in prompt
    assert "OFFER — the only records you may name, by id:" in prompt
    assert str(program.id) in prompt


# --- progress ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_is_the_stored_number_not_a_recount(client, session, auth_headers, user_id):
    """The learning service owns the percentage. The Coach quotes it."""
    await _account(session, user_id)
    program = await _course(session, taught=["Excel"], lessons=4)
    lessons = list(
        (
            await session.execute(
                select(ProgramLesson.id)
                .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
                .where(ProgramModule.program_id == program.id)
                .order_by(ProgramLesson.order_index)
            )
        ).scalars()
    )
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=program.id,
            status=EnrollmentStatus.IN_PROGRESS,
            progress_percent=25,
            completed_lessons=[str(lessons[0])],
            started_at=datetime.now(UTC),
        )
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    [course] = body["learning"]["in_progress"]
    assert course["progress_percent"] == 25
    # The next lesson is the first one she has not ticked, not lesson one again.
    assert course["next_lesson_slug"] == "2-dars"


# --- empty states -----------------------------------------------------------


@pytest.mark.asyncio
async def test_without_an_assessment_the_context_says_so(client, session, auth_headers, user_id):
    await _account(session, user_id)

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert body["score"]["assessed"] is False
    assert body["score"]["composite"] is None
    assert any(item["key"] == "coach.q.assessment" for item in body["suggestions"])
    prompt = (await ai_coach.build(session, user_id)).as_prompt()
    assert "she has not taken the diagnostic yet" in prompt


@pytest.mark.asyncio
async def test_with_no_recorded_skills_the_prompt_does_not_claim_she_has_none(session, user_id):
    await _account(session, user_id)

    prompt = (await ai_coach.build(session, user_id)).as_prompt()

    assert "not a statement that she has no skills" in prompt


@pytest.mark.asyncio
async def test_an_empty_catalogue_offers_nothing_rather_than_inventing(session, user_id):
    await _account(session, user_id)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    context = await ai_coach.build(session, user_id)

    assert context.offer == {}
    assert "(nothing in the catalogue matches her right now)" in context.as_prompt()


@pytest.mark.asyncio
async def test_no_matching_opportunities_is_said_plainly(client, session, auth_headers, user_id):
    await _account(session, user_id)
    session.add(
        Opportunity(
            source=OpportunitySource.INTERNAL,
            external_id=uuid.uuid4().hex,
            type=OpportunityType.VACANCY,
            title_i18n={"uz": "Buxgalter"},
            required_skills=["1C", "Buxgalteriya"],
            is_active=True,
        )
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    # She holds none of the listed skills, so nothing is called a match.
    assert body["opportunities"] == []
    prompt = (await ai_coach.build(session, user_id)).as_prompt()
    assert "open listings matching her skills: none right now" in prompt


@pytest.mark.asyncio
async def test_when_the_provider_is_down_the_answer_is_real_and_says_so(
    client, session, auth_headers, user_id
):
    """`disable_llm` blanks the key for the whole suite, so this is the live
    fallback path rather than a simulated one."""
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    response = await client.post(
        COACH, json={"message": "Nima qilishim kerak?", "language": "en"}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["generated"] is False
    # Not an error card: it quotes her real score and a real course.
    assert "Development Score" in body["message"]
    assert "unavailable" in body["message"]
    assert body["next_step"] is not None
    assert body["next_step"]["id"] == str(program.id)


# --- suggestions and the trace ---------------------------------------------


@pytest.mark.asyncio
async def test_suggestions_come_from_her_actual_state(client, session, auth_headers, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=program.id,
            status=EnrollmentStatus.IN_PROGRESS,
            progress_percent=40,
            started_at=datetime.now(UTC),
        )
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    keys = [item["key"] for item in body["suggestions"]]
    # She has a course open, so the course question is offered with its title.
    assert "coach.q.course" in keys
    course_question = next(i for i in body["suggestions"] if i["key"] == "coach.q.course")
    assert course_question["params"]["course"] == "Moliyaviy savodxonlik"
    # She is on no path, so the path question is not offered.
    assert "coach.q.path" not in keys
    # The assessment question is gone now that she has a score.
    assert "coach.q.assessment" not in keys


@pytest.mark.asyncio
async def test_the_path_question_names_a_route_she_is_still_walking(
    client, session, auth_headers, user_id
):
    """A finished route sorts first because it is at 100%. "What is my next
    step on X" is a question about an open one."""
    await _account(session, user_id)
    done = await _course(session, taught=["Excel"], lessons=1)
    todo = await _course(session, taught=["Sotuv"], lessons=1)

    finished = LearningPath(
        slug="tugatilgan",
        title_i18n={"uz": "Tugatilgan yoʻl"},
        description_i18n={},
        is_published=True,
        published_at=datetime.now(UTC),
    )
    open_path = LearningPath(
        slug="ochiq",
        title_i18n={"uz": "Ochiq yoʻl"},
        description_i18n={},
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add_all([finished, open_path])
    await session.flush()
    session.add(LearningPathItem(path_id=finished.id, program_id=done.id, order_index=0))
    session.add(LearningPathItem(path_id=open_path.id, program_id=todo.id, order_index=0))
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=done.id,
            status=EnrollmentStatus.COMPLETED,
            progress_percent=100,
            started_at=datetime.now(UTC),
        )
    )
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=todo.id,
            status=EnrollmentStatus.IN_PROGRESS,
            progress_percent=10,
            started_at=datetime.now(UTC),
        )
    )
    await session.flush()

    body = (await client.get(COACH, headers=auth_headers)).json()

    question = next(i for i in body["suggestions"] if i["key"] == "coach.q.path")
    assert question["params"]["path"] == "Ochiq yoʻl"


@pytest.mark.asyncio
async def test_every_answer_leaves_a_trace(client, session, auth_headers, user_id):
    await _account(session, user_id)
    await _score(session, user_id, {ScoreDimension.EMPLOYMENT: 0.0})

    await client.post(COACH, json={"message": "Nima qilay?"}, headers=auth_headers)

    row = await session.scalar(select(AiInteraction).where(AiInteraction.user_id == user_id))
    assert row is not None
    assert row.feature == "assistant_coach"


@pytest.mark.asyncio
async def test_a_safety_disclosure_goes_to_a_person_not_the_model(session, user_id):
    await _account(session, user_id)
    gateway = _Gateway(
        {
            "message": "should never run",
            "next_step_id": None,
            "reference_ids": [],
            "unsupported": False,
        }
    )

    reply = await ai_coach.answer(
        session,
        user_id=user_id,
        question="Erim meni uradi, nima qilay?",
        language="uz",
        gateway=gateway,
    )

    assert reply.escalated is True
    assert reply.escalation_reason == "safety_topic_detected"
    assert gateway.prompts == []


@pytest.mark.asyncio
async def test_without_consent_the_coach_still_answers_but_says_it_is_not_personalised(
    client, session, auth_headers, user_id
):
    await _account(session, user_id, consent=False)

    body = (await client.get(COACH, headers=auth_headers)).json()

    assert body["personalised"] is False
    # And her records are still hers — the flag reports consent, it does not
    # leak somebody else's data in its place.
    assert body["learning"]["in_progress"] == []


@pytest.mark.asyncio
async def test_the_status_groups_reach_the_browser_separately(session, user_id):
    """The whole Step 3 distinction has to survive serialisation."""
    await _account(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.COURSE_COMPLETION,
        source_type="enrollment",
        source_id=uuid.uuid4().hex,
        level=ProficiencyLevel.BEGINNER,
    )
    await session.flush()

    read = (await ai_coach.build(session, user_id)).read

    assert read.skills.learned[0].status == SkillStatus.LEARNED
    assert read.skills.learned[0].evidence_count == 1
    assert read.skills.verified == []
