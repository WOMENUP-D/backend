"""The skill layer: one vocabulary, one place that decides what evidence means.

Everything that will later reason about skills — learning paths, career paths,
the AI coach, job matching, practical tasks, employer review — goes through
here rather than comparing strings of its own. Three jobs:

* **Resolve.** A label written on a course, a listing or a profile becomes a
  taxonomy entry, so "buxgalteriya", "Бухгалтерия" and "accounting" are one
  skill and can be displayed in the reader's own language.
* **Record.** Evidence is appended with the record that produced it, which
  makes it idempotent: replaying a course completion changes nothing.
* **Derive.** Status and level are computed from evidence and never set by
  hand. This is where "finishing a course is not a verified skill" is enforced,
  once, for every caller.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EnrollmentStatus,
    EvidenceKind,
    ProficiencyLevel,
    SkillCategory,
    SkillStatus,
)
from app.models.learning_path import LearningPath
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.schemas.skill import EvidenceRead, SkillRef, UserSkillRead

#: What one piece of evidence is worth. Course completion tops out at
#: `LEARNED`: it says she was taught the skill, not that anyone checked.
#: Verification needs a person or a result from outside the platform.
EVIDENCE_STATUS: dict[EvidenceKind, SkillStatus] = {
    EvidenceKind.SELF_REPORTED: SkillStatus.SELF_REPORTED,
    EvidenceKind.COURSE_COMPLETION: SkillStatus.LEARNED,
    EvidenceKind.LEARNING_PATH: SkillStatus.LEARNED,
    EvidenceKind.CERTIFICATE: SkillStatus.LEARNED,
    EvidenceKind.PRACTICAL_PROJECT: SkillStatus.LEARNED,
    EvidenceKind.AI_ASSESSMENT: SkillStatus.ASSESSED,
    EvidenceKind.FORMAL_ASSESSMENT: SkillStatus.ASSESSED,
    EvidenceKind.MENTOR_ASSESSMENT: SkillStatus.VERIFIED,
    EvidenceKind.EMPLOYER_ASSESSMENT: SkillStatus.VERIFIED,
    EvidenceKind.INTERNSHIP: SkillStatus.VERIFIED,
    EvidenceKind.JOB_OUTCOME: SkillStatus.VERIFIED,
}

STATUS_RANK: dict[SkillStatus, int] = {
    SkillStatus.SELF_REPORTED: 0,
    SkillStatus.LEARNED: 1,
    SkillStatus.ASSESSED: 2,
    SkillStatus.VERIFIED: 3,
}

LEVEL_RANK: dict[ProficiencyLevel, int] = {
    level: index for index, level in enumerate(ProficiencyLevel)
}

#: The floor a kind of evidence establishes when its source does not say more.
#: Finishing a course teaching a skill means at least the first step of it;
#: what she says about herself sets no level at all.
EVIDENCE_LEVEL_FLOOR: dict[EvidenceKind, ProficiencyLevel] = {
    EvidenceKind.COURSE_COMPLETION: ProficiencyLevel.BEGINNER,
    EvidenceKind.LEARNING_PATH: ProficiencyLevel.INTERMEDIATE,
    EvidenceKind.CERTIFICATE: ProficiencyLevel.BEGINNER,
    EvidenceKind.PRACTICAL_PROJECT: ProficiencyLevel.ELEMENTARY,
    EvidenceKind.INTERNSHIP: ProficiencyLevel.ELEMENTARY,
    EvidenceKind.JOB_OUTCOME: ProficiencyLevel.INTERMEDIATE,
}

# Uzbek writes oʻ and gʻ with a modifier letter that is typed half a dozen
# ways; the same word arrives as "jamgʻarma", "jamgarma" and "jamg'arma".
# Dropping the mark folds all of them together.
_OKINA = dict.fromkeys(map(ord, "ʻʼ‘’'`´"), None)
# Hyphens separate rather than distinguish: "biznes-reja" and "biznes reja" are
# the same skill written two ways. `+` and `#` are kept — they are part of the
# name in "c++" and "c#".
_PUNCTUATION = re.compile(r"[^\w\s+#]", re.UNICODE)
_SPACES = re.compile(r"[\s_-]+")

# Enough Cyrillic to build a readable slug out of a Russian label.
_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def normalise(label: str) -> str:
    """The comparable form of a written skill label.

    Case, spacing, punctuation and the Uzbek okina are all noise when deciding
    whether two labels name the same skill; everything else is kept.
    """
    folded = label.strip().casefold().translate(_OKINA)
    folded = _PUNCTUATION.sub(" ", folded)
    return _SPACES.sub(" ", folded).strip()


def slug_for(label: str) -> str:
    """A stable ascii identifier for a label the taxonomy has not seen."""
    base = "".join(_TRANSLIT.get(char, char) for char in normalise(label))
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    # A label with nothing transliterable left (emoji, an unknown script) still
    # needs an identifier that is stable for that label.
    return slug[:80] or f"skill-{uuid.uuid5(uuid.NAMESPACE_OID, label).hex[:12]}"


@dataclass(slots=True)
class SkillIndex:
    """Labels resolved against the taxonomy, loaded once for a request.

    Built from the labels actually in play rather than the whole taxonomy: the
    catalogue of skills is expected to grow into the thousands, and a page only
    ever names a few dozen.
    """

    by_alias: dict[str, Skill]

    @classmethod
    async def load(cls, session: AsyncSession, labels: Iterable[str]) -> SkillIndex:
        written = [label for label in labels if label and label.strip()]
        keys = {normalise(label) for label in written}
        if not keys:
            return cls({})
        # A slug is matched as written as well: folding turns "customer-service"
        # into "customer service", which no slug is.
        slugs = keys | {label.strip().casefold() for label in written}
        rows = await session.execute(
            select(Skill).where(
                or_(Skill.aliases.overlap(list(keys)), Skill.slug.in_(slugs)),
                Skill.is_active.is_(True),
            )
        )
        by_alias: dict[str, Skill] = {}
        for skill in rows.scalars():
            for alias in (*skill.aliases, skill.slug):
                by_alias.setdefault(normalise(alias), skill)
        return cls(by_alias)

    @classmethod
    def of(cls, skills: Iterable[Skill]) -> SkillIndex:
        """An index over skills already in hand, with no query."""
        by_alias: dict[str, Skill] = {}
        for skill in skills:
            for alias in (*skill.aliases, skill.slug):
                by_alias.setdefault(normalise(alias), skill)
        return cls(by_alias)

    def get(self, label: str) -> Skill | None:
        return self.by_alias.get(normalise(label))

    def key(self, label: str) -> str:
        """What two labels are compared on: the skill they resolve to, or the
        label itself when the taxonomy has never seen it."""
        skill = self.get(label)
        return skill.slug if skill is not None else f"label:{normalise(label)}"

    def ref(self, label: str) -> SkillRef:
        skill = self.get(label)
        if skill is None:
            return SkillRef(label=label)
        return SkillRef(
            slug=skill.slug,
            name_i18n=skill.name_i18n,
            label=label,
            category=skill.category,
            dimensions=list(skill.dimensions),
        )

    def refs(self, labels: Iterable[str]) -> list[SkillRef]:
        """One ref per distinct skill, in the order the labels were written."""
        seen: set[str] = set()
        out: list[SkillRef] = []
        for label in labels:
            if not label or not label.strip():
                continue
            key = self.key(label)
            if key in seen:
                continue
            seen.add(key)
            out.append(self.ref(label))
        return out


def ref_for(skill: Skill, label: str | None = None) -> SkillRef:
    """A ref for a taxonomy row, for callers that hold the skill already."""
    return SkillRef(
        slug=skill.slug,
        name_i18n=skill.name_i18n,
        label=label or skill.slug,
        category=skill.category,
        dimensions=list(skill.dimensions),
    )


async def ensure_skill(
    session: AsyncSession, label: str, *, index: SkillIndex | None = None
) -> Skill:
    """The taxonomy entry for a label, minting an uncurated one if it is new.

    A label nobody has curated still has to be storable — otherwise a course
    written this morning teaches a skill the platform cannot record. The entry
    is marked uncurated and carries the label as its name in every language,
    which reads correctly and is obviously waiting for an editor.
    """
    if index is not None and (found := index.get(label)) is not None:
        return found

    key = normalise(label)
    skill = await session.scalar(
        select(Skill).where(or_(Skill.aliases.overlap([key]), Skill.slug == key)).limit(1)
    )
    if skill is not None:
        return skill

    slug = slug_for(label)
    clash = await session.scalar(select(Skill).where(Skill.slug == slug))
    if clash is not None:
        # Same identifier, different wording: record the spelling and reuse it.
        if key not in clash.aliases:
            clash.aliases = [*clash.aliases, key]
        return clash

    display = label.strip()
    skill = Skill(
        slug=slug,
        name_i18n={"uz": display, "ru": display, "en": display},
        category=SkillCategory.PROFESSIONAL,
        dimensions=[],
        aliases=[key],
        is_curated=False,
    )
    session.add(skill)
    await session.flush()
    return skill


def derive_state(
    evidence: Iterable[SkillEvidence],
) -> tuple[SkillStatus | None, ProficiencyLevel | None]:
    """What her evidence adds up to: the strongest claim it supports.

    Withdrawn evidence is ignored but kept on the record. With nothing left
    standing the answer is `None` — the skill is remembered, not claimed.
    """
    status: SkillStatus | None = None
    level: ProficiencyLevel | None = None

    for item in evidence:
        if item.revoked_at is not None:
            continue
        supports = EVIDENCE_STATUS[item.kind]
        if status is None or STATUS_RANK[supports] > STATUS_RANK[status]:
            status = supports
        evidenced = item.level or EVIDENCE_LEVEL_FLOOR.get(item.kind)
        if evidenced is not None and (level is None or LEVEL_RANK[evidenced] > LEVEL_RANK[level]):
            level = evidenced

    return status, level


async def _user_skill(session: AsyncSession, user_id: uuid.UUID, skill: Skill) -> UserSkill:
    record = await session.scalar(
        select(UserSkill).where(UserSkill.user_id == user_id, UserSkill.skill_id == skill.id)
    )
    if record is None:
        record = UserSkill(user_id=user_id, skill_id=skill.id)
        session.add(record)
        await session.flush()
    return record


async def recompute(session: AsyncSession, user_skill: UserSkill) -> UserSkill:
    """Re-read the evidence and store what it supports."""
    evidence = list(
        (
            await session.execute(
                select(SkillEvidence).where(SkillEvidence.user_skill_id == user_skill.id)
            )
        ).scalars()
    )
    user_skill.status, user_skill.level = derive_state(evidence)
    await session.flush()
    return user_skill


async def record_evidence(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    label: str | None = None,
    skill: Skill | None = None,
    kind: EvidenceKind,
    source_type: str,
    source_id: str,
    level: ProficiencyLevel | None = None,
    score: float | None = None,
    verified_by_id: uuid.UUID | None = None,
    details: dict | None = None,
    occurred_at: datetime | None = None,
    index: SkillIndex | None = None,
) -> SkillEvidence:
    """Append one piece of evidence and refresh what it supports.

    Idempotent on (skill, kind, source): finishing the same course twice, or a
    backfill running after the live path already recorded it, leaves one row.
    """
    if skill is None:
        if label is None:
            raise ValueError("record_evidence needs either a skill or a label")
        skill = await ensure_skill(session, label, index=index)

    user_skill = await _user_skill(session, user_id, skill)
    existing = await session.scalar(
        select(SkillEvidence).where(
            SkillEvidence.user_skill_id == user_skill.id,
            SkillEvidence.kind == kind,
            SkillEvidence.source_type == source_type,
            SkillEvidence.source_id == source_id,
        )
    )
    if existing is not None:
        # A withdrawn piece of evidence that happens again counts again.
        existing.revoked_at = None
        if level is not None:
            existing.level = level
        if score is not None:
            existing.score = score
        await recompute(session, user_skill)
        return existing

    evidence = SkillEvidence(
        user_skill_id=user_skill.id,
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        level=level,
        score=score,
        verified_by_id=verified_by_id,
        details=details or {},
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(evidence)
    await session.flush()
    await recompute(session, user_skill)
    return evidence


async def revoke_evidence(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: EvidenceKind,
    source_type: str,
    source_id: str,
    skill_id: uuid.UUID | None = None,
) -> int:
    """Withdraw evidence without erasing it, and recompute what is left."""
    stmt = (
        select(SkillEvidence, UserSkill)
        .join(UserSkill, SkillEvidence.user_skill_id == UserSkill.id)
        .where(
            UserSkill.user_id == user_id,
            SkillEvidence.kind == kind,
            SkillEvidence.source_type == source_type,
            SkillEvidence.source_id == source_id,
            SkillEvidence.revoked_at.is_(None),
        )
    )
    if skill_id is not None:
        stmt = stmt.where(UserSkill.skill_id == skill_id)

    now = datetime.now(UTC)
    revoked = 0
    for evidence, user_skill in (await session.execute(stmt)).all():
        evidence.revoked_at = now
        await session.flush()
        await recompute(session, user_skill)
        revoked += 1
    return revoked


# --- the places evidence comes from ----------------------------------------


async def sync_self_reported(
    session: AsyncSession, user_id: uuid.UUID, labels: Iterable[str]
) -> list[Skill]:
    """Mirror the skills she lists on her profile into the skill layer.

    Her own word about herself is the weakest kind of evidence and is recorded
    as exactly that. Removing a skill from the profile withdraws the claim and
    nothing else: a course she finished stays on the record.
    """
    written = [label.strip() for label in labels if label and label.strip()]
    index = await SkillIndex.load(session, written)

    kept: list[Skill] = []
    for label in written:
        skill = await ensure_skill(session, label, index=index)
        await record_evidence(
            session,
            user_id=user_id,
            skill=skill,
            kind=EvidenceKind.SELF_REPORTED,
            source_type="profile",
            source_id="profile",
            details={"label": label},
        )
        kept.append(skill)

    keep_ids = {skill.id for skill in kept}
    # Only what the *profile* claimed is withdrawn here. Other sources record
    # self-reported evidence too — a portfolio project is her word as well —
    # and editing the profile list must not quietly erase those claims.
    rows = await session.execute(
        select(SkillEvidence, UserSkill)
        .join(UserSkill, SkillEvidence.user_skill_id == UserSkill.id)
        .where(
            UserSkill.user_id == user_id,
            SkillEvidence.kind == EvidenceKind.SELF_REPORTED,
            SkillEvidence.source_type == "profile",
            SkillEvidence.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for evidence, user_skill in rows.all():
        if user_skill.skill_id in keep_ids:
            continue
        evidence.revoked_at = now
        await session.flush()
        await recompute(session, user_skill)

    return kept


async def record_course_completion(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    labels: Iterable[str],
    enrollment_id: uuid.UUID,
    occurred_at: datetime | None = None,
    level: ProficiencyLevel | None = None,
) -> list[SkillEvidence]:
    """Finishing a course is evidence that she was taught its skills.

    Deliberately not verification: nobody has checked what she can do with it.
    """
    written = [label for label in labels if label and label.strip()]
    index = await SkillIndex.load(session, written)
    return [
        await record_evidence(
            session,
            user_id=user_id,
            label=label,
            kind=EvidenceKind.COURSE_COMPLETION,
            source_type="enrollment",
            source_id=str(enrollment_id),
            level=level,
            occurred_at=occurred_at,
            index=index,
        )
        for label in written
    ]


async def record_certificate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    labels: Iterable[str],
    certificate_id: uuid.UUID,
    occurred_at: datetime | None = None,
) -> list[SkillEvidence]:
    """A certificate the platform issued, recorded against what it covers."""
    written = [label for label in labels if label and label.strip()]
    index = await SkillIndex.load(session, written)
    return [
        await record_evidence(
            session,
            user_id=user_id,
            label=label,
            kind=EvidenceKind.CERTIFICATE,
            source_type="certificate",
            source_id=str(certificate_id),
            occurred_at=occurred_at,
            index=index,
        )
        for label in written
    ]


# --- reading them back ------------------------------------------------------


async def user_skills(session: AsyncSession, user_id: uuid.UUID) -> list[UserSkill]:
    """Everything she holds, strongest first, with evidence loaded."""
    rows = await session.execute(select(UserSkill).where(UserSkill.user_id == user_id))
    records = list(rows.scalars())
    records.sort(
        key=lambda record: (
            -STATUS_RANK.get(record.status, -1) if record.status else 1,
            -LEVEL_RANK.get(record.level, -1) if record.level else 1,
            record.skill.slug,
        )
    )
    return records


@dataclass(slots=True)
class SkillNote:
    """One skill she holds, in the words and the shape a prompt needs.

    Deliberately carries the status as well as the name. The whole distinction
    Step 3 is built on — a course teaches, only a person verifies — is lost the
    moment a skill reaches the model as a bare noun, and a coach that says
    "your Excel is verified" because she finished a course has told her
    something the platform does not believe.
    """

    label: str
    status: SkillStatus | None
    level: ProficiencyLevel | None

    def as_line(self) -> str:
        parts = [self.label]
        if self.status is not None:
            parts.append(self.status.value)
        if self.level is not None:
            parts.append(self.level.value)
        return " — ".join(parts) if len(parts) > 1 else parts[0]


async def notes_for(
    session: AsyncSession, user_id: uuid.UUID, *, language: str = "uz"
) -> list[SkillNote]:
    """What she holds, named in her language and labelled with how well it is known.

    The canonical replacement for reading `profile.skills` — that column is
    what she once typed about herself, which is one kind of evidence among
    several and the weakest of them. Anything reasoning about her skills should
    ask here instead.
    """
    return [
        SkillNote(
            label=(
                record.skill.name_i18n.get(language)
                or record.skill.name_i18n.get("uz")
                or next(iter(record.skill.name_i18n.values()), record.skill.slug)
            ),
            status=record.status,
            level=record.level,
        )
        for record in await user_skills(session, user_id)
    ]


async def skill_keys(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """What she can be matched on: the slugs of every skill she still holds."""
    rows = await session.execute(
        select(Skill.slug)
        .join(UserSkill, UserSkill.skill_id == Skill.id)
        .where(UserSkill.user_id == user_id, UserSkill.status.is_not(None))
    )
    return set(rows.scalars())


async def skill_profile(session: AsyncSession, user_id: uuid.UUID) -> list[UserSkillRead]:
    """Her skills as the cabinet shows them.

    Each one carries the evidence behind it, named by the course or certificate
    that produced it rather than by an identifier — and, where she is part-way
    through a course that teaches it, how far along that course is. Progress is
    read from the enrollment every time; storing a second copy of a number the
    enrollment already holds is how the two start disagreeing.
    """
    records = await user_skills(session, user_id)
    if not records:
        return []

    enrollment_ids = _source_ids(records, "enrollment")
    certificate_ids = _source_ids(records, "certificate")
    path_ids = _source_ids(records, "learning_path")
    titles: dict[str, dict] = {}

    if enrollment_ids:
        rows = await session.execute(
            select(Enrollment.id, Program.title_i18n)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Enrollment.id.in_(enrollment_ids))
        )
        titles.update({f"enrollment:{row[0]}": row[1] for row in rows.all()})

    if certificate_ids:
        rows = await session.execute(
            select(Certificate.id, Program.title_i18n)
            .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Certificate.id.in_(certificate_ids))
        )
        titles.update({f"certificate:{row[0]}": row[1] for row in rows.all()})

    if path_ids:
        rows = await session.execute(
            select(LearningPath.id, LearningPath.title_i18n).where(LearningPath.id.in_(path_ids))
        )
        titles.update({f"learning_path:{row[0]}": row[1] for row in rows.all()})

    # Practical work and her own projects name themselves, so the evidence
    # behind a skill reads "Practical task — One-month budget" rather than an id.
    attempt_ids = _source_ids(records, "task_attempt")
    if attempt_ids:
        rows = await session.execute(
            select(TaskAttempt.id, PracticalTask.title_i18n)
            .join(PracticalTask, PracticalTask.id == TaskAttempt.task_id)
            .where(TaskAttempt.id.in_(attempt_ids))
        )
        titles.update({f"task_attempt:{row[0]}": row[1] for row in rows.all()})

    project_ids = _source_ids(records, "portfolio_project")
    if project_ids:
        rows = await session.execute(
            select(PortfolioProject.id, PortfolioProject.title).where(
                PortfolioProject.id.in_(project_ids)
            )
        )
        titles.update(
            {
                f"portfolio_project:{row[0]}": {"uz": row[1], "ru": row[1], "en": row[1]}
                for row in rows.all()
            }
        )

    # What she is studying right now that teaches these skills.
    index = SkillIndex.of(record.skill for record in records)
    open_rows = await session.execute(
        select(Enrollment.progress_percent, Program.skills_taught)
        .join(Program, Program.id == Enrollment.program_id)
        .where(
            Enrollment.user_id == user_id,
            Enrollment.status.in_((EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)),
        )
    )
    progress: dict[str, int] = {}
    for percent, labels in open_rows.all():
        for label in labels or []:
            skill = index.get(label)
            if skill is not None:
                progress[skill.slug] = max(progress.get(skill.slug, 0), percent or 0)

    profile: list[UserSkillRead] = []
    for record in records:
        active = [item for item in record.evidence if item.revoked_at is None]
        profile.append(
            UserSkillRead(
                skill=ref_for(record.skill),
                status=record.status,
                level=record.level,
                evidence_count=len(active),
                evidence=[_evidence_read(item, titles) for item in active],
                progress_percent=progress.get(record.skill.slug),
            )
        )
    return profile


def _source_ids(records: Iterable[UserSkill], source_type: str) -> list[uuid.UUID]:
    """Record ids referenced by evidence of one source, ignoring keys that are
    not ids at all — self-reported evidence points at no row."""
    found: set[uuid.UUID] = set()
    for record in records:
        for item in record.evidence:
            if item.source_type != source_type:
                continue
            try:
                found.add(uuid.UUID(item.source_id))
            except ValueError:
                continue
    return list(found)


def _evidence_read(evidence: SkillEvidence, titles: dict[str, dict]) -> EvidenceRead:
    return EvidenceRead(
        kind=evidence.kind,
        supports=EVIDENCE_STATUS[evidence.kind],
        level=evidence.level,
        score=evidence.score,
        title_i18n=titles.get(f"{evidence.source_type}:{evidence.source_id}", {}),
        reference=evidence.details.get("label"),
        occurred_at=evidence.occurred_at,
    )
