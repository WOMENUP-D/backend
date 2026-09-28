"""The demonstration dataset and the real one must never meet.

`app/seed.py` ships inside the production image. It deletes every account and
creates an administrator whose password is published in this repository — on
an empty production database it would have run without even asking.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.seed import PRODUCTION_OVERRIDE, refuse_in_production


def test_a_production_database_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.delenv(PRODUCTION_OVERRIDE, raising=False)

    with pytest.raises(SystemExit) as caught:
        refuse_in_production()

    message = str(caught.value)
    assert "Refusing to seed" in message
    assert PRODUCTION_OVERRIDE in message
    # The refusal explains itself without printing the demo password.
    assert "WomanUP2026" not in message


def test_a_deliberate_override_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setenv(PRODUCTION_OVERRIDE, "yes-wipe-production")

    refuse_in_production()  # does not raise


def test_a_half_hearted_override_is_not_enough(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    for value in ("1", "true", "yes", ""):
        monkeypatch.setenv(PRODUCTION_OVERRIDE, value)
        with pytest.raises(SystemExit):
            refuse_in_production()


@pytest.mark.parametrize("environment", ["local", "staging"])
def test_other_environments_are_untouched(monkeypatch, environment: str):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.delenv(PRODUCTION_OVERRIDE, raising=False)

    refuse_in_production()  # does not raise
