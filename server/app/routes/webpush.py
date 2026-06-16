from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .. import webpush
from ..db import get_db
from ..deps import get_current_user
from ..models import PushSubscription, User
from ..schemas import PushSubscriptionIn

router = APIRouter(prefix="/api/v1/push", tags=["push"])


@router.get("/vapid")
async def vapid_key() -> dict:
    """The VAPID application server key the client subscribes with. Empty only
    if key resolution failed — the client then skips subscribing."""
    return {"key": webpush.public_key()}


@router.post("/test")
async def test_push(user: User = Depends(get_current_user)) -> dict:
    """Send a test notification to the caller's own subscriptions and report the
    per-device delivery outcome — a no-server-access way to diagnose push."""
    return await webpush.send_test(user.id)


@router.post("/subscribe", status_code=204)
async def subscribe(
    body: PushSubscriptionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Save (or reassign) this PWA's push subscription to the current user.
    Idempotent: the same endpoint re-subscribing just refreshes its keys."""
    existing = await db.get(PushSubscription, body.endpoint)
    if existing is not None:
        existing.user_id = user.id
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
    else:
        db.add(
            PushSubscription(
                endpoint=body.endpoint,
                user_id=user.id,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
            )
        )


@router.delete("/subscribe", status_code=204)
async def unsubscribe(
    body: PushSubscriptionIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Drop this PWA's subscription (call on logout / permission revoke)."""
    existing = await db.get(PushSubscription, body.endpoint)
    if existing is not None and existing.user_id == user.id:
        await db.delete(existing)
