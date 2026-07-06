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

# One shared client for all relay traffic: a client per delivery paid a fresh
# TCP+TLS handshake to the relay every time, and a channel-wide fanout opened
# dozens at once. The connection limit also caps that thundering herd.
_client: httpx.AsyncClient | None = None

# Keep strong references to in-flight sends — the event loop holds tasks only
# weakly, so an unreferenced create_task() can be garbage-collected mid-flight
# (the push silently never arrives).
_inflight: set[asyncio.Task] = set()


def _relay_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _client


def push_enabled() -> bool:
    return bool(
        settings.push_relay_url
        and settings.push_relay_api_key
        and settings.apns_topic  # bundle id the relay routes on
    )


async def _post_one(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    token: str,
    environment: str,
    title: str,
    body: str,
    custom: dict | None,
    badge: int,
) -> str | None:
    """Send one push; return the token if the relay says it's dead, else None."""
    payload: dict = {
        "bundle_id": settings.apns_topic,
        "device_token": token,
        "title": title,
        "body": body,
        "badge": badge,
        # Per-token environment: debug builds register sandbox tokens.
        "sandbox": environment == "sandbox",
    }
    if custom:
        payload["custom_data"] = custom
    try:
        resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("relay send failed: %s", e)
        return None
    if resp.status_code == 200:
        return None
    reason = ""
    try:
        reason = resp.json().get("detail", "")
    except Exception:
        pass
    if any(r in reason for r in _DEAD):
        return token
    log.warning("relay %s (%s) for token %s…", resp.status_code, reason, token[:8])
    return None


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> None:
    if not push_enabled():
        return
    # Short session: fetch the tokens and let go — holding a pooled connection
    # across seconds of network I/O starved the pool under channel-wide fanouts.
    async with SessionLocal() as db:
        rows = (
            await db.scalars(
                select(DeviceToken).where(DeviceToken.user_id == user_id)
            )
        ).all()
        tokens = [(dt.token, dt.environment) for dt in rows]
    if not tokens:
        return

    url = settings.push_relay_url.rstrip("/") + "/notify"
    headers = {"X-API-Key": settings.push_relay_api_key}
    # Custom keys for deep-linking (channel_id, root_id, message_id, …).
    custom = {k: v for k, v in (data or {}).items() if v is not None} or None

    client = _relay_client()
    # A user's devices are pushed concurrently (was serial: 3 devices = 3 full
    # relay round-trips back to back).
    results = await asyncio.gather(
        *[
            _post_one(client, url, headers, tok, env, title, body, custom, badge)
            for tok, env in tokens
        ],
        return_exceptions=True,
    )
    dead = [r for r in results if isinstance(r, str)]
    if dead:
        async with SessionLocal() as db:
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
    task = asyncio.create_task(_safe_deliver(user_id, title, body, data, badge))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
