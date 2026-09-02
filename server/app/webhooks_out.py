"""Outgoing webhooks: POST signed events to registered subscriber URLs.

Best effort and fire-and-forget, so delivery never blocks or breaks the request
that triggered it. Subscribers should dedupe on the event id and fall back to
the /sync feed for completeness.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from . import links
from .config import settings
from .db import SessionLocal
from .models import EventSubscription, utcnow

log = logging.getLogger("colloqui.webhooks_out")

# Strong refs to scheduled dispatch tasks — the event loop holds tasks only
# weakly, so an unreferenced create_task() can be GC'd before it delivers.
_inflight: set[asyncio.Task] = set()

# One shared client: reuses connections instead of a fresh TCP+TLS handshake
# per delivery (same pattern as push.py's relay client).
_client: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5)
    return _client


async def _deliver(url: str, secret: str, body: bytes, event_type: str) -> None:
    # Refuse unsafe destinations at delivery time too (not just registration,
    # since DNS can change): http(s) only, and every resolved address must pass
    # the outbound guard. Loopback, link-local/metadata, reserved and multicast
    # are always refused; LAN hosts are allowed unless WEBHOOK_BLOCK_PRIVATE.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not await links.host_allowed(
        parsed.hostname or "", allow_private=not settings.webhook_block_private
    ):
        log.warning("outgoing webhook %s -> %s refused: unsafe destination", event_type, url)
        return
    delivery_id = str(uuid.uuid4())
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Colloqui-Event": event_type,
        "X-Colloqui-Delivery": delivery_id,
        "X-Colloqui-Signature": f"sha256={sig}",
    }
    client = _http_client()
    for attempt in range(2):  # one retry, then give up
        try:
            r = await client.post(url, content=body, headers=headers)
            if r.status_code < 500:
                return
        except Exception:
            pass
        if attempt == 0:
            await asyncio.sleep(1)
    log.warning("outgoing webhook %s -> %s failed after retries", event_type, url)


async def _dispatch(event_type: str, data: dict) -> None:
    async with SessionLocal() as db:
        subs = (
            await db.scalars(
                select(EventSubscription).where(EventSubscription.active.is_(True))
            )
        ).all()
        # Snapshot the fields before the session closes; filter by event allowlist.
        targets = [
            (s.url, s.secret)
            for s in subs
            if not s.events or event_type in {e.strip() for e in s.events.split(",")}
        ]
    if not targets:
        return
    payload = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "sent_at": utcnow().isoformat(),
        "data": data,
    }
    body = json.dumps(payload, default=str).encode()
    await asyncio.gather(
        *[_deliver(url, secret, body, event_type) for url, secret in targets],
        return_exceptions=True,
    )


def dispatch_event(event_type: str, data: dict) -> None:
    """Schedule delivery to subscribers without blocking the caller."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop (e.g. a sync script); nothing to schedule onto
    task = loop.create_task(_dispatch(event_type, data))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
