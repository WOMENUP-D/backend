"""Notification engine: in-app, email, SMS, push.

Channel adapters are intentionally thin and pluggable — the strategy document
requires Telegram/WhatsApp to be addable later without touching call sites.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import NotificationChannel, NotificationTrigger
from app.models.notification import Notification, NotificationPreference

logger = logging.getLogger(__name__)


class ChannelAdapter(ABC):
    channel: NotificationChannel

    @abstractmethod
    async def send(self, *, recipient: str, title: str, body: str) -> bool:
        """Deliver one message. Return True on success."""


class InAppAdapter(ChannelAdapter):
    channel = NotificationChannel.IN_APP

    async def send(self, *, recipient: str, title: str, body: str) -> bool:
        # In-app delivery is the database row itself.
        return True


class LoggingAdapter(ChannelAdapter):
    """Stand-in for SMS/email/push until providers are contracted.

    Logs the attempt and reports success so flows are testable end-to-end
    without a live provider. Swap for a real adapter before pilot launch.
    """

    def __init__(self, channel: NotificationChannel) -> None:
        self.channel = channel

    async def send(self, *, recipient: str, title: str, body: str) -> bool:
        logger.info(
            "notification dispatched",
            extra={"extra_fields": {"channel": self.channel.value, "title": title}},
        )
        return True


ADAPTERS: dict[NotificationChannel, ChannelAdapter] = {
    NotificationChannel.IN_APP: InAppAdapter(),
    NotificationChannel.EMAIL: LoggingAdapter(NotificationChannel.EMAIL),
    NotificationChannel.SMS: LoggingAdapter(NotificationChannel.SMS),
    NotificationChannel.PUSH: LoggingAdapter(NotificationChannel.PUSH),
}


# Message templates per trigger, keyed by language. Text follows section 11.
TEMPLATES: dict[NotificationTrigger, dict[str, tuple[str, str]]] = {
    NotificationTrigger.ONBOARDING_INCOMPLETE: {
        "uz": ("Rejangiz deyarli tayyor", "Shaxsiy rejangizni olish uchun 2 qadam qoldi."),
        "ru": ("Ваш план почти готов", "Осталось 2 шага до персонального плана."),
        "en": ("Your plan is almost ready", "Two steps left to get your personal plan."),
    },
    NotificationTrigger.COURSE_START: {
        "uz": ("Yangi modul ochildi", "Bugun yangi modul ochildi — davom eting."),
        "ru": ("Открыт новый модуль", "Сегодня открылся новый модуль — продолжайте."),
        "en": ("A new module is open", "A new module opened today — keep going."),
    },
    NotificationTrigger.DEADLINE: {
        "uz": ("Muddat yaqinlashmoqda", "Grant arizasi yakuniga 3 kun qoldi."),
        "ru": ("Приближается срок", "До окончания приёма заявок 3 дня."),
        "en": ("Deadline approaching", "Three days left to apply for the grant."),
    },
    NotificationTrigger.SKILL_GAP: {
        "uz": ("2 ko‘nikma yetishmayapti", "Tanlagan ish uchun 2 ko‘nikmani kuchaytiring."),
        "ru": ("Не хватает 2 навыков", "Для выбранной вакансии стоит усилить 2 навыка."),
        "en": ("Two skills missing", "Strengthen two skills for the role you chose."),
    },
    NotificationTrigger.PROGRESS: {
        "uz": ("Oylik natija", "Bu oy maqsadingizning 70% bajarildi."),
        "ru": ("Итог месяца", "В этом месяце выполнено 70% вашей цели."),
        "en": ("Monthly progress", "You completed 70% of your goal this month."),
    },
    NotificationTrigger.INACTIVITY: {
        "uz": ("Reja yangilandi", "Siz uchun 5 daqiqalik keyingi qadam tayyor."),
        "ru": ("План обновлён", "Для вас готов следующий шаг на 5 минут."),
        "en": ("Your plan was updated", "A five-minute next step is ready for you."),
    },
}


async def enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    trigger: NotificationTrigger,
    language: str = "uz",
    action_url: str | None = None,
    payload: dict | None = None,
    scheduled_for: datetime | None = None,
) -> list[Notification]:
    """Queue a templated notification on every channel the user opted into."""
    template = TEMPLATES.get(trigger, {}).get(language) or TEMPLATES.get(trigger, {}).get("uz")
    if template is None:
        raise ValueError(f"no template for trigger {trigger}")
    title, body = template

    prefs = await session.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    channels = [NotificationChannel.IN_APP]
    if prefs:
        if prefs.email_enabled:
            channels.append(NotificationChannel.EMAIL)
        if prefs.sms_enabled:
            channels.append(NotificationChannel.SMS)
        if prefs.push_enabled:
            channels.append(NotificationChannel.PUSH)

    created = []
    for channel in channels:
        notification = Notification(
            user_id=user_id,
            channel=channel,
            trigger=trigger,
            title=title,
            body=body,
            action_url=action_url,
            payload=payload or {},
            scheduled_for=scheduled_for,
        )
        session.add(notification)
        created.append(notification)
    return created


async def dispatch_pending(session: AsyncSession, limit: int = 100) -> int:
    """Deliver queued notifications. Called by the worker, not by a request."""
    now = datetime.now(UTC)
    pending = list(
        (
            await session.execute(
                select(Notification)
                .where(
                    Notification.sent_at.is_(None),
                    (Notification.scheduled_for.is_(None)) | (Notification.scheduled_for <= now),
                )
                .limit(limit)
            )
        ).scalars()
    )

    delivered = 0
    for notification in pending:
        adapter = ADAPTERS[notification.channel]
        notification.delivery_attempts += 1
        try:
            if await adapter.send(
                recipient=str(notification.user_id),
                title=notification.title,
                body=notification.body,
            ):
                notification.sent_at = now
                delivered += 1
        except Exception as exc:  # a failed channel must not stall the queue
            notification.last_error = str(exc)[:500]
            logger.exception("notification delivery failed")

    return delivered
