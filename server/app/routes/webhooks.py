import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Channel, ChannelMember, Message, User, Webhook, utcnow
from ..schemas import WebhookCreatedOut, WebhookIn, WebhookOut, WebhookPostIn
from ..security import RateLimiter, hash_token, new_token
from .channels import require_member
from .messages import _notify_for_message, broadcast, message_out

router = APIRouter(prefix="/api/v1", tags=["webhooks"])
# Ingest lives at a short, prefix-less URL (/hooks/{token}) for clean webhook URLs.
public_router = APIRouter(tags=["webhooks"])

# Cap each webhook's posting rate (per-process, sliding window).
_hook_limiter = RateLimiter(limit=30, window_seconds=60)


async def require_channel_manager(
    db: AsyncSession, channel_id: uuid.UUID, user: User
) -> Channel:
    channel = await require_member(db, channel_id, user)
    if channel.is_dm:
        raise HTTPException(400, "DMs can't have webhooks")
    member = await db.get(ChannelMember, (channel_id, user.id))
    if not (user.is_admin or (member and member.role == "owner")):
        raise HTTPException(403, "Only the channel owner or an admin can manage webhooks")
    return channel


@router.post(
    "/channels/{channel_id}/webhooks", response_model=WebhookCreatedOut, status_code=201
)
async def create_webhook(
    channel_id: uuid.UUID,
    body: WebhookIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookCreatedOut:
    await require_channel_manager(db, channel_id, user)
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[WebhookOut]:
    await require_channel_manager(db, channel_id, user)
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    hook = await db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(404, "Webhook not found")
    await require_channel_manager(db, hook.channel_id, user)
    await db.delete(hook)


@public_router.post("/hooks/{token}", status_code=201)
async def ingest(
    token: str,
    body: WebhookPostIn,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Public ingest endpoint — auth is the secret token in the URL."""
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
    sender_id = hook.created_by or channel.created_by
    sender = await db.get(User, sender_id)
    display_name = (body.name or hook.name).strip()[:64]
    message = Message(
        channel_id=channel.id,
        sender_id=sender_id,
        content=body.text.strip(),
        webhook_name=display_name,
    )
    db.add(message)
    hook.last_used_at = utcnow()
    await db.flush()
    out = message_out(message, sender)
    await broadcast(
        db, channel.id, {"type": "message.created", "message": jsonable_encoder(out)}
    )
    if sender is not None:
        await _notify_for_message(db, channel, sender, message.content)
    return {"ok": True}
