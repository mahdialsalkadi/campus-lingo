"""Retention loop: notification dispatch and the scheduled reminder sweep.

Every alert is written to the ``notifications`` table first — that is the
durable, always-working channel (the dashboard inbox, the unread badge and the
SSE feed all read it). Web Push delivery on top of it is **best effort**: RFC
8291 payload encryption needs the optional ``pywebpush`` package plus a VAPID
key pair, so when the package or the keys are missing the row is still recorded
with ``push_status='push_unavailable'`` and the reminder is delivered in-app.

Three alerts are produced by the sweep:

* **new_lecture** — an instructor posted material into a classroom (fired inline
  by the upload path, not by the sweep).
* **streak_saver** — sent after ``STREAK_SAVER_HOUR`` (local, default 20:00)
  when the learner has not completed a challenge today.
* **fsrs_decay** — sent when aged cards lose stability and risk being forgotten.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    FsrsCard,
    Notification,
    PushSubscription,
    User,
    utcnow,
)

logger = logging.getLogger("campuslingo.retention")

# --- tunables ----------------------------------------------------------------
NEW_LECTURE = "new_lecture"
STREAK_SAVER = "streak_saver"
FSRS_DECAY = "fsrs_decay"
CLASSROOM = "classroom"
CREATOR_ROYALTY = "creator_royalty"

STREAK_SAVER_HOUR = int(os.getenv("STREAK_SAVER_HOUR", "20"))  # 8:00 PM
FSRS_DECAY_MIN_CARDS = int(os.getenv("FSRS_DECAY_MIN_CARDS", "5"))
FSRS_DECAY_STABILITY_DAYS = float(os.getenv("FSRS_DECAY_STABILITY_DAYS", "1.0"))
RETENTION_INTERVAL_SECONDS = int(os.getenv("RETENTION_INTERVAL_SECONDS", str(30 * 60)))
RETENTION_ENABLED = os.getenv("RETENTION_ENABLED", "1").lower() not in ("0", "false", "no")

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@campuslingo.local")

# Optional: real Web Push needs RFC 8291 encryption (pywebpush). Guarded import
# keeps the service runnable with zero extra dependencies.
try:  # pragma: no cover - depends on the deployment
    from pywebpush import WebPushException, webpush  # type: ignore
except Exception:  # noqa: BLE001
    webpush = None  # type: ignore[assignment]

    class WebPushException(Exception):  # type: ignore[no-redef]
        """Placeholder so call sites stay import-safe without pywebpush."""


def push_available() -> bool:
    """True when a real Web Push delivery can be attempted."""
    return webpush is not None and bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


# ---------------------------------------------------------------------------
# Notification plumbing
# ---------------------------------------------------------------------------


async def record_notification(
    session: AsyncSession,
    user_id: Any,
    *,
    kind: str,
    title: str,
    body: str,
    url: Optional[str] = None,
    course_id: Optional[Any] = None,
    push_status: str = "recorded",
) -> Notification:
    """Insert one notification row (the durable, always-available channel)."""
    notification = Notification(
        user_id=user_id,
        course_id=course_id,
        kind=kind,
        title=title[:240],
        body=body[:1000],
        url=(url or None),
        push_status=push_status,
    )
    session.add(notification)
    await session.flush()
    return notification


async def subscriptions_for(
    session: AsyncSession, user_id: Any
) -> list[PushSubscription]:
    return list(
        (
            await session.execute(
                select(PushSubscription)
                .where(PushSubscription.user_id == user_id)
                .order_by(PushSubscription.created_at)
            )
        )
        .scalars()
        .all()
    )


def _payload(notification: Notification) -> str:
    return json.dumps(
        {
            "title": notification.title or "CampusLingo",
            "body": notification.body,
            "url": notification.url or "/",
            "kind": notification.kind,
            "notification_id": str(notification.id),
        },
        ensure_ascii=False,
    )


async def deliver_push(
    subscription: PushSubscription, notification: Notification
) -> tuple[bool, str]:
    """Attempt one Web Push delivery. Returns ``(ok, status)``.

    Never raises: a dead endpoint or a missing package degrades to the in-app
    notification that was already persisted.
    """
    if webpush is None:
        return False, "push_unavailable (install pywebpush)"
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        return False, "push_unavailable (no VAPID keys)"

    def _send() -> None:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": subscription.keys or {},
            },
            data=_payload(notification),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
        )

    try:
        # pywebpush is synchronous (requests-based); keep the event loop free.
        await asyncio.to_thread(_send)
        return True, "sent"
    except WebPushException as exc:  # pragma: no cover - network dependent
        logger.warning("web push failed for %s: %s", subscription.user_id, exc)
        return False, f"failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.warning("web push error for %s: %s", subscription.user_id, exc)
        return False, f"failed: {exc}"


async def dispatch(
    session: AsyncSession,
    user_id: Any,
    *,
    kind: str,
    title: str,
    body: str,
    url: Optional[str] = None,
    course_id: Optional[Any] = None,
) -> Notification:
    """Record a notification, then fan it out to every push endpoint."""
    notification = await record_notification(
        session,
        user_id,
        kind=kind,
        title=title,
        body=body,
        url=url,
        course_id=course_id,
    )
    subscriptions = await subscriptions_for(session, user_id)
    if not subscriptions:
        return notification
    statuses: list[str] = []
    for subscription in subscriptions:
        ok, status = await deliver_push(subscription, notification)
        statuses.append(status)
        if ok:
            break
    notification.push_status = (statuses[0] if statuses else "recorded")[:64]
    await session.flush()
    return notification


# ---------------------------------------------------------------------------
# Alert producers
# ---------------------------------------------------------------------------


async def notify_new_lecture(
    session: AsyncSession,
    *,
    student_ids: Sequence[Any],
    lecture_title: str,
    course_id: Any,
    course_name: str = "",
    url: Optional[str] = None,
) -> list[Notification]:
    """📢 'New material posted' alert to every enrolled student."""
    sent: list[Notification] = []
    for student_id in student_ids:
        sent.append(
            await dispatch(
                session,
                student_id,
                kind=NEW_LECTURE,
                title=f"📢 مادة جديدة: {lecture_title}",
                body=(
                    f"تم نشر محاضرة «{lecture_title}» في {course_name or 'الفصل'} — "
                    "جاهزة للتدريب الآن."
                ),
                url=url or "/",
                course_id=course_id,
            )
        )
    return sent


def streak_saver_due(
    now: datetime, last_active_at: Optional[datetime], *, hour: int = STREAK_SAVER_HOUR
) -> bool:
    """True when it is past ``hour`` and the learner has not studied today."""
    if now.hour < hour:
        return False
    if last_active_at is None:
        return True
    last = last_active_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.date() < now.date()


async def streak_saver_targets(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> list[User]:
    """Users who should receive the 8 PM streak-saver reminder."""
    moment = now or utcnow()
    if moment.hour < STREAK_SAVER_HOUR:
        return []
    users = (await session.execute(select(User))).scalars().all()
    return [
        user
        for user in users
        if streak_saver_due(moment, user.last_active_at) and not user.is_pro
    ]


async def notify_streak_saver(session: AsyncSession, user: User) -> Notification:
    return await dispatch(
        session,
        user.id,
        kind=STREAK_SAVER,
        title="🔥 لا تدع سلسلتك تنكسر!",
        body="حلّ تحدياً واحداً قبل منتصف الليل واحافظ على حماسك اليومي.",
        url="/",
    )


def fading_card_count(
    cards: Sequence[FsrsCard],
    *,
    now: Optional[datetime] = None,
    min_stability_days: float = FSRS_DECAY_STABILITY_DAYS,
) -> int:
    """How many review cards are due *and* have decayed to a fragile stability."""
    moment = now or utcnow()
    count = 0
    for card in cards:
        stability_days = float(card.stability or 0.0)
        due = card.due_date
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        overdue = due is not None and due <= moment
        if overdue and stability_days <= min_stability_days:
            count += 1
    return count


async def notify_fsrs_decay(
    session: AsyncSession, user: User, fading: int
) -> Notification:
    return await dispatch(
        session,
        user.id,
        kind=FSRS_DECAY,
        title="🧠 ذاكرتك تحتاج جولة سريعة",
        body=(
            f"{fading} بطاقات بدأت تتلاشى من ذاكرتك طويلة المدى. "
            "مراجعة دقيقتين تكفي لترسيخها."
        ),
        url="/play/review",
    )


async def retention_sweep(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> dict[str, int]:
    """One pass of the retention loop; returns per-kind dispatch counters.

    Idempotency: an alert of a given kind is sent at most once per calendar day
    per user, so a restart (or two workers) cannot spam the same reminder.
    """
    moment = now or utcnow()
    today = moment.date()
    counts = {STREAK_SAVER: 0, FSRS_DECAY: 0}

    users = (await session.execute(select(User))).scalars().all()
    for user in users:
        recent = (
            await session.execute(
                select(Notification.kind, Notification.created_at)
                .where(Notification.user_id == user.id)
                .where(Notification.created_at >= moment - timedelta(days=1))
            )
        ).all()
        sent_today: set[str] = set()
        for kind, created_at in recent:
            if created_at is not None:
                stamp = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                if stamp.date() == today:
                    sent_today.add(kind)

        if (
            STREAK_SAVER not in sent_today
            and not user.is_pro
            and streak_saver_due(moment, user.last_active_at)
        ):
            await notify_streak_saver(session, user)
            counts[STREAK_SAVER] += 1

        if FSRS_DECAY not in sent_today:
            cards = (
                await session.execute(
                    select(FsrsCard).where(FsrsCard.user_id == user.id).limit(500)
                )
            ).scalars().all()
            fading = fading_card_count(cards, now=moment)
            if fading >= FSRS_DECAY_MIN_CARDS:
                await notify_fsrs_decay(session, user, fading)
                counts[FSRS_DECAY] += 1

    await session.commit()
    logger.info("retention sweep complete: %s", counts)
    return counts


async def retention_loop(interval_seconds: int = RETENTION_INTERVAL_SECONDS) -> None:
    """Background task started by the app lifespan (disabled when not wanted)."""
    from database import session_scope

    while True:
        try:
            await asyncio.sleep(max(60, interval_seconds))
            async with session_scope() as session:
                await retention_sweep(session)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - a bad sweep must not kill the loop
            logger.warning("retention sweep failed: %s", exc)
