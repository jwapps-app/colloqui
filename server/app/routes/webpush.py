from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import links, webpush
from ..db import get_db
from ..deps import get_current_user
from ..models import PushSubscription, User
from ..schemas import PushSubscriptionIn

router = APIRouter(prefix="/api/v1/push", tags=["push"])

# A person's real installs (phone, tablet, laptops) fit in a handful; this
# bounds per-message fan-out and stops one account hoarding subscriptions.
MAX_SUBSCRIPTIONS_PER_USER = 20


async def _endpoint_allowed(endpoint: str) -> bool:
    """A push endpoint is always an https URL on a public push service. Anything
    else (http, loopback, LAN, link-local/metadata) is a request the server
    must not be talked into making; the endpoint is caller-supplied."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return await links.host_allowed(parsed.hostname, allow_private=False)


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
    if not await _endpoint_allowed(body.endpoint):
        raise HTTPException(400, "Push endpoint must be an https URL on a public push service")
    existing = await db.get(PushSubscription, body.endpoint)
    if existing is not None:
        existing.user_id = user.id
        existing.p256dh = body.keys.p256dh
        existing.auth = body.keys.auth
    else:
        count = await db.scalar(
            select(func.count())
            .select_from(PushSubscription)
            .where(PushSubscription.user_id == user.id)
        )
        if (count or 0) >= MAX_SUBSCRIPTIONS_PER_USER:
            raise HTTPException(400, "Too many push subscriptions for this account")
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
