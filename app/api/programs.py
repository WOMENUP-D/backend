"""Programme catalogue, lessons, enrollment and progress."""

from __future__ import annotations

import uuid

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
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.schemas.common import Page, PaginationParams
from app.schemas.program import (
    CertificateRead,
    EnrollmentDetail,
    EnrollmentRead,
    LessonProgressUpdate,
    ProgramCreate,
    ProgramDetail,
    ProgramLessonDetail,
    ProgramRead,
    ProgramUpdate,
    ProgressUpdate,
)
from app.services import learning, skills

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


@router.get("/me/enrollments", response_model=list[EnrollmentDetail])
async def my_enrollments(
    user: CurrentUserDep, session: DbSession, status_filter: EnrollmentStatus | None = None
) -> list[EnrollmentDetail]:
    """What she is studying, each with the course it belongs to.

    The programme travels with the enrollment because every screen that lists
    them needs to name the course, and fetching them one by one is a request
    per row.
    """
    stmt = (
        select(Enrollment, Program)
        .join(Program, Program.id == Enrollment.program_id)
        .where(Enrollment.user_id == uuid.UUID(user.id))
        .order_by(Enrollment.last_activity_at.desc().nullslast())
    )
    if status_filter:
        stmt = stmt.where(Enrollment.status == status_filter)

    return [
        EnrollmentDetail(
            **EnrollmentRead.model_validate(enrollment).model_dump(),
            program=ProgramRead.model_validate(program),
        )
        for enrollment, program in (await session.execute(stmt)).all()
    ]


@router.get("/me/certificates", response_model=list[CertificateRead])
async def my_certificates(user: CurrentUserDep, session: DbSession) -> list[CertificateRead]:
    rows = await session.execute(
        select(Certificate, Program)
        .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
        .join(Program, Program.id == Enrollment.program_id)
        .where(
            Certificate.user_id == uuid.UUID(user.id),
            Certificate.revoked_at.is_(None),
        )
        .order_by(Certificate.issued_at.desc())
    )
    return [_certificate_read(certificate, program) for certificate, program in rows.all()]


@router.get("/certificates/verify/{code}", response_model=CertificateRead)
async def verify_certificate(code: str, session: DbSession) -> CertificateRead:
    """Public verification — an employer checks a certificate without an account."""
    row = (
        await session.execute(
            select(Certificate, Program)
            .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Certificate.verification_code == code, Certificate.revoked_at.is_(None))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found")
    return _certificate_read(*row)


def _certificate_read(certificate: Certificate, program: Program) -> CertificateRead:
    return CertificateRead(
        **{
            field: getattr(certificate, field)
            for field in ("id", "serial_number", "issued_at", "file_url", "verification_code")
        },
        program_id=program.id,
        program_title_i18n=program.title_i18n,
    )


@router.get("/slug/{slug}", response_model=ProgramDetail)
async def read_program_by_slug(
    slug: str, session: DbSession, user: OptionalUserDep
) -> ProgramDetail:
    """The same card, addressed the way the learning section links to it.

    Course URLs are readable — `/talim/kurslar/buxgalteriya-asoslari` — and a
    slug is the one thing about a course that a person can type.
    """
    program = await session.scalar(select(Program).where(Program.slug == slug))
    return await _program_detail(session, program, user)


@router.get("/{program_id}", response_model=ProgramDetail)
async def read_program(
    program_id: uuid.UUID, session: DbSession, user: OptionalUserDep
) -> ProgramDetail:
    """One programme card, open to visitors like the catalogue itself.

    Unpublished programmes are 404 for everyone except the people who edit
    them. Without that check this endpoint handed a draft to anyone holding its
    id — which was already true for every signed-in user before the catalogue
    became public, and would have widened to the whole internet with it.
    """
    program = await session.get(Program, program_id)
    return await _program_detail(session, program, user)


async def _program_detail(session, program: Program | None, user) -> ProgramDetail:
    """The card with its contents and its skills, or a 404 she may not see past."""
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    may_see_drafts = user is not None and user.has_role(Role.ADMIN, Role.TRAINER, Role.MODERATOR)
    if not program.is_published and not may_see_drafts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    # Modules carry their lessons with them, so the contents list is one query
    # rather than one per module.
    await session.refresh(program, ["modules"])
    detail = ProgramDetail.model_validate(program)
    # The card names what it teaches in the reader's language; the column keeps
    # the author's own words.
    index = await skills.SkillIndex.load(session, program.skills_taught)
    detail.skills = index.refs(program.skills_taught)
    return detail


@router.get("/{program_id}/lessons/{lesson_slug}", response_model=ProgramLessonDetail)
async def read_lesson(
    program_id: uuid.UUID, lesson_slug: str, session: DbSession, user: OptionalUserDep
) -> ProgramLesson:
    """One lesson, with its body.

    Readable wherever the course card is: the syllabus and the module texts are
    already public, and a woman deciding whether a course is for her is owed a
    look at it. Completing one is what needs an enrollment.
    """
    program = await session.get(Program, program_id)
    if program is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    may_see_drafts = user is not None and user.has_role(Role.ADMIN, Role.TRAINER, Role.MODERATOR)
    if not program.is_published and not may_see_drafts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")

    lesson = await session.scalar(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program_id, ProgramLesson.slug == lesson_slug)
    )
    if lesson is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found")
    return lesson


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
    from datetime import UTC, datetime

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
    """Put her on a course. Enrolling twice returns the enrollment she has.

    The catalogue stays open: a course that happens to sit inside a learning
    path is still enrollable from here, because a path advises an order rather
    than owning the course.
    """
    enrollment = await learning.enroll(session, user_id=uuid.UUID(user.id), program_id=program_id)
    if enrollment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found")
    return enrollment


@router.post("/enrollments/{enrollment_id}/progress", response_model=EnrollmentRead)
async def update_progress(
    enrollment_id: uuid.UUID,
    payload: ProgressUpdate,
    user: CurrentUserDep,
    session: DbSession,
) -> Enrollment:
    """Mark a module done and recompute progress.

    For programmes whose modules carry no lessons. Progress, completion, the
    certificate and the skill evidence all run through `services.learning`, so
    a module tick and a lesson tick end in exactly the same place.
    """
    enrollment = await _own_enrollment(session, enrollment_id, user)
    module_ids, _ = await learning.course_units(session, enrollment.program_id)
    if str(payload.module_id) not in module_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Module does not belong to this programme",
        )

    return await learning.mark_module(
        session,
        enrollment=enrollment,
        module_id=payload.module_id,
        completed=payload.completed,
    )


@router.post("/enrollments/{enrollment_id}/lessons", response_model=EnrollmentRead)
async def complete_lesson(
    enrollment_id: uuid.UUID,
    payload: LessonProgressUpdate,
    user: CurrentUserDep,
    session: DbSession,
) -> Enrollment:
    """Mark a lesson done, and recompute what that means for the course.

    The lesson has to belong to the programme she is enrolled in: an id from
    somebody else's course is a 422, not a tick.
    """
    enrollment = await _own_enrollment(session, enrollment_id, user)
    lesson = await learning.lesson_in_program(
        session, lesson_id=payload.lesson_id, program_id=enrollment.program_id
    )
    if lesson is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lesson does not belong to this programme",
        )

    return await learning.mark_lesson(
        session,
        enrollment=enrollment,
        lesson_id=lesson.id,
        completed=payload.completed,
    )


async def _own_enrollment(session, enrollment_id: uuid.UUID, user) -> Enrollment:
    """Her enrollment, or a 404. Never the id the browser happens to send."""
    enrollment = await session.get(Enrollment, enrollment_id)
    if enrollment is None or enrollment.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enrollment not found")
    return enrollment
