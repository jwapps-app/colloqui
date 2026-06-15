import asyncio
import logging
import uuid

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import push, webpush
from .db import SessionLocal
from .models import Notification, Reminder, utcnow
from .schemas import NotificationOut
from .ws import manager

log = logging.getLogger("notify")

REMINDER_TICK_SECONDS = 20


async def notify_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    type_: str,
    title: str,
    body: str,
    data: dict | None = None,
    inbox: bool = True,
) -> Notification | None:
    """Single dispatch point for all user notifications.

    `inbox=True` (mentions, DMs, thread replies, reminders): stores a persistent
    entry shown in the 🔔 list with an unread badge. `inbox=False` ("All"-level
    channel chatter): a transient live alert — a desktop/browser popup now, and
    a push once the iOS app exists — that never fills the inbox. Either way the
    future APNs delivery hooks in HERE; producers stay transport-agnostic.
    """
    notification = None
    if inbox:
        notification = Notification(
            user_id=user_id, type=type_, title=title, body=body, data=data
        )
        db.add(notification)
        await db.flush()
        await manager.send_to_users(
            [user_id],
            {
                "type": "notification",
                "notification": jsonable_encoder(
                    NotificationOut.model_validate(notification)
                ),
            },
        )
    else:
        await manager.send_to_users(
            [user_id],
            {"type": "alert", "title": title, "body": body, "data": data},
        )
    # Badge count = this user's unread notifications. Compute it HERE, in the
    # same transaction that just added the new one (visible after flush), so the
    # number we ship is correct. Computing it later in the push task's own
    # session raced the request's commit and shipped a stale/too-low count —
    # which is why the app-icon badge often didn't update until you opened the
    # app and it recomputed.
    badge = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    # Push to the user's other surfaces (both inbox items and transient
    # "all"-level alerts). Each is a no-op unless configured; fire-and-forget,
    # never blocks here. APNs reaches the native iOS app; web push reaches
    # installed PWAs (incl. iOS) when they're backgrounded or closed.
    push.schedule(user_id, title, body, data, badge or 0)
    webpush.schedule(user_id, title, body, data, badge or 0)
    return notification


async def reminder_loop() -> None:
    """Fires due reminders. Reminders missed while the server was down fire
    on the first tick after startup — late beats lost."""
    while True:
        try:
            await asyncio.sleep(REMINDER_TICK_SECONDS)
            async with SessionLocal() as db:
                due = (
                    await db.scalars(
                        select(Reminder)
                        .where(Reminder.fired_at.is_(None), Reminder.due_at <= utcnow())
                        .order_by(Reminder.due_at)
                        .limit(100)
                    )
                ).all()
                for reminder in due:
                    reminder.fired_at = utcnow()
                    await notify_user(
                        db,
                        reminder.user_id,
                        "reminder",
                        "⏰ Reminder",
                        reminder.text,
                        {
                            "reminder_id": str(reminder.id),
                            "channel_id": str(reminder.channel_id)
                            if reminder.channel_id
                            else None,
                        },
                    )
                if due:
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder loop tick failed")
