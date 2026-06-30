"""Native iOS push via APNs, delivered through the self-hosted push-relay
(one shared signing key + central metrics for all our apps). This is the
implementation behind the single `notify.notify_user()` dispatch seam.

Push is a silent no-op until the PUSH_RELAY_* settings are configured, so the
server runs fine without them. Sends are fire-and-forget off the request path;
tokens the relay reports dead (BadDeviceToken / Unregistered) are pruned.
"""
import asyncio
import logging
import uuid

import httpx
from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal
from .models import DeviceToken

log = logging.getLogger("push")

# Reasons the relay passes back from Apple that mean the token is gone for good.
_DEAD = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def push_enabled() -> bool:
    return bool(
        settings.push_relay_url
        and settings.push_relay_api_key
        and settings.apns_topic  # bundle id the relay routes on
    )


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> None:
    if not push_enabled():
        return
    async with SessionLocal() as db:
        tokens = (
            await db.scalars(
                select(DeviceToken).where(DeviceToken.user_id == user_id)
            )
        ).all()
        if not tokens:
            return
        url = settings.push_relay_url.rstrip("/") + "/notify"
        headers = {"X-API-Key": settings.push_relay_api_key}
        # Custom keys for deep-linking (channel_id, root_id, message_id, …).
        custom = {k: v for k, v in (data or {}).items() if v is not None} or None
        dead: list[str] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for dt in tokens:
                payload: dict = {
                    "bundle_id": settings.apns_topic,
                    "device_token": dt.token,
                    "title": title,
                    "body": body,
                    "badge": badge,
                    # Per-token environment: debug builds register sandbox tokens.
                    "sandbox": dt.environment == "sandbox",
                }
                if custom:
                    payload["custom_data"] = custom
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as e:
                    log.warning("relay send failed: %s", e)
                    continue
                if resp.status_code == 200:
                    continue
                reason = ""
                try:
                    reason = resp.json().get("detail", "")
                except Exception:
                    pass
                if any(r in reason for r in _DEAD):
                    dead.append(dt.token)
                else:
                    log.warning(
                        "relay %s (%s) for token %s…",
                        resp.status_code, reason, dt.token[:8],
                    )
        if dead:
            await db.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
            await db.commit()


async def _safe_deliver(user_id, title, body, data, badge) -> None:
    try:
        await _deliver(user_id, title, body, data, badge)
    except Exception:
        log.exception("push delivery failed for user %s", user_id)


def schedule(
    user_id: uuid.UUID,
    title: str,
    body: str,
    data: dict | None = None,
    badge: int = 0,
) -> None:
    """Fire-and-forget an APNs push (via the relay) to all of a user's devices.
    `badge` is the unread total (computed by the caller in the same transaction).
    No-op unless the relay is configured; never blocks or raises into the caller."""
    if not push_enabled():
        return
    asyncio.create_task(_safe_deliver(user_id, title, body, data, badge))
