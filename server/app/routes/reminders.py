import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Message, Reminder, User, utcnow
from ..schemas import ReminderIn, ReminderOut
from .channels import require_member

router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])

MAX_PENDING = 100


@router.post("", response_model=ReminderOut, status_code=201)
async def create_reminder(
    body: ReminderIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Reminder:
    if body.due_at.tzinfo is None:
        raise HTTPException(400, "due_at must include a timezone")
    now = utcnow()
    if body.due_at <= now:
        raise HTTPException(400, "Reminder time must be in the future")
    if body.due_at > now + timedelta(days=365):
        raise HTTPException(400, "Reminders can be at most a year out")

    pending = await db.scalar(
        select(func.count())
        .select_from(Reminder)
        .where(Reminder.user_id == user.id, Reminder.fired_at.is_(None))
    )
    if pending >= MAX_PENDING:
        raise HTTPException(400, f"You already have {MAX_PENDING} pending reminders")

    channel_id = body.channel_id
    if body.message_id is not None:
        message = await db.get(Message, body.message_id)
        if message is None or message.deleted_at is not None:
            raise HTTPException(404, "Message not found")
        await require_member(db, message.channel_id, user)
        channel_id = message.channel_id
    elif channel_id is not None:
        await require_member(db, channel_id, user)

    reminder = Reminder(
        user_id=user.id,
        text=body.text.strip(),
        due_at=body.due_at,
        channel_id=channel_id,
        message_id=body.message_id,
    )
    db.add(reminder)
    await db.flush()
    return reminder


@router.get("", response_model=list[ReminderOut])
async def list_pending_reminders(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Reminder]:
    reminders = await db.scalars(
        select(Reminder)
        .where(Reminder.user_id == user.id, Reminder.fired_at.is_(None))
        .order_by(Reminder.due_at)
        .limit(MAX_PENDING)
    )
    return list(reminders)


@router.delete("/{reminder_id}", status_code=204)
async def delete_reminder(
    reminder_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    reminder = await db.get(Reminder, reminder_id)
    if reminder is None or reminder.user_id != user.id:
        raise HTTPException(404, "Reminder not found")
    await db.delete(reminder)
