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

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .models import EventSubscription, utcnow

log = logging.getLogger("colloqui.webhooks_out")


async def _deliver(url: str, secret: str, body: bytes, event_type: str) -> None:
    delivery_id = str(uuid.uuid4())
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Colloqui-Event": event_type,
        "X-Colloqui-Delivery": delivery_id,
        "X-Colloqui-Signature": f"sha256={sig}",
    }
    async with httpx.AsyncClient(timeout=5) as client:
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
    loop.create_task(_dispatch(event_type, data))
