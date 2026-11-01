"""Courses for a vacancy's requirements: the rules choose soundly on their own,
the model chooses only among real candidates, and the choice is kept.

What matters most, first: the model cannot name a course that is not in the
catalogue (it answers with a candidate number, and a number that is not one is
dropped); when the model is off or fails, the rules answer and say so; a
failed model call is not repeated on every page view; and every model choice
leaves a trace with its model and prompt versions.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.constants import (
    Language,
    OpportunitySource,
    OpportunityType,
    ProgramCategory,
    ProgramFormat,
)
from app.models.audit import AiInteraction
from app.models.opportunity import Opportunity
from app.models.program import Program
from app.services import vacancy_courses
from app.services.llm_gateway import LlmResponse, LlmUnavailableError
from app.services.vacancy_courses import stem, suggestions_for, tokens


def _program(
    slug: str,
    title: str,
    skills: tuple[str, ...],
    *,
    published: bool = True,
    category: ProgramCategory = ProgramCategory.VOCATIONAL_SKILLS,
) -> Program:
    return Program(
        slug=slug,
        title_i18n={"ru": title},
        goal_i18n={},
        description_i18n={},
        category=category,
        format=ProgramFormat.VIDEO,
        language=Language.RU,
        skills_taught=list(skills),
        is_published=published,
        published_at=datetime.now(UTC) if published else None,
        provider="Stepik",
        external_url=f"https://stepik.org/course/{slug}",
    )


def _vacancy(
    title: str = "Бухгалтер",
    skills: tuple[str, ...] = ("Excel", "1С: Бухгалтерия", "Налоговый учет"),
    description: str = "Опыт работы с 1С и Excel. Знание налогового учёта.",
) -> Opportunity:
    return Opportunity(
        source=OpportunitySource.HH_UZ,
        external_id=uuid.uuid4().hex,
        type=OpportunityType.VACANCY,
        title_i18n={"ru": title},
        description_i18n={"ru": description},
        organisation="Alfa Savdo",
        required_skills=list(skills),
        eligibility={},
        reward={},
        external_url="https://hh.uz/vacancy/1",
        is_active=True,
    )


async def _catalogue(session) -> dict[str, Program]:
    programs = {
        "excel": _program("excel-functions", "Полезные функции Excel", ("excel",)),
        "onec": _program(
            "onec-accounting", "1С: Бухгалтерия для начинающих", ("1с", "бухгалтерия")
        ),
        "python": _program("python-basics", "Python: основы и применение", ("python",)),
        "marketing": _program("marketing", "Основы интернет-маркетинга", ("маркетинг",)),
        "hidden": _program("excel-hidden", "Excel для бухгалтера", ("excel",), published=False),
    }
    for program in programs.values():
        session.add(program)
    await session.flush()
    return programs


class FakeGateway:
    """A model in a box: answers with the payload it was given, or refuses, or
    is unreachable — and counts how often it was asked."""

    enabled = True

    def __init__(self, payload: dict | None = None, *, refuse: bool = False, down: bool = False):
        self.payload = payload
        self.refuse = refuse
        self.down = down
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, **kwargs) -> LlmResponse:
        self.calls += 1
        self.prompts.append(kwargs["messages"][0]["content"])
        if self.down:
            raise LlmUnavailableError("down")
        return LlmResponse(
            text=json.dumps(self.payload or {}),
            trace_id="trace-courses",
            model="claude-test",
            prompt_version="2026.10.1",
            latency_ms=7,
            parsed=None if self.refuse else self.payload,
            refused=self.refuse,
        )


class OffGateway:
    enabled = False

    async def complete(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the model must not be called while disabled")


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def test_stems_put_inflections_on_one_key():
    assert stem("бухгалтер") == stem("бухгалтерия") == stem("бухгалтерский")
    assert stem("маркетолог") == stem("маркетинг")
    assert stem("excel") == "excel"


def test_tokens_drop_filler_and_keep_what_names_a_skill():
    words = tokens("Знание Excel и 1С, опыт работы от 1 года")
    assert "excel" in words and "1с" in words
    assert "опыт" not in words and "работы" not in words and "года" not in words
    assert tokens(None) == set()


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rules_pick_the_courses_that_teach_a_stated_requirement(session):
    programs = await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()

    result = await suggestions_for(session, vacancy, gateway=OffGateway())
    assert result.mode == "rules"
    chosen = [item.program.slug for item in result.items]
    assert chosen[:2] == ["excel-functions", "onec-accounting"] or set(chosen[:2]) == {
        "excel-functions",
        "onec-accounting",
    }
    assert "python-basics" not in chosen
    assert "marketing" not in chosen
    # An unpublished course is not offered, however well it matches.
    assert programs["hidden"].slug not in chosen

    excel = next(item for item in result.items if item.program.slug == "excel-functions")
    assert excel.covers == ["Excel"]
    assert excel.reason_i18n == {}
    assert excel.program.external_url == "https://stepik.org/course/excel-functions"

    # The choice is kept on the listing, with how it was made.
    cache = vacancy.course_suggestions
    assert cache["mode"] == "rules"
    assert [entry["program_id"] for entry in cache["items"]] == [
        str(item.program.id) for item in result.items
    ]


@pytest.mark.asyncio
async def test_a_vacancy_nothing_teaches_gets_an_honest_empty_list(session):
    await _catalogue(session)
    vacancy = _vacancy("Водитель категории D", ("Водительские права D",), "Развозка персонала.")
    session.add(vacancy)
    await session.flush()
    result = await suggestions_for(session, vacancy, gateway=OffGateway())
    assert result.items == []
    assert result.mode == "rules"


@pytest.mark.asyncio
async def test_the_kept_choice_is_served_again_while_it_stands(session):
    await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()

    first = await suggestions_for(session, vacancy, gateway=OffGateway())
    later = first.generated_at + timedelta(days=3)
    second = await suggestions_for(session, vacancy, gateway=OffGateway(), now=later)
    assert second.generated_at == first.generated_at

    expired = first.generated_at + vacancy_courses.TTL + timedelta(hours=1)
    third = await suggestions_for(session, vacancy, gateway=OffGateway(), now=expired)
    assert third.generated_at == expired


@pytest.mark.asyncio
async def test_a_choice_naming_a_course_since_unpublished_is_made_again(session):
    programs = await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()
    first = await suggestions_for(session, vacancy, gateway=OffGateway())
    assert any(item.program.slug == "excel-functions" for item in first.items)

    programs["excel"].is_published = False
    await session.flush()
    second = await suggestions_for(session, vacancy, gateway=OffGateway())
    assert all(item.program.slug != "excel-functions" for item in second.items)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_model_chooses_only_among_real_candidates_and_leaves_a_trace(session):
    programs = await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()

    # The candidate numbering is the ranking's; find Excel's number from the
    # rules so the test does not depend on the exact order.
    ranked = vacancy_courses.rank(
        [p for p in programs.values() if p.is_published],
        vacancy_courses.Requirement.of_opportunity(vacancy),
        await _index(session, programs),
    )
    excel_number = next(
        i for i, item in enumerate(ranked, start=1) if item.program.slug == "excel-functions"
    )

    gateway = FakeGateway(
        {
            "items": [
                {
                    "candidate": excel_number,
                    "covers": ["Excel", "налоговый учет"],
                    "reason_uz": "Excel ko'nikmasi uchun kerak",
                    "reason_ru": "Закрывает требование по Excel",
                    "reason_en": "Covers the Excel requirement",
                },
                {
                    "candidate": 99,
                    "covers": ["x"],
                    "reason_uz": "yo'q",
                    "reason_ru": "нет",
                    "reason_en": "none",
                },
                {
                    "candidate": excel_number,
                    "covers": ["dup"],
                    "reason_uz": "",
                    "reason_ru": "",
                    "reason_en": "",
                },
            ]
        }
    )
    result = await suggestions_for(session, vacancy, gateway=gateway)

    assert result.mode == "ai"
    assert [item.program.slug for item in result.items] == ["excel-functions"]
    pick = result.items[0]
    assert pick.covers == ["Excel", "налоговый учет"]
    # Uzbek reaches the page in the reformed alphabet whatever the model wrote.
    assert pick.reason_i18n["uz"] == "Excel könikmasi uçun kerak"
    assert pick.reason_i18n["ru"] == "Закрывает требование по Excel"

    # The prompt showed the vacancy and the numbered catalogue, nothing else.
    prompt = gateway.prompts[0]
    assert "Title: Бухгалтер" in prompt
    assert f"{excel_number}. Полезные функции Excel" in prompt
    assert "Excel для бухгалтера" not in prompt

    trace = (
        await session.execute(
            select(AiInteraction).where(AiInteraction.feature == "vacancy_courses")
        )
    ).scalar_one()
    assert trace.model_version == "claude-test"
    assert trace.prompt_version == "2026.10.1"
    assert trace.trace_id == "trace-courses"
    assert trace.user_id is None

    cache = vacancy.course_suggestions
    assert cache["mode"] == "ai" and cache["trace_id"] == "trace-courses"
    assert cache["items"][0]["reason_i18n"]["en"] == "Covers the Excel requirement"


@pytest.mark.asyncio
async def test_when_the_model_is_down_the_rules_answer_and_are_not_asked_again_at_once(session):
    await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()

    gateway = FakeGateway(down=True)
    first = await suggestions_for(session, vacancy, gateway=gateway)
    assert first.mode == "rules"
    assert [item.program.slug for item in first.items][:1] in (
        ["excel-functions"],
        ["onec-accounting"],
    )
    assert gateway.calls == 1

    soon = first.generated_at + timedelta(minutes=10)
    second = await suggestions_for(session, vacancy, gateway=gateway, now=soon)
    assert second.mode == "rules" and gateway.calls == 1

    later = first.generated_at + vacancy_courses.RETRY_AFTER + timedelta(minutes=1)
    gateway.down = False
    gateway.payload = {"items": []}
    third = await suggestions_for(session, vacancy, gateway=gateway, now=later)
    assert gateway.calls == 2
    assert third.mode == "ai" and third.items == []


@pytest.mark.asyncio
async def test_a_refusal_falls_back_to_the_rules(session):
    await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()
    result = await suggestions_for(session, vacancy, gateway=FakeGateway(refuse=True))
    assert result.mode == "rules"
    assert result.items


@pytest.mark.asyncio
async def test_a_rules_choice_made_with_the_model_off_is_upgraded_when_it_comes_on(session):
    await _catalogue(session)
    vacancy = _vacancy()
    session.add(vacancy)
    await session.flush()
    first = await suggestions_for(session, vacancy, gateway=OffGateway())
    assert first.mode == "rules"

    gateway = FakeGateway({"items": []})
    second = await suggestions_for(session, vacancy, gateway=gateway)
    assert second.mode == "ai" and gateway.calls == 1


async def _index(session, programs):
    from app.services import skills

    return await skills.SkillIndex.load(
        session, [label for p in programs.values() for label in p.skills_taught]
    )
