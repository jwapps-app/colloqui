"""Web Push (PWA notifications) via VAPID, signed with our own keys and POSTed
straight to the browser's push service — no third-party gateway. This sits
behind the same `notify.notify_user()` dispatch seam as the APNs path (push.py).

Push is a silent no-op until the VAPID_* settings are configured. Sends are
fire-and-forget off the request path; subscriptions the push service reports as
gone (404 / 410) are pruned. pywebpush is synchronous, so each send is handed
to a worker thread to avoid blocking the event loop.
"""
import asyncio
import json
import logging
import uuid

from sqlalchemy import delete, func, select

from .config import settings
from .db import SessionLocal
from .models import Notification, PushSubscription

log = logging.getLogger("webpush")

_vapid_cache: dict = {}


def web_push_enabled() -> bool:
    return bool(
        settings.vapid_private_key
        and settings.vapid_public_key
        and settings.vapid_subject
    )


def _vapid():
    """The Vapid signer built from the base64url private key, cached. Returns
    None if the key is missing or unparseable (web push then stays off)."""
    if "obj" in _vapid_cache:
        return _vapid_cache["obj"]
    obj = None
    if settings.vapid_private_key:
        try:
            from py_vapid import Vapid01

            obj = Vapid01.from_raw(settings.vapid_private_key.encode())
        except Exception:
            log.exception("invalid VAPID_PRIVATE_KEY — web push disabled")
    _vapid_cache["obj"] = obj
    return obj


async def _deliver(user_id: uuid.UUID, title: str, body: str, data: dict | None) -> None:
    from pywebpush import WebPushException, webpush

    vapid = _vapid()
    if vapid is None:
        return
    async with SessionLocal() as db:
        subs = (
            await db.scalars(
                select(PushSubscription).where(PushSubscription.user_id == user_id)
            )
        ).all()
        if not subs:
            return
        badge = await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        payload = json.dumps(
            {"title": title, "body": body, "data": data or {}, "badge": badge or 0}
        )
        dead: list[str] = []
        for sub in subs:
            info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            }
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=info,
                    data=payload,
                    vapid_private_key=vapid,
                    # Fresh dict per call: pywebpush mutates it (adds aud/exp).
                    vapid_claims={"sub": settings.vapid_subject},
                    ttl=86400,
                )
            except WebPushException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):
                    dead.append(sub.endpoint)
                else:
                    log.warning("web push failed (%s): %s", status, e)
            except Exception:
                log.exception("web push send error")
        if dead:
            await db.execute(
                delete(PushSubscription).where(PushSubscription.endpoint.in_(dead))
            )
            await db.commit()


async def _safe_deliver(user_id, title, body, data) -> None:
    try:
        await _deliver(user_id, title, body, data)
    except Exception:
        log.exception("web push delivery failed for user %s", user_id)


def schedule(user_id: uuid.UUID, title: str, body: str, data: dict | None = None) -> None:
    """Fire-and-forget a web push to all of a user's PWA subscriptions. No-op
    unless VAPID is configured. Never blocks or raises into the caller."""
    if not web_push_enabled():
        return
    asyncio.create_task(_safe_deliver(user_id, title, body, data))
