"""AI Assistant endpoints.

Two access levels, as the brief asks. A visitor who has not signed up may try
the assistant a few times and see what it does; a signed-in user gets answers
built from her own profile. The difference is the persona, not a different
model or a different prompt — which is what makes the upgrade legible when she
does sign up.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from datetime import UTC, date, datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession
from app.core.constants import ConsentScope, GoalHorizon, Region, ScoreDimension
from app.core.security import decode_token
from app.models.consent import ConsentLog
from app.models.profile import Goal, Profile
from app.models.user import User
from app.schemas.assistant import (
    AssistantAsk,
    AssistantProfile,
    AssistantReply,
    DetailAsk,
    OnboardingIn,
)
from app.services import ai_assistant
from app.services.ai_assistant import AssistantAnswer, Persona

router = APIRouter(prefix="/assistant", tags=["assistant"])

_optional_bearer = HTTPBearer(auto_error=False)


async def optional_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
) -> uuid.UUID | None:
    """The caller's id when signed in, `None` when browsing anonymously.

    No token at all is a guest. A token that is present but expired is a
    *signed-in woman whose session ran out*, and silently demoting her to guest
    was worse than an error: the assistant appeared to forget everything it knew
    about her, and then refused her on the guest quota. She gets a 401 so the
    client can refresh instead.
    """
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from None
    return uuid.UUID(payload["sub"])


OptionalUserDep = Annotated[uuid.UUID | None, Depends(optional_user_id)]


# --- guest allowance -------------------------------------------------------
# In-process and best-effort: it resets on restart and is per-worker. That is
# adequate for the pilot and is not a security control — the real boundary is
# that a guest gets no persona. Move to Redis when the API runs multi-worker.
_GUEST_WINDOW_SECONDS = 24 * 60 * 60
_guest_hits: dict[str, list[float]] = {}


def _guest_key(request: Request, guest_id: str | None) -> str:
    if guest_id:
        return f"g:{guest_id[:64]}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


def _guest_take(key: str) -> int:
    """Spend one question. Returns how many are left; raises when exhausted."""
    allowance = ai_assistant.guest_allowance()
    now = time.time()
    hits = [t for t in _guest_hits.get(key, []) if now - t < _GUEST_WINDOW_SECONDS]
    if len(hits) >= allowance:
        _guest_hits[key] = hits
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="guest_limit_reached",
        )
    hits.append(now)
    _guest_hits[key] = hits
    return allowance - len(hits)


def _guest_left(key: str) -> int:
    now = time.time()
    hits = [t for t in _guest_hits.get(key, []) if now - t < _GUEST_WINDOW_SECONDS]
    return max(0, ai_assistant.guest_allowance() - len(hits))


def _reply(answer: AssistantAnswer, guest_left: int | None) -> AssistantReply:
    return AssistantReply(
        answer=answer.answer,
        section=answer.section,
        kind=answer.kind,
        trace_id=answer.trace_id,
        personalised=answer.personalised,
        escalated=answer.escalated,
        escalation_reason=answer.escalation_reason,
        see_a_doctor=answer.see_a_doctor,
        professions=answer.professions,
        roadmap=answer.roadmap,
        next_actions=answer.next_actions,
        programs=answer.programs,
        sources=answer.sources,
        confidence=answer.confidence,
        is_uncertain=answer.is_uncertain,
        route=answer.route,
        guest_questions_left=guest_left,
    )


@router.post("/ask", response_model=AssistantReply)
async def ask(
    payload: AssistantAsk,
    request: Request,
    session: DbSession,
    user_id: OptionalUserDep,
    x_guest_id: Annotated[str | None, Header()] = None,
) -> AssistantReply:
    """The one door. Portal documentation, career advice or health — routed."""
    guest_left = None
    if user_id is None:
        guest_left = _guest_take(_guest_key(request, x_guest_id))

    answer = await ai_assistant.ask(
        session,
        user_id=user_id,
        question=payload.message,
        language=payload.language,
        route=payload.route,
    )
    await session.commit()
    return _reply(answer, guest_left)


@router.post("/education", response_model=AssistantReply)
async def education(
    payload: AssistantAsk,
    request: Request,
    session: DbSession,
    user_id: OptionalUserDep,
    x_guest_id: Annotated[str | None, Header()] = None,
) -> AssistantReply:
    """Choosing a profession, learning one, or picking up a craft."""
    guest_left = None
    if user_id is None:
        guest_left = _guest_take(_guest_key(request, x_guest_id))

    answer = await ai_assistant.ask_education(
        session, user_id=user_id, question=payload.message, language=payload.language
    )
    await session.commit()
    return _reply(answer, guest_left)


@router.post("/education/detail", response_model=AssistantReply)
async def education_detail(
    payload: DetailAsk,
    session: DbSession,
    user_id: OptionalUserDep,
) -> AssistantReply:
    """Expand an answer into profession or roadmap cards.

    A second call rather than a bigger first one: the cards are most of the
    output tokens, and folding them in made the visible reply four times slower.
    """
    answer = await ai_assistant.education_detail(
        session,
        user_id=user_id,
        question=payload.message,
        answer_so_far=payload.answer,
        kind=payload.kind,
        language=payload.language,
    )
    await session.commit()
    return _reply(answer, None)


@router.post("/health", response_model=AssistantReply)
async def health(
    payload: AssistantAsk,
    request: Request,
    session: DbSession,
    user_id: OptionalUserDep,
    x_guest_id: Annotated[str | None, Header()] = None,
) -> AssistantReply:
    """Age-appropriate health information. Never a diagnosis."""
    guest_left = None
    if user_id is None:
        guest_left = _guest_take(_guest_key(request, x_guest_id))

    answer = await ai_assistant.ask_health(
        session, user_id=user_id, question=payload.message, language=payload.language
    )
    await session.commit()
    return _reply(answer, guest_left)


@router.get("/daily", response_model=AssistantReply)
async def daily(
    user: CurrentUserDep, session: DbSession, language: str | None = None
) -> AssistantReply:
    """Today's line, written for this user and cached per day and language."""
    answer = await ai_assistant.daily_message(
        session, user_id=uuid.UUID(user.id), language=language
    )
    await session.commit()
    return _reply(answer, None)


@router.get("/guest-allowance", response_model=dict)
async def guest_allowance(
    request: Request,
    user_id: OptionalUserDep,
    x_guest_id: Annotated[str | None, Header()] = None,
) -> dict:
    """How many trial questions are left, so the UI can say so up front."""
    if user_id is not None:
        return {"unlimited": True, "left": None, "allowance": None}
    key = _guest_key(request, x_guest_id)
    return {
        "unlimited": False,
        "left": _guest_left(key),
        "allowance": ai_assistant.guest_allowance(),
    }


@router.get("/profile", response_model=AssistantProfile)
async def assistant_profile(user: CurrentUserDep, session: DbSession) -> AssistantProfile:
    """What the assistant knows, and what is still missing.

    Shown in the UI so personalisation is visible rather than mysterious — the
    brief asks for an assistant that gets to know her, which only reads that
    way if she can see what it has learned.
    """
    persona: Persona = await ai_assistant.build_persona(session, uuid.UUID(user.id))

    missing: list[str] = []
    if persona.age is None:
        missing.append("age")
    if not persona.interests:
        missing.append("interests")
    if not persona.goals:
        missing.append("goal")
    if not persona.directions:
        missing.append("direction")
    if not persona.personalised:
        missing.append("consent")

    return AssistantProfile(
        personalised=persona.personalised,
        age_band=persona.band,
        age=persona.age,
        name=persona.name,
        interests=persona.interests,
        goals=persona.goals,
        directions=persona.directions,
        in_progress=persona.in_progress,
        plan_progress=persona.plan_progress,
        missing=missing,
    )


@router.post("/onboarding", response_model=AssistantProfile)
async def onboarding(
    payload: OnboardingIn, user: CurrentUserDep, session: DbSession
) -> AssistantProfile:
    """The five-field onboarding: name, age, interests, one goal, one direction.

    Age is stored as a birth date on 1 January of the implied year rather than
    as a number, so the band stays correct as she gets older instead of ageing
    out of accuracy the day after she signs up.
    """
    user_id = uuid.UUID(user.id)
    today = date.today()

    record = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    if record is None:
        record = Profile(user_id=user_id)
        session.add(record)

    record.full_name = " ".join(part for part in (payload.name, payload.surname) if part.strip())
    record.birth_date = date(today.year - payload.age, 1, 1)
    record.age_group = f"{payload.age // 5 * 5}-{payload.age // 5 * 5 + 4}"
    record.interests = [i.strip() for i in payload.interests if i.strip()][:12]
    record.completeness_percent = max(record.completeness_percent or 0, 60)

    # One goal, tied to a development dimension when the direction maps to one.
    # Both are optional: a woman who skipped the second step still gets an
    # account, she just gets less personalisation until she fills them in.
    if payload.goal.strip():
        try:
            dimension = ScoreDimension(payload.direction)
        except ValueError:
            dimension = None
        session.add(
            Goal(
                user_id=user_id,
                title=payload.goal.strip(),
                horizon=GoalHorizon.M6,
                dimension=dimension,
            )
        )

    # Personalisation is what the whole assistant runs on, so it is asked for
    # explicitly here and recorded like any other consent — append-only.
    session.add(
        ConsentLog(
            user_id=user_id,
            scope=ConsentScope.AI_PERSONALISATION,
            accepted=bool(payload.consent_ai_personalisation),
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
        )
    )

    account = await session.get(User, user_id)
    if account is not None:
        if payload.region:
            # An unrecognised region is ignored rather than rejected: it must
            # not be the thing that stops her finishing sign-up.
            with contextlib.suppress(ValueError):
                account.region = Region(payload.region)
        if account.onboarding_completed_at is None:
            account.onboarding_completed_at = datetime.now(UTC)

    await session.flush()
    await session.commit()

    persona = await ai_assistant.build_persona(session, user_id)
    return AssistantProfile(
        personalised=persona.personalised,
        age_band=persona.band,
        age=persona.age,
        name=persona.name,
        interests=persona.interests,
        goals=persona.goals,
        directions=persona.directions,
        in_progress=persona.in_progress,
        plan_progress=persona.plan_progress,
        missing=[],
    )
