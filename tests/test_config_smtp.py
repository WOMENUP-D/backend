"""The mail port, and what happens when it is not one.

A port that was not a number once took production down: it was read for the
first time inside a migration that ran after the deploy had already stopped
the running containers. The deploy now asks first, and this is the answer it
gets — an unusable value is refused, so a release carrying one never starts.
Mail itself stays optional: no mail configured is a supported state.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

SECRET_LOOKING = "smtp.mail.example.com"


def build(port: object) -> Settings:
    """Settings with only the mail port set, and nothing read from a file."""
    return Settings(_env_file=None, smtp_port=port)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (587, 587),
        ("587", 587),
        ('"587"', 587),  # a secret store hands back what it was given
        ("  465  ", 465),
        (25, 25),
        (65535, 65535),
    ],
)
def test_a_port_is_accepted(given: object, expected: int):
    assert build(given).smtp_port == expected


@pytest.mark.parametrize("given", [None, "", "   ", '""'])
def test_no_mail_at_all_stays_a_supported_state(given: object):
    """Optional means optional: the mailer reports itself as not configured."""
    assert build(given).smtp_port is None


@pytest.mark.parametrize("given", [SECRET_LOOKING, "0", "70000", "-1", "5 8 7", "587;ls", True])
def test_anything_that_is_not_a_port_is_refused(given: object):
    with pytest.raises(ValidationError) as caught:
        build(given)
    errors = caught.value.errors()
    assert [error["loc"] for error in errors] == [("smtp_port",)]
    assert "port number between 1 and 65535" in errors[0]["msg"]


def test_the_refusal_names_the_setting_and_not_the_value():
    """Our own message must be safe to paste into a public log."""
    with pytest.raises(ValidationError) as caught:
        build(SECRET_LOOKING)
    message = caught.value.errors()[0]["msg"]
    assert "SMTP_PORT" in message
    assert SECRET_LOOKING not in message


def test_the_default_is_unchanged():
    assert Settings(_env_file=None).smtp_port == 587


def test_a_refused_setting_reaches_the_log_by_name_only(monkeypatch):
    """`get_settings` keeps Pydantic's rendering of the value out of the log."""
    monkeypatch.setenv("SMTP_PORT", SECRET_LOOKING)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError) as caught:
            get_settings()
        message = str(caught.value)
        assert "smtp_port" in message
        assert "values are not shown" in message
        assert SECRET_LOOKING not in message
    finally:
        get_settings.cache_clear()
