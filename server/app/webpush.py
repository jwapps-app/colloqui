"""Web Push (PWA notifications) via VAPID, signed with our own keys and POSTed
straight to the browser's push service — no third-party gateway. This sits
behind the same `notify.notify_user()` dispatch seam as the APNs path (push.py).

Keys are resolved once at startup (`ensure_keys()`):
  1. explicit VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY env (for power users / shared
     keys across instances), else
  2. an auto-managed pair persisted in the data dir — generated on first boot so
     web push works out of the box with zero configuration.
VAPID_SUBJECT defaults to the site ORIGIN when unset.

Sends are fire-and-forget off the request path; subscriptions the push service
reports as gone (404 / 410) are pruned. pywebpush is synchronous, so each send
is handed to a worker thread to avoid blocking the event loop.
"""
import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path

from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal
from .models import PushSubscription

log = logging.getLogger("webpush")

# Resolved keypair: {"vapid": Vapid01, "public": str, "subject": str} or None
# (None = web push disabled). Set by ensure_keys() at startup; never resolved
# lazily on the hot path, so a server that never calls ensure_keys (e.g. tests)
# keeps web push cleanly off.
_keys: dict | None = None


def _key_file() -> Path:
    """Where the auto-managed keypair is persisted — on the data volume, so it
    survives restarts (a changed key would invalidate every subscription)."""
    return Path(settings.upload_dir) / "vapid.json"


def _public_b64(vapid) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = vapid.public_key.public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _from_env() -> dict | None:
    if not (settings.vapid_private_key and settings.vapid_public_key):
        return None
    try:
        from py_vapid import Vapid01

        return {
            "vapid": Vapid01.from_raw(settings.vapid_private_key.encode()),
            "public": settings.vapid_public_key,
        }
    except Exception:
        log.exception("invalid VAPID_* env keys; falling back to auto-managed")
        return None


def _load_or_generate() -> dict | None:
    """Load the persisted auto-managed keypair, or generate + persist a fresh
    one. Returns None if generation/persistence fails (web push stays off)."""
    from py_vapid import Vapid01

    path = _key_file()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return {
                "vapid": Vapid01.from_raw(data["private"].encode()),
                "public": data["public"],
            }
    except Exception:
        log.exception("unreadable VAPID key file %s; regenerating", path)
    try:
        vapid = Vapid01()
        vapid.generate_keys()
        priv = (
            base64.urlsafe_b64encode(
                vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
            )
            .rstrip(b"=")
            .decode()
        )
        public = _public_b64(vapid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"private": priv, "public": public}))
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("generated and persisted a VAPID keypair at %s", path)
        return {"vapid": Vapid01.from_raw(priv.encode()), "public": public}
    except Exception:
        log.exception("VAPID key generation failed; web push disabled")
        return None


def ensure_keys() -> dict | None:
    """Resolve the VAPID keypair once, at startup. Env keys win; otherwise an
    auto-managed pair is loaded/generated. Idempotent."""
    global _keys
    pair = _from_env() or _load_or_generate()
    if pair is not None:
        pair["subject"] = settings.vapid_subject or settings.origin
    _keys = pair
    return _keys


def reset_cache() -> None:
    """Drop the resolved keypair (tests re-resolve via ensure_keys)."""
    global _keys
    _keys = None


def web_push_enabled() -> bool:
    return _keys is not None


def public_key() -> str:
    return _keys["public"] if _keys else ""


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> None:
    from pywebpush import WebPushException, webpush

    keys = _keys
    if keys is None:
        return
    async with SessionLocal() as db:
        subs = (
            await db.scalars(
                select(PushSubscription).where(PushSubscription.user_id == user_id)
            )
        ).all()
        if not subs:
            return
        payload = json.dumps(
            {"title": title, "body": body, "data": data or {}, "badge": badge}
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
                    vapid_private_key=keys["vapid"],
                    # Fresh dict per call: pywebpush mutates it (adds aud/exp).
                    vapid_claims={"sub": keys["subject"]},
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


async def _safe_deliver(user_id, title, body, data, badge) -> None:
    try:
        await _deliver(user_id, title, body, data, badge)
    except Exception:
        log.exception("web push delivery failed for user %s", user_id)


def schedule(
    user_id: uuid.UUID,
    title: str,
    body: str,
    data: dict | None = None,
    badge: int = 0,
) -> None:
    """Fire-and-forget a web push to all of a user's PWA subscriptions. `badge`
    is the unread total to show on the app icon (computed by the caller in the
    same transaction). No-op unless keys are configured; never blocks or raises
    into the caller."""
    if not web_push_enabled():
        return
    asyncio.create_task(_safe_deliver(user_id, title, body, data, badge))
