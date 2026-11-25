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

from . import background, storage
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
    return storage.vapid_key_path()


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


async def _endpoint_ok(endpoint: str) -> bool:
    from urllib.parse import urlparse

    from . import links

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return await links.host_allowed(parsed.hostname, allow_private=False)


def public_key() -> str:
    return _keys["public"] if _keys else ""


async def _post(
    keys: dict, endpoint: str, p256dh: str, auth: str, payload: str, ttl: int
) -> tuple[bool, int | None, str | None]:
    """POST one push to one subscription. Returns (ok, HTTP status, error
    text). The ONLY place the push library is called, so delivery and the
    diagnostic test cannot disagree about how a push is sent."""
    from pywebpush import WebPushException, webpush

    info = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
    try:
        resp = await asyncio.to_thread(
            webpush,
            subscription_info=info,
            data=payload,
            vapid_private_key=keys["vapid"],
            # Fresh dict per call: pywebpush mutates it (adds aud/exp).
            vapid_claims={"sub": keys["subject"]},
            ttl=ttl,
            # pywebpush uses requests, whose default is NO timeout; a hung
            # push service would otherwise pin a worker thread forever.
            timeout=10,
        )
        return True, getattr(resp, "status_code", 0), None
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        return False, status, str(e)[:160]
    except Exception as e:  # noqa: BLE001
        log.exception("web push send error")
        return False, None, str(e)[:160]


async def _subscriptions(user_id: uuid.UUID) -> list[tuple[str, str, str]]:
    # Short session: snapshot the subscriptions and let the connection go —
    # holding it across the network fan-out starved the pool under load (the
    # exact starvation push.py already fixed for APNs tokens).
    async with SessionLocal() as db:
        return [
            (s.endpoint, s.p256dh, s.auth)
            for s in (
                await db.scalars(
                    select(PushSubscription).where(PushSubscription.user_id == user_id)
                )
            ).all()
        ]


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> None:
    keys = _keys
    if keys is None:
        return
    # Defence in depth for rows that predate endpoint validation at subscribe
    # time: never POST to anything but an https public push service.
    subs = [s for s in await _subscriptions(user_id) if await _endpoint_ok(s[0])]
    if not subs:
        return
    payload = json.dumps(
        {"title": title, "body": body, "data": data or {}, "badge": badge}
    )
    # Fan the sends out concurrently — a user with 2-3 PWA installs otherwise
    # paid that many push-service round-trips back to back (mirrors push.py).
    results = await asyncio.gather(
        *[_post(keys, endpoint, p256dh, auth, payload, 86400) for endpoint, p256dh, auth in subs]
    )
    dead = []
    for (endpoint, _, _), (ok, status, error) in zip(subs, results):
        if ok:
            continue
        if status in (404, 410):
            dead.append(endpoint)  # the push service says this subscription is gone
        else:
            log.warning("web push failed (%s): %s", status, error)
    if dead:
        # Re-open a session only for the prune, after the network work is done.
        async with SessionLocal() as db:
            await db.execute(
                delete(PushSubscription).where(PushSubscription.endpoint.in_(dead))
            )
            await db.commit()


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
    background.spawn(
        _deliver(user_id, title, body, data, badge), f"web push to user {user_id}"
    )


async def send_test(user_id: uuid.UUID) -> dict:
    """Diagnostic: synchronously push a test notification to each of the user's
    subscriptions and report the per-subscription outcome. Unlike _deliver it
    does NOT prune, so failures (and their status codes) stay visible."""
    keys = _keys
    if keys is None:
        return {"enabled": False, "count": 0, "results": []}
    payload = json.dumps(
        {
            "title": "Colloqui test",
            "body": "If you can see this, push notifications work.",
            "data": {"test": True},  # tells the SW to show even if a window is focused
            "badge": 0,
        }
    )
    results: list[dict] = []
    for endpoint, p256dh, auth in await _subscriptions(user_id):
        host = endpoint.split("/")[2] if "://" in endpoint else endpoint[:40]
        if not await _endpoint_ok(endpoint):
            results.append({"host": host, "ok": False, "status": None,
                            "error": "endpoint refused: not a public https push service"})
            continue
        ok, status, error = await _post(keys, endpoint, p256dh, auth, payload, 60)
        result = {"host": host, "ok": ok, "status": status}
        if not ok:
            result["error"] = error
        results.append(result)
    return {"enabled": True, "count": len(results), "results": results}
