"""Composing and sending the sign-in code.

The code in these messages is a live credential for sixty seconds or five
minutes, so what is pinned here is mostly about where it must *not* go: not
into a log line, not into a message addressed to the wrong language, and not
into a silent failure that leaves a woman waiting for mail nobody sent.
"""

from __future__ import annotations

import smtplib

import pytest

from app.core.config import settings
from app.services import mailer
from app.services.mailer import MailFailed, MailNotConfigured


@pytest.fixture(autouse=True)
def smtp_configured(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "no-reply@womanup.uz")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_from", "WomanUP <no-reply@womanup.uz>")


def test_the_message_carries_the_code_and_says_how_long_it_lasts():
    message = mailer._compose("dilnoza@gmail.com", "482913", "ru")
    body = message.get_content()
    assert "482913" in body
    assert str(settings.otp_ttl_seconds // 60) in body
    assert message["To"] == "dilnoza@gmail.com"
    assert message["From"] == "WomanUP <no-reply@womanup.uz>"


@pytest.mark.parametrize(
    ("language", "probe"),
    [("uz", "kirish kodi"), ("ru", "код для входа"), ("en", "sign-in code")],
)
def test_she_is_written_to_in_the_language_she_was_reading(language, probe):
    assert probe in mailer._compose("a@b.uz", "111111", language)["Subject"]


def test_an_unknown_language_falls_back_to_uzbek():
    """Uzbek is the primary locale; an unexpected code must not send a blank."""
    message = mailer._compose("a@b.uz", "111111", "fr")
    assert message["Subject"] == mailer.SUBJECTS["uz"]


def test_without_a_host_nothing_is_sent_and_the_caller_is_told(monkeypatch):
    """Silently dropping the message would leave her waiting for mail that
    was never posted."""
    import anyio

    monkeypatch.setattr(settings, "smtp_host", None)
    assert mailer.enabled() is False
    with pytest.raises(MailNotConfigured):
        anyio.run(mailer.send_code, "a@b.uz", "111111", "uz")


def test_a_refused_send_is_reported_rather_than_swallowed(monkeypatch):
    class Refusing:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self):
            pass

        def login(self, *_):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: Refusing())
    with pytest.raises(MailFailed):
        mailer._send(mailer._compose("a@b.uz", "1", "uz"))


def test_a_failure_never_writes_the_code_into_the_log(monkeypatch, caplog):
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self):
            raise OSError("connection reset")

        def login(self, *_):
            pass

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: Broken())
    with caplog.at_level("WARNING"), pytest.raises(MailFailed):
        mailer._send(mailer._compose("a@b.uz", "482913", "uz"))
    assert "482913" not in caplog.text
