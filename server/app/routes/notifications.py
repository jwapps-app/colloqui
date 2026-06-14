import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Notification, User, utcnow
from ..schemas import NotificationOut

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Notification]:
    notifications = await db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    return list(notifications)


@router.post("/read-all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    return {"ok": True}


@router.delete("", status_code=204)
async def clear_notifications(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    """Clear (delete) all of the caller's notifications."""
    await db.execute(delete(Notification).where(Notification.user_id == user.id))


@router.delete("/{notification_id}", status_code=204)
async def dismiss_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Dismiss (delete) a single notification."""
    n = await db.get(Notification, notification_id)
    if n is not None and n.user_id == user.id:
        await db.delete(n)
