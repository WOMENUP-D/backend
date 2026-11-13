"""Courses from the diagnostic profile: the rules find and rank real courses,
the model only explains them, and the result is kept on the attempt.

What matters most: a need comes from her goal, her interests, her
difficulties or a weak dimension — never from nowhere; a skill she already
holds is not a gap; the catalogue is searched under every spelling the
taxonomy knows; the model can only point at a candidate by number; and when
the model is off, down or refuses, the rules answer and say so.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.constants import (
    EnrollmentStatus,
    Language,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    ScoreDimension,
)
from app.models.assessment import Assessment
from app.models.audit import AiInteraction
from app.models.program import Enrollment, Program
from app.models.user import User
from app.seed_skills import load_skills
from app.services import course_match as cm
from app.services.llm_gateway import LlmResponse, LlmUnavailableError

# ---------------------------------------------------------------------------
# Pure rules: needs, gaps, fit
# ---------------------------------------------------------------------------


def test_needs_come_from_goal_interest_and_difficulty_with_the_strongest_reason_kept():
    profile = {
        "goals": ["first_job", "international"],
        "interests": ["languages"],
        "difficulties": ["cv"],
    }
    needs = cm.needs_for(profile, [])
    by_slug = {n.slug: n for n in needs}
    assert by_slug["cv-writing"].why == "goal" and by_slug["cv-writing"].weight == 3.0
    # English is asked for by the second goal and by an interest; the goal wins
    # by weight only when it is the primary goal — here both are 2.0, the first
    # reason recorded stays.
    assert by_slug["english"].why in ("goal_other", "interest")
    assert needs[0].why == "goal"  # primary-goal skills come first
    assert all(n.why != "dimension" for n in needs)


def test_a_weak_dimension_adds_its_skills_at_the_lowest_weight():
    needs = cm.needs_for({"goals": ["improve_skills"]}, [ScoreDimension.EMPLOYMENT])
    assert {n.slug for n in needs} == set(cm.DIMENSION_FOUNDATIONS[ScoreDimension.EMPLOYMENT])
    assert all(n.why == "dimension" and n.weight == 1.0 for n in needs)


def test_for_a_direction_goal_her_interests_carry_the_goal_weight():
    switching = cm.needs_for({"goals": ["change_direction"], "interests": ["marketing_smm"]}, [])
    assert next(n for n in switching if n.slug == "smm").weight == cm.WEIGHT["goal"]
    first_job = cm.needs_for({"goals": ["first_job"], "interests": ["marketing_smm"]}, [])
    assert next(n for n in first_job if n.slug == "smm").weight == cm.WEIGHT["interest"]


def test_a_course_mostly_about_other_things_scores_lower():
    learner = _learner(goals=["first_job"])
    focused = _program("cv", ("резюме",))
    broad = _program("broad", ("резюме", "python", "статистика", "маркетинг"))
    covers = [cm.Need("cv-writing", 3.0, "goal", "first_job")]
    assert cm.score(focused, covers, 0, learner) > cm.score(broad, covers, 3, learner)


def test_a_field_rated_at_work_level_counts_as_held():
    held = cm.held_from({"skill_levels": {"english": 3, "it": 1}}, {"excel"})
    assert "english" in held and "excel" in held
    assert "programming" not in held


def test_gaps_are_needs_minus_held():
    needs = cm.needs_for({"goals": ["international"]}, [])
    gaps = cm.gaps_for(needs, {"english"})
    assert "english" not in {g.slug for g in gaps}
    assert "ielts" in {g.slug for g in gaps}


def test_career_wish_and_business_stage_add_goals():
    needs = cm.needs_for({"goals": ["improve_skills"], "career_wish": "own_business"}, [])
    assert "entrepreneurship" in {n.slug for n in needs}
    needs = cm.needs_for({"goals": ["start_business"], "business_stage": "idea"}, [])
    assert "business-plan" in {n.slug for n in needs}


def test_legacy_goal_codes_map_back_to_version_3_goals():
    assert cm.goals_from_legacy(["find_job", "business"]) == ["first_job", "start_business"]
    assert cm.goals_from_legacy(["nonsense"]) == []


def _program(slug: str, tags: tuple[str, ...], **extra) -> Program:
    values = {}
    values.update(
        slug=slug,
        title_i18n={"ru": slug.replace("-", " ").capitalize()},
        goal_i18n={},
        description_i18n={
            "ru": f"Курс о {slug}.",
            "uz": f"{slug} haqida kurs.",
            "en": f"A course on {slug}.",
        },
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.RU,
        skills_taught=list(tags),
        is_published=True,
        published_at=datetime.now(UTC),
        provider="Stepik",
        external_url=f"https://stepik.org/course/{slug}",
    )
    values.update(extra)
    return Program(**values)


def test_level_fit_refuses_an_advanced_course_to_a_beginner_and_vice_versa():
    advanced = _program("adv", ("english",), level=ProficiencyLevel.ADVANCED)
    starter = _program("start", ("english",), level=ProficiencyLevel.BEGINNER)
    assert cm.level_fits(advanced, 0) == -3
    assert cm.level_fits(starter, 4) == -3
    assert cm.level_fits(starter, 1) == 1
    assert cm.level_fits(_program("x", ("english",)), 2) == 0  # unknown level: neutral


def test_length_fit_follows_her_hours():
    short = _program("s", ("english",), duration_hours=10)
    long = _program("l", ("english",), duration_hours=200)
    assert cm.length_fits(short, "lt2") == 1
    assert cm.length_fits(long, "lt2") == -1
    assert cm.length_fits(long, "h10_plus") == 0


def test_the_rules_spread_their_courses_over_her_gaps():
    english = cm.Need("english", 3.0, "goal", "international")
    ielts = cm.Need("ielts", 3.0, "goal", "international")
    candidates = []
    for i in range(6):
        p = _program(f"en{i}", ("english",))
        p.id = uuid.uuid4()
        candidates.append(cm.Candidate(p, [english], 6.0 - i * 0.1, i + 1))
    exam = _program("ielts", ("ielts",))
    exam.id = uuid.uuid4()
    candidates.append(cm.Candidate(exam, [ielts], 4.0, 7))
    chosen = cm.spread(candidates, [english, ielts])
    assert [c.program.slug for c in chosen[:3]] == ["en0", "en1", "ielts"]
    assert len(chosen) == 7  # the rest fill the remaining places


def test_priority_follows_the_strongest_reason():
    goal = cm.Need("cv-writing", 3.0, "goal", "first_job")
    interest = cm.Need("english", 2.0, "interest", "languages")
    dim = cm.Need("excel", 1.0, "dimension", "digital_skills")
    p = _program("p", ("x",))
    assert cm.priority_of(cm.Candidate(p, [goal, dim], 0)) == "high"
    assert cm.priority_of(cm.Candidate(p, [interest], 0)) == "medium"
    assert cm.priority_of(cm.Candidate(p, [dim], 0)) == "additional"


# ---------------------------------------------------------------------------
# The model's answer, checked
# ---------------------------------------------------------------------------


def _learner(**profile) -> cm.Learner:
    return cm.Learner(
        user_id=uuid.uuid4(),
        profile=profile,
        language=Language.RU,
        held=set(),
        skill_levels={},
        weak_dimensions=[],
        finished=set(),
        started=set(),
        age=25,
        region=None,
        gaps=[cm.Need("cv-writing", 3.0, "goal", "first_job")],
    )


def _candidates(n: int) -> list[cm.Candidate]:
    out = []
    for i in range(1, n + 1):
        p = _program(f"c{i}", ("резюме",))
        p.id = uuid.uuid4()
        out.append(cm.Candidate(p, [cm.Need("cv-writing", 3.0, "goal", "first_job")], 5.0, i))
    return out


def _texts(prefix: str, text: str) -> dict:
    return {f"{prefix}_{lang}": text for lang in ("uz", "ru", "en")}


def _course(number: int, priority: str = "high", **override) -> dict:
    item = {
        "candidate": number,
        "priority": priority,
        **_texts("why", "why"),
        **_texts("outcome", "out"),
    }
    item.update(override)
    return item


def _payload(**override) -> dict:
    payload = {
        "summary": {
            **_texts("goal", "a first job"),
            **_texts("situation", "graduate"),
            "gaps_uz": ["rezyume"],
            "gaps_ru": ["резюме"],
            "gaps_en": ["cv"],
        },
        "courses": [_course(1), _course(2, "medium")],
        "roadmap": [
            {
                **_texts("title", "Foundation"),
                "items": [
                    {"kind": "course", "candidate": 1, "activity": "", **_texts("text", "")},
                    {
                        "kind": "activity",
                        "candidate": 0,
                        "activity": "cv",
                        **_texts("text", "Write a CV"),
                    },
                ],
            }
        ],
        "next_steps": [_texts("text", "Start course 1")],
        "follow_up": _texts("text", ""),
    }
    payload.update(override)
    return payload


def test_the_model_may_only_point_at_candidates_it_was_given():
    candidates = _candidates(2)
    parsed = cm.parse_model(
        _payload(courses=[_course(1), _course(7), _course(1), _course(2, "weird")]),
        candidates,
        {},
        _learner(),
    )
    assert parsed is not None and parsed.mode == "ai"
    numbers = [c["candidate"] for c in parsed.courses]
    assert numbers == [1, 2]  # 7 is not a candidate; the second 1 is a repeat
    assert parsed.courses[1]["priority"] == "high"  # an unknown priority falls back to the rules'
    assert parsed.courses[0]["program_id"] == str(candidates[0].program.id)


def test_an_empty_explanation_drops_the_course_and_no_courses_means_no_answer():
    candidates = _candidates(2)
    parsed = cm.parse_model(_payload(courses=[_course(1, why_uz="")]), candidates, {}, _learner())
    assert parsed is None


def test_roadmap_keeps_only_chosen_courses_and_full_activities():
    candidates = _candidates(3)
    payload = _payload(
        courses=[_course(1)],
        roadmap=[
            {
                **_texts("title", "Stage"),
                "items": [
                    {"kind": "course", "candidate": 1, "activity": "", **_texts("text", "")},
                    {"kind": "course", "candidate": 3, "activity": "", **_texts("text", "")},
                    {
                        "kind": "activity",
                        "candidate": 0,
                        "activity": "cv",
                        **_texts("text", "Write"),
                        "text_en": "",
                    },
                    {
                        "kind": "activity",
                        "candidate": 0,
                        "activity": "apply",
                        **_texts("text", "Apply"),
                    },
                ],
            },
            {
                **_texts("title", "Empty"),
                "items": [{"kind": "course", "candidate": 9, "activity": "", **_texts("text", "")}],
            },
        ],
    )
    parsed = cm.parse_model(payload, candidates, {}, _learner())
    assert parsed is not None
    assert len(parsed.roadmap) == 1  # the stage with nothing valid is gone
    kinds = [(i["kind"], i.get("activity")) for i in parsed.roadmap[0]["items"]]
    assert kinds == [("course", None), ("activity", "apply")]
    assert parsed.follow_up is None  # empty follow-up text means no question


# ---------------------------------------------------------------------------
# With the catalogue: candidates, ranking, the rules' words, the model's
# ---------------------------------------------------------------------------


class FakeGateway:
    enabled = True

    def __init__(self, payload: dict | None = None, *, refuse: bool = False, down: bool = False):
        self.payload = payload
        self.refuse = refuse
        self.down = down
        self.calls = 0
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    async def complete(self, **kwargs) -> LlmResponse:
        self.calls += 1
        self.prompts.append(kwargs["messages"][0]["content"])
        self.schemas.append(kwargs["json_schema"])
        if self.down:
            raise LlmUnavailableError("down")
        return LlmResponse(
            text=json.dumps(self.payload or {}),
            trace_id="trace-match",
            model="claude-test",
            prompt_version="2026.11.1",
            latency_ms=5,
            parsed=None if self.refuse else self.payload,
            refused=self.refuse,
        )


class OffGateway:
    enabled = False

    async def complete(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the model must not be called when it is off")


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _attempt(
    session, user_id: uuid.UUID, profile: dict | None, *, version: int = 3
) -> Assessment:
    attempt = Assessment(
        user_id=user_id,
        version=version,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        is_baseline=True,
        overall_score=50,
        dimension_scores={d.value: 60 for d in ScoreDimension},
        goals=[],
        profile=profile,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def _catalogue(session) -> dict[str, Program]:
    programs = {
        "cv_uz": _program("rezyume-kursi", ("Rezyume yozish", "ish suhbati"), language=Language.UZ),
        "cv_ru": _program("rezyume-kurs", ("резюме",), language=Language.RU),
        "english": _program("english-a1", ("Английский язык",), level=ProficiencyLevel.BEGINNER),
        "english_pro": _program("english-c1", ("english",), level=ProficiencyLevel.ADVANCED),
        "python": _program("python-start", ("python",)),
        "hidden": _program("rezyume-hidden", ("резюме",), is_published=False, published_at=None),
    }
    for program in programs.values():
        session.add(program)
    await session.flush()
    return programs


FIRST_JOB = {
    "version": 3,
    "situation": "graduate",
    "goal_primary": "first_job",
    "goals_other": [],
    "goals": ["first_job"],
    "interests": ["languages"],
    "skill_levels": {"english": 1},
    "difficulties": ["cv"],
    "formats": ["video"],
    "hours": "h2_5",
    "language": "ru",
}


@pytest.mark.asyncio
async def test_candidates_are_found_under_every_spelling_and_ranked_by_what_they_close(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    programs = await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)

    learner = await cm.load_learner(session, attempt)
    assert {g.slug for g in learner.gaps} >= {"cv-writing", "job-interviews", "english"}
    candidates = await cm.candidates_for(session, learner)
    slugs = [c.program.slug for c in candidates]
    assert "rezyume-hidden" not in slugs
    assert "python-start" not in slugs  # closes none of her gaps
    # The Uzbek course closes two goal skills; the Russian one closes one in
    # her language. Both come before the English courses (an interest).
    assert slugs[:2] == ["rezyume-kursi", "rezyume-kurs"] or slugs[:2] == [
        "rezyume-kurs",
        "rezyume-kursi",
    ]
    assert slugs.index("english-a1") < slugs.index("english-c1")  # C1 is not for a beginner
    assert [c.index for c in candidates] == list(range(1, len(candidates) + 1))
    assert programs["cv_uz"].id == next(
        c.program.id for c in candidates if c.program.slug == "rezyume-kursi"
    )


@pytest.mark.asyncio
async def test_a_finished_course_is_not_offered_again_and_a_started_one_comes_first(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    programs = await _catalogue(session)
    session.add(
        Enrollment(
            user_id=user_id, program_id=programs["cv_uz"].id, status=EnrollmentStatus.COMPLETED
        )
    )
    session.add(
        Enrollment(
            user_id=user_id, program_id=programs["english"].id, status=EnrollmentStatus.IN_PROGRESS
        )
    )
    await session.flush()
    attempt = await _attempt(session, user_id, FIRST_JOB)

    candidates = await cm.candidates_for(session, await cm.load_learner(session, attempt))
    slugs = [c.program.slug for c in candidates]
    assert "rezyume-kursi" not in slugs
    assert slugs.index("english-a1") < slugs.index("english-c1")


@pytest.mark.asyncio
async def test_with_the_model_off_the_rules_write_the_words_and_keep_them(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)

    result = await cm.recommend(session, attempt, gateway=OffGateway())
    assert result.mode == "rules"
    assert result.courses and result.courses[0]["priority"] == "high"
    assert all(result.courses[0]["why_i18n"][lang] for lang in ("uz", "ru", "en"))
    stages = [s["title_i18n"]["en"] for s in result.roadmap]
    assert stages[0].startswith("Foundation") and stages[-1].startswith("Putting")
    activities = [i["activity"] for i in result.roadmap[-1]["items"]]
    assert "cv" in activities and "apply" in activities  # she wants a first job
    assert result.follow_up is None  # she named interests
    assert attempt.recommendation["mode"] == "rules"
    assert attempt.recommendation["courses"][0]["program_id"] == result.courses[0]["program_id"]
    assert (await session.scalar(select(AiInteraction))) is None


@pytest.mark.asyncio
async def test_without_interests_or_a_goal_text_the_rules_ask_a_follow_up(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, {**FIRST_JOB, "interests": [], "goal_text": ""})
    result = await cm.recommend(session, attempt, gateway=OffGateway())
    assert result.follow_up is not None and result.follow_up["ru"]


@pytest.mark.asyncio
async def test_the_model_explains_the_rules_candidates_and_leaves_a_trace(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    programs = await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)
    gateway = FakeGateway(_payload(courses=[_course(1), _course(99)]))

    result = await cm.recommend(session, attempt, gateway=gateway, inline_model=True)
    assert gateway.calls == 1
    assert result.mode == "ai"
    assert len(result.courses) == 1  # 99 is nobody
    assert result.courses[0]["program_id"] in {str(p.id) for p in programs.values()}
    assert result.trace == {
        "model": "claude-test",
        "prompt_version": "2026.11.1",
        "trace_id": "trace-match",
    }
    # The prompt carries her profile and numbered candidates, never a course the catalogue lacks.
    prompt = gateway.prompts[0]
    assert "main goal: first_job" in prompt and "1. " in prompt
    assert "python" not in prompt.lower()
    # No minItems/maxItems in the schema: the provider refuses them.
    assert "maxItems" not in json.dumps(gateway.schemas[0])
    row = await session.scalar(select(AiInteraction))
    assert row is not None and row.feature == cm.FEATURE and row.trace_id == "trace-match"
    assert row.retrieved_sources and all("program_id" in s for s in row.retrieved_sources)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gateway", [FakeGateway(down=True), FakeGateway(refuse=True), FakeGateway({"courses": []})]
)
async def test_a_silent_or_failing_model_leaves_the_rules_answer(session, gateway):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)
    result = await cm.recommend(session, attempt, gateway=gateway, inline_model=True)
    assert result.mode == "rules" and result.courses
    assert attempt.recommendation["ai_tries"] == 1


@pytest.mark.asyncio
async def test_a_version_2_attempt_still_gets_courses_from_its_goal_codes(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, None, version=2)
    attempt.goals = ["find_job"]
    await session.flush()
    learner = await cm.load_learner(session, attempt)
    assert learner.profile["goals"] == ["first_job"]
    assert "cv-writing" in {g.slug for g in learner.gaps}


@pytest.mark.asyncio
async def test_no_gaps_means_no_courses_and_an_honest_mode(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(
        session, user_id, {"version": 3, "goals": ["improve_skills"], "interests": []}
    )
    attempt.dimension_scores = {d.value: 80 for d in ScoreDimension}
    result = await cm.recommend(
        session, attempt, gateway=FakeGateway(_payload()), inline_model=True
    )
    assert result.mode == "insufficient" and result.courses == []
    assert attempt.recommendation["ai_pending"] is False
    assert result.follow_up is not None


@pytest.mark.asyncio
async def test_the_api_stores_the_rules_and_leaves_the_model_to_the_worker(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)
    gateway = FakeGateway(_payload(courses=[_course(1)]))

    first = await cm.recommend(session, attempt, gateway=gateway)
    assert first.mode == "rules" and first.ai_pending
    assert gateway.calls == 0
    assert attempt.recommendation["ai_pending"] is True

    handled = await cm.explain_pending(session, gateway=gateway)
    assert handled == 1 and gateway.calls == 1
    assert attempt.recommendation["mode"] == "ai"
    assert attempt.recommendation["ai_pending"] is False
    assert await cm.explain_pending(session, gateway=gateway) == 0  # nothing left


@pytest.mark.asyncio
async def test_a_model_that_keeps_failing_stops_being_waited_for(session):
    await load_skills(session)
    user_id = uuid.uuid4()
    session.add(User(id=user_id, phone=_phone()))
    await _catalogue(session)
    attempt = await _attempt(session, user_id, FIRST_JOB)
    down = FakeGateway(down=True)
    await cm.recommend(session, attempt, gateway=down)
    for _ in range(cm.MODEL_TRIES):
        assert attempt.recommendation["ai_pending"] is True
        await cm.explain_pending(session, gateway=down)
    assert attempt.recommendation["ai_pending"] is False
    assert attempt.recommendation["mode"] == "rules"
    assert down.calls == cm.MODEL_TRIES


# --- QA audit D-01: no interview drills or senior tracks for a beginner -------


def _learner_with(levels: dict[str, int]):
    import uuid as _uuid

    from app.services.course_match import Learner

    return Learner(
        user_id=_uuid.uuid4(),
        profile={},
        language=None,
        held=set(),
        skill_levels=levels,
        weak_dimensions=[],
        finished=set(),
        started=set(),
        age=None,
        region=None,
    )


def test_a_professional_course_is_offered_only_at_work_level():
    from app.core.constants import ProgramCategory
    from app.models.program import Program
    from app.services.course_match import Need, suits

    drill = Program(
        slug="x",
        title_i18n={"ru": "Сборник задач с собеседований программиста"},
        category=ProgramCategory.DIGITAL_SKILLS,
    )
    basics = Program(
        slug="y", title_i18n={"ru": "Поколение Python"}, category=ProgramCategory.DIGITAL_SKILLS
    )
    covers = [Need("programming", 2.0, "interest", "it_programming")]
    assert not suits(drill, _learner_with({}), covers)
    assert not suits(drill, _learner_with({"it": 1}), covers)
    assert suits(drill, _learner_with({"it": 3}), covers)
    assert suits(basics, _learner_with({}), covers)
