"""Who may apply to a listing — decided once, on the server, for every surface.

The catalogue, the listing page, the apply endpoint, the recommendation engine
and the Coach all ask this one function, so a listing cannot be "for you" in
the cabinet and refused at the apply button.

Three answers, never rounded:

* **not eligible** — the listing is closed, or a rule it states excludes her,
  or she is a girl the platform knows to be under 18 and the listing states no
  rule of its own. That last one is platform policy, the same one the career
  page applies (Step 9): a vacancy or an investment is not a next step to put
  in front of a child.
* **unknown** — the listing states an age rule and her age is not on record,
  or it states a requirement this platform cannot read. Nothing is invented to
  settle it; the page says what would.
* **eligible** — nothing the platform can check stands in the way. That is not
  a promise of anything: the organisation still decides.

Rules are read from `opportunities.eligibility`, which partners send through
the sync. The contract this module reads is `age_min` and `age_max`, whole
years. Any other key is a requirement the platform cannot evaluate, so it is
reported rather than ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.constants import EligibilityStatus
from app.models.opportunity import Opportunity

ADULT_AGE = 18
#: The eligibility keys this platform knows how to check.
READABLE = frozenset({"age_min", "age_max"})


@dataclass(frozen=True, slots=True)
class Eligibility:
    status: EligibilityStatus
    #: `closed`, `too_young`, `too_old`, `adults_only`, `age_unknown`,
    #: `other_requirements` — a key the page turns into a sentence.
    reason: str | None = None
    age_min: int | None = None
    age_max: int | None = None

    @property
    def may_apply(self) -> bool:
        """Whether the apply button may submit.

        A requirement the platform cannot read does not block her — the
        organisation reads it, and the page says so. An age it cannot
        establish does: that one is about who the listing is safe for.
        """
        if self.status == EligibilityStatus.ELIGIBLE:
            return True
        return self.status == EligibilityStatus.UNKNOWN and self.reason == "other_requirements"


def is_open(opportunity: Opportunity, now: datetime | None = None) -> bool:
    """Active, and its deadline — if it has one — not yet passed. An event also
    stops taking registrations once it has begun."""
    now = now or datetime.now(UTC)
    return (
        bool(opportunity.is_active)
        and (opportunity.deadline is None or opportunity.deadline > now)
        and (opportunity.starts_at is None or opportunity.starts_at > now)
    )


def _whole(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def age_limits(opportunity: Opportunity) -> tuple[int | None, int | None]:
    rules = opportunity.eligibility or {}
    return _whole(rules.get("age_min")), _whole(rules.get("age_max"))


def unreadable(opportunity: Opportunity) -> list[str]:
    """Requirements the listing states that this platform cannot evaluate."""
    rules = opportunity.eligibility or {}
    return sorted(key for key, value in rules.items() if key not in READABLE and value)


def check(opportunity: Opportunity, *, age: int | None, now: datetime | None = None) -> Eligibility:
    """Whether she may apply, from her age and what the listing states."""
    low, high = age_limits(opportunity)
    limits = {"age_min": low, "age_max": high}

    if not is_open(opportunity, now):
        return Eligibility(EligibilityStatus.NOT_ELIGIBLE, "closed", **limits)

    if low is not None or high is not None:
        if age is None:
            return Eligibility(EligibilityStatus.UNKNOWN, "age_unknown", **limits)
        if low is not None and age < low:
            return Eligibility(EligibilityStatus.NOT_ELIGIBLE, "too_young", **limits)
        if high is not None and age > high:
            return Eligibility(EligibilityStatus.NOT_ELIGIBLE, "too_old", **limits)
    elif age is not None and age < ADULT_AGE:
        return Eligibility(EligibilityStatus.NOT_ELIGIBLE, "adults_only", **limits)

    if unreadable(opportunity):
        return Eligibility(EligibilityStatus.UNKNOWN, "other_requirements", **limits)
    return Eligibility(EligibilityStatus.ELIGIBLE, None, **limits)


def excluded(opportunity: Opportunity, *, age: int | None, now: datetime | None = None) -> bool:
    """Whether a listing must not be put in front of her at all.

    Only a definite no. A listing whose eligibility is unknown is still shown —
    with the reason — because hiding it would decide for her.
    """
    return check(opportunity, age=age, now=now).status == EligibilityStatus.NOT_ELIGIBLE
