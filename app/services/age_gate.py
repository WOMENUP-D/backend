"""Age banding and the health-topic gate.

The portal is open to girls from 10 years old, so "personalise by age" is a
safety boundary rather than a nicety: adult reproductive content must never be
generated for a child. Two rules follow from that and are enforced here rather
than in a prompt:

* an unknown age is treated as the *more protective* band, never as adult;
* an adult-only question from a minor is answered without calling the model at
  all — the same ordering the navigator already uses for disclosures of
  violence, and for the same reason.

A prompt instruction is a request. This module is the boundary.
"""

from __future__ import annotations

from datetime import date

from app.core.constants import AgeBand
from app.models.profile import Profile

# Below this the portal has no content at all; the account is not for them.
MIN_SUPPORTED_AGE = 10

# Matching is done on a form with apostrophes stripped, so one spelling covers
# the several glyphs Uzbek Latin is typed with. See `fold`.
_APOSTROPHES = ("ʻ", "ʼ", "‘", "’", "'", "`")

# Health topics that belong to adult care. Reaching one of these from a child
# or teenage account is not a personalisation miss — it is the failure the age
# band exists to prevent.
ADULT_HEALTH_TOPICS: tuple[str, ...] = (
    # --- uz ---
    "homilador",
    "homiladorlik",
    "tugish",
    "tugruq",
    "kontratsep",
    "abort",
    "klimaks",
    "menopauza",
    "bepushtlik",
    "jinsiy hayot",
    "jinsiy aloqa",
    "emizish",
    "kokrak suti",
    # --- ru ---
    "беремен",
    "роды",
    "родах",
    "контрацеп",
    "аборт",
    "climax",
    "климакс",
    "менопауз",
    "бесплоди",
    "половая жизнь",
    "половой жизни",
    "половой акт",
    "грудное вскармливание",
    "лактаци",
    # --- en ---
    "pregnan",
    "childbirth",
    "giving birth",
    "contracepti",
    "abortion",
    "menopaus",
    "infertilit",
    "sexual intercourse",
    "sex life",
    "breastfeed",
)


def fold(text: str) -> str:
    """Lowercase and drop apostrophes, so tug'ish / tugʻish / tugish agree.

    Public because `news_age` matches the same vocabulary against the same
    Uzbek spellings; a second private copy of this would drift from this one.
    """
    lowered = text.lower()
    for mark in _APOSTROPHES:
        lowered = lowered.replace(mark, "")
    return lowered


def age_from_profile(profile: Profile | None, *, today: date | None = None) -> int | None:
    """Whole years, from the birth date if there is one.

    Falls back to the lower bound of `age_group` ("13-19" -> 13) so a profile
    that only carries the bracket still lands in the protective band rather
    than in UNKNOWN.
    """
    if profile is None:
        return None

    if profile.birth_date is not None:
        today = today or date.today()
        born = profile.birth_date
        years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        return max(0, years)

    if profile.age_group:
        digits = ""
        for char in profile.age_group:
            if char.isdigit():
                digits += char
            elif digits:
                break
        if digits:
            return int(digits)

    return None


def band_for_age(age: int | None) -> AgeBand:
    """Map an age to its band. `None` is UNKNOWN, which is not ADULT."""
    if age is None:
        return AgeBand.UNKNOWN
    if age < 13:
        return AgeBand.CHILD
    if age < 18:
        return AgeBand.TEEN
    return AgeBand.ADULT


def band_for_profile(profile: Profile | None, *, today: date | None = None) -> AgeBand:
    return band_for_age(age_from_profile(profile, today=today))


def is_minor(band: AgeBand) -> bool:
    """UNKNOWN counts as a minor for gating: we protect what we cannot verify."""
    return band in {AgeBand.CHILD, AgeBand.TEEN, AgeBand.UNKNOWN}


def blocks_adult_health(question: str, band: AgeBand) -> bool:
    """True when this question must not be sent to the model for this band."""
    if not is_minor(band):
        return False
    folded = fold(question)
    return any(topic in folded for topic in ADULT_HEALTH_TOPICS)


# What each band may be told about, stated positively. Goes into the system
# prompt so the model's own framing matches the boundary enforced above.
HEALTH_SCOPE: dict[AgeBand, str] = {
    AgeBand.CHILD: (
        "The reader is a girl aged 10-12. Stay on healthy habits, sleep, food, "
        "movement, feelings, friendships, personal hygiene and the ordinary "
        "changes of growing up, explained simply. Never discuss pregnancy, "
        "contraception, sexual activity or any adult reproductive topic. Point "
        "her to a parent, a trusted adult or a doctor for anything medical."
    ),
    AgeBand.TEEN: (
        "The reader is a girl aged 13-17. Health guidance may cover puberty, "
        "periods, nutrition, sleep, exercise, skin, emotional wellbeing and "
        "study stress, at an educational level. Do not discuss pregnancy, "
        "contraception or sexual activity; refer those to a parent, a school "
        "nurse or a doctor. Never suggest medication or a diagnosis."
    ),
    AgeBand.ADULT: (
        "The reader is an adult woman. General informational health content is "
        "appropriate, including women's health broadly. It remains educational: "
        "no diagnosis, no prescription, no treatment plan — a doctor decides."
    ),
    AgeBand.UNKNOWN: (
        "The reader's age is not known, so assume she may be a minor. Keep to "
        "general healthy-habit guidance and avoid every adult reproductive "
        "topic. Invite her to complete her profile so the answer can be made "
        "age-appropriate."
    ),
}
