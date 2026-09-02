"""Sending the one-time sign-in code by e-mail.

Sign-in asks for an e-mail address, so this is the path the code actually
travels. It is deliberately small: one plain message, no templating engine, no
tracking pixel, no queue. A sign-in code that arrives thirty seconds late is a
sign-in that failed, so it is sent inline rather than handed to the outbox.

With no SMTP host configured — local development, or a server where the mail
account has not been set up yet — nothing is sent and the caller finds out, so
the API can fall back to showing the code instead of pretending it posted one.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from anyio import to_thread

from app.core.config import settings

logger = logging.getLogger(__name__)

# The three languages the portal speaks. The subject and body follow whatever
# she was reading when she asked for the code.
SUBJECTS = {
    "uz": "WomanUP — kirish kodi",
    "ru": "WomanUP — код для входа",
    "en": "WomanUP — your sign-in code",
}

BODIES = {
    "uz": (
        "Assalomu alaykum!\n\n"
        "WomanUP portaliga kirish kodingiz: {code}\n\n"
        "Kod {minutes} daqiqa amal qiladi. Agar bu siz boʻlmasangiz, "
        "bu xatni eʼtiborsiz qoldiring — hisobingizga hech kim kirmaydi.\n\n"
        "WomanUP"
    ),
    "ru": (
        "Здравствуйте!\n\n"
        "Ваш код для входа на портал WomanUP: {code}\n\n"
        "Код действует {minutes} минут. Если это были не вы, просто "
        "не отвечайте на письмо — в ваш аккаунт никто не войдёт.\n\n"
        "WomanUP"
    ),
    "en": (
        "Hello,\n\n"
        "Your WomanUP sign-in code: {code}\n\n"
        "The code is valid for {minutes} minutes. If this was not you, ignore "
        "this message — nobody can get into your account with it.\n\n"
        "WomanUP"
    ),
}


class MailNotConfigured(Exception):
    """No SMTP host in the environment."""


class MailFailed(Exception):
    """The provider refused or could not be reached."""


def enabled() -> bool:
    return bool(settings.smtp_host)


def _compose(to: str, code: str, language: str) -> EmailMessage:
    lang = language if language in SUBJECTS else "uz"
    message = EmailMessage()
    message["Subject"] = SUBJECTS[lang]
    message["From"] = settings.smtp_from or (settings.smtp_user or "no-reply@womanup.uz")
    message["To"] = to
    message.set_content(BODIES[lang].format(code=code, minutes=settings.otp_ttl_seconds // 60))
    return message


def _send(message: EmailMessage) -> None:
    """Blocking SMTP send. Call `send_code`, not this."""
    host, port = settings.smtp_host, settings.smtp_port
    try:
        # 465 is implicit TLS; everything else starts plain and upgrades.
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password or "")
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password or "")
                server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # Never log the message body: it carries a live sign-in code.
        logger.warning("could not send the sign-in code: %s", type(exc).__name__)
        raise MailFailed(str(exc)) from exc


async def send_code(to: str, code: str, language: str = "uz") -> None:
    """Send a one-time code. Raises rather than failing silently."""
    if not enabled():
        raise MailNotConfigured("no SMTP host configured")
    await to_thread.run_sync(_send, _compose(to, code, language))
