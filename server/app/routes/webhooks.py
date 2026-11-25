import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import messaging
from ..access import is_channel_manager, require_member
from ..config import settings
from ..db import DB
from ..deps import client_ip, get_current_user
from ..models import Channel, User, Webhook, utcnow
from ..schemas import WebhookCreatedOut, WebhookIn, WebhookOut, WebhookPostIn
from ..security import RateLimiter, hash_token, new_token

router = APIRouter(prefix="/api/v1", tags=["webhooks"])
# Ingest lives at a short, prefix-less URL (/hooks/{token}) for clean webhook URLs.
public_router = APIRouter(tags=["webhooks"])

# Cap each webhook's posting rate (per-process, sliding window).
_hook_limiter = RateLimiter(limit=30, window_seconds=60)
# Cap ingest attempts per client IP BEFORE token lookup, so the public,
# unauthenticated endpoint can't be used to hammer the DB with bad tokens.
_hook_ip_limiter = RateLimiter(limit=60, window_seconds=60)


async def require_webhook_manager(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    """A webhook posts into the channel and its listing shows what is wired to
    it, so managing one needs channel MEMBERSHIP as well as the manager rule:
    an admin who is not in a private channel can't attach a webhook to it."""
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "DMs can't have webhooks")
    if not await is_channel_manager(db, channel_id, user):
        raise HTTPException(403, "Only the channel owner or an admin can manage webhooks")
    return channel


@router.post(
    "/channels/{channel_id}/webhooks", response_model=WebhookCreatedOut, status_code=201
)
async def create_webhook(
    channel_id: uuid.UUID,
    body: WebhookIn,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> WebhookCreatedOut:
    await require_webhook_manager(db, channel_id, user)
    token = new_token()
    hook = Webhook(
        channel_id=channel_id,
        name=body.name.strip(),
        token_hash=hash_token(token),
        created_by=user.id,
    )
    db.add(hook)
    await db.flush()
    return WebhookCreatedOut(
        id=hook.id,
        name=hook.name,
        created_at=hook.created_at,
        last_used_at=None,
        url=f"{settings.origin}/hooks/{token}",
    )


@router.get("/channels/{channel_id}/webhooks", response_model=list[WebhookOut])
async def list_webhooks(
    channel_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> list[WebhookOut]:
    await require_webhook_manager(db, channel_id, user)
    hooks = (
        await db.scalars(
            select(Webhook)
            .where(Webhook.channel_id == channel_id)
            .order_by(Webhook.created_at)
        )
    ).all()
    return [WebhookOut.model_validate(h) for h in hooks]


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: uuid.UUID,
    db: AsyncSession = DB,
    user: User = Depends(get_current_user),
) -> None:
    hook = await db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(404, "Webhook not found")
    await require_webhook_manager(db, hook.channel_id, user)
    await db.delete(hook)


@public_router.post("/hooks/{token}", status_code=201)
async def ingest(
    request: Request,
    token: str,
    body: WebhookPostIn,
    db: AsyncSession = DB,
) -> dict:
    """Public ingest endpoint — auth is the secret token in the URL."""
    if not _hook_ip_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests")
    hook = await db.scalar(
        select(Webhook).where(Webhook.token_hash == hash_token(token))
    )
    if hook is None:
        raise HTTPException(404, "Unknown webhook")
    if not _hook_limiter.allow(str(hook.id)):
        raise HTTPException(429, "Too many requests")
    channel = await db.get(Channel, hook.channel_id)
    if channel is None:
        raise HTTPException(404, "Channel no longer exists")
    # FK requires a real sender; the displayed identity is the webhook name.
    sender = await db.get(User, hook.created_by or channel.created_by)
    if sender is None:
        raise HTTPException(404, "Channel no longer exists")
    hook.last_used_at = utcnow()
    # Webhook-posted messages are real messages: same change-log, live event,
    # integration event and notifications as any other. The one difference is
    # deliberate: the server does not go and fetch link previews for URLs that
    # an external system posted (outbound requests happen only for links a
    # person pasted).
    await messaging.post_message(
        db,
        channel,
        sender,
        body.text,
        webhook_name=(body.name or hook.name).strip()[:64],
        fetch_previews=False,
    )
    return {"ok": True}
