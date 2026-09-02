"""Programme catalogue, enrollment and progress."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import ContentDep, CurrentUserDep, DbSession, OptionalUserDep
from app.core.constants import (
    EnrollmentStatus,
    Language,
    ProgramCategory,
    ProgramFormat,
    Role,
)
from app.models.program import Certificate, Enrollment, Program, ProgramModule
from app.schemas.common import Page, PaginationParams
from app.schemas.program import (
    CertificateRead,
    EnrollmentRead,
    ProgramCreate,
    ProgramDetail,
    ProgramRead,
    ProgramUpdate,
    ProgressUpdate,
)
from app.services.plan_service import close_plan_items_for_program

router = APIRouter(prefix="/programs", tags=["programs"])


@router.get("", response_model=Page[ProgramRead])
async def list_programs(
    session: DbSession,
    pagination: PaginationParams = Depends(),
    category: ProgramCategory | None = None,
    format: ProgramFormat | None = None,
    language: Language | None = None,
    region: str | None = None,
    search: str | None = Query(default=None, max_length=100),
    has_certificate: bool | None = None,
) -> Page[ProgramRead]:
    """Catalogue with the filters the programme screen offers.

    Open to visitors: the landing page advertises this catalogue publicly, and
    only published programmes are ever returned. Enrolling still needs an
    account.
    """
    stmt = select(Program).where(Program.is_published.is_(True))

    if category:
        stmt = stmt.where(Program.category == category)
    if format:
        stmt = stmt.where(Program.format == format)
    if language:
        stmt = stmt.where(Program.language == language)
    if has_certificate is not None:
        stmt = stmt.where(Program.has_certificate.is_(has_certificate))
    if region:
        # An empty target_regions list means "available everywhere".
        stmt = stmt.where(
            or_(
                Program.target_regions == [],
                Program.target_regions.any(region),
            )
        )
    if search:
        # Title, goal and the skills taught, in every locale. Someone hunting
        # for "excel" or "1c" is describing what she wants to be able to do,
        # not the name of a course, and a title-only search left her with
        # nothing.
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Program.title_i18n.op("->>")("uz")).like(pattern),
                func.lower(Program.title_i18n.op("->>")("ru")).like(pattern),
                func.lower(Program.title_i18n.op("->>")("en")).like(pattern),
                func.lower(Program.goal_i18n.op("->>")("uz")).like(pattern),
                func.lower(Program.goal_i18n.op("->>")("ru")).like(pattern),
                func.lower(Program.goal_i18n.op("->>")("en")).like(pattern),
                func.lower(func.array_to_string(Program.skills_taught, " ")).like(pattern),
                func.lower(Program.slug).like(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(Program.published_at.desc().nullslast())
        .offset(pagination.offset)
        .limit(pagination.size)
    )
    return Page[ProgramRead](
        items=[ProgramRead.model_validate(p) for p in rows.scalars()],
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


@router.get("/{program_id}", response_model=ProgramDetail)
async def read_program(program_id: uuid.UUID, session: DbSession, user: OptionalUserDep) -> Program:
    """One programme card, open to visitors like the catalogue itself.

    Unpublished programmes are 404 for everyone except the people who edit
    them. Without that check this endpoint handed a draft to anyone holding its
    id — which was already true for every signed-in user before the catalogue
    became public, and would have widened to the whole internet with it.
    """
    program = await session.get(Program, program_id)
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    may_see_drafts = user is not None and user.has_role(Role.ADMIN, Role.TRAINER, Role.MODERATOR)
    if not program.is_published and not may_see_drafts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    await session.refresh(program, ["modules"])
    return program


@router.post("", response_model=ProgramRead, status_code=status.HTTP_201_CREATED)
async def create_program(payload: ProgramCreate, user: ContentDep, session: DbSession) -> Program:
    """Create a programme card. Trainers and admins only."""
    exists = await session.scalar(select(Program.id).where(Program.slug == payload.slug))
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Program with slug '{payload.slug}' already exists",
        )
    program = Program(**payload.model_dump(), author_id=uuid.UUID(user.id))
    session.add(program)
    await session.flush()
    return program


@router.patch("/{program_id}", response_model=ProgramRead)
async def update_program(
    program_id: uuid.UUID,
    payload: ProgramUpdate,
    user: ContentDep,
    session: DbSession,
) -> Program:
    program = await session.get(Program, program_id)
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_published") and program.published_at is None:
        program.published_at = datetime.now(UTC)
    for field, value in changes.items():
        setattr(program, field, value)
    await session.flush()
    return program


@router.post(
    "/{program_id}/enroll",
    response_model=EnrollmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def enroll(program_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> Enrollment:
    program = await session.get(Program, program_id)
    if program is None or not program.is_published:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    user_id = uuid.UUID(user.id)
    existing = await session.scalar(
        select(Enrollment).where(Enrollment.user_id == user_id, Enrollment.program_id == program_id)
    )
    if existing is not None:
        return existing

    enrollment = Enrollment(
        user_id=user_id,
        program_id=program_id,
        status=EnrollmentStatus.ENROLLED,
        started_at=datetime.now(UTC),
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


@router.get("/me/enrollments", response_model=list[EnrollmentRead])
async def my_enrollments(
    user: CurrentUserDep, session: DbSession, status_filter: EnrollmentStatus | None = None
) -> list[Enrollment]:
    stmt = select(Enrollment).where(Enrollment.user_id == uuid.UUID(user.id))
    if status_filter:
        stmt = stmt.where(Enrollment.status == status_filter)
    return list((await session.execute(stmt)).scalars())


@router.post("/enrollments/{enrollment_id}/progress", response_model=EnrollmentRead)
async def update_progress(
    enrollment_id: uuid.UUID,
    payload: ProgressUpdate,
    user: CurrentUserDep,
    session: DbSession,
) -> Enrollment:
    """Mark a module done and recompute progress; issues the certificate on
    completion when the programme awards one."""
    enrollment = await session.get(Enrollment, enrollment_id)
    if enrollment is None or enrollment.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")

    module_ids = list(
        (
            await session.execute(
                select(ProgramModule.id).where(ProgramModule.program_id == enrollment.program_id)
            )
        ).scalars()
    )
    if payload.module_id not in module_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Module does not belong to this programme",
        )

    completed = set(enrollment.completed_modules or [])
    if payload.completed:
        completed.add(str(payload.module_id))
    else:
        completed.discard(str(payload.module_id))

    now = datetime.now(UTC)
    enrollment.completed_modules = sorted(completed)
    enrollment.progress_percent = round(len(completed) * 100 / len(module_ids)) if module_ids else 0
    enrollment.last_activity_at = now
    enrollment.status = (
        EnrollmentStatus.COMPLETED
        if enrollment.progress_percent >= 100
        else EnrollmentStatus.IN_PROGRESS
    )

    if enrollment.status == EnrollmentStatus.COMPLETED and enrollment.completed_at is None:
        enrollment.completed_at = now
        # The roadmap step that *is* this course closes itself. The platform
        # already knows the course is finished — it just recorded the last
        # module — so making her confirm it again on another screen is asking
        # her to restate what the system holds.
        await close_plan_items_for_program(session, enrollment.user_id, enrollment.program_id)
        program = await session.get(Program, enrollment.program_id)
        if program is not None and program.has_certificate:
            session.add(
                Certificate(
                    enrollment_id=enrollment.id,
                    user_id=enrollment.user_id,
                    serial_number=f"WU-{now:%Y}-{secrets.token_hex(4).upper()}",
                    issued_at=now,
                    verification_code=secrets.token_urlsafe(24),
                )
            )

    await session.flush()
    return enrollment


@router.get("/me/certificates", response_model=list[CertificateRead])
async def my_certificates(user: CurrentUserDep, session: DbSession) -> list[Certificate]:
    rows = await session.execute(
        select(Certificate).where(
            Certificate.user_id == uuid.UUID(user.id),
            Certificate.revoked_at.is_(None),
        )
    )
    return list(rows.scalars())


@router.get("/certificates/verify/{code}", response_model=CertificateRead)
async def verify_certificate(code: str, session: DbSession) -> Certificate:
    """Public verification — an employer checks a certificate without an account."""
    certificate = await session.scalar(
        select(Certificate).where(
            Certificate.verification_code == code, Certificate.revoked_at.is_(None)
        )
    )
    if certificate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found")
    return certificate
