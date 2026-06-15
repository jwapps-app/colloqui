"""Native iOS push via APNs, sent directly to Apple with our own signing key
(no third-party push gateway). This is the implementation behind the single
`notify.notify_user()` dispatch seam.

Push is a silent no-op until the APNS_* settings are configured, so the server
runs fine without them. Sends are fire-and-forget off the request path; tokens
Apple rejects as dead (410 / BadDeviceToken) are pruned.
"""
import asyncio
import json
import logging
import time
import uuid

import httpx
import jwt
from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal
from .models import DeviceToken

log = logging.getLogger("push")

_HOST_PROD = "https://api.push.apple.com"
_HOST_SANDBOX = "https://api.sandbox.push.apple.com"

# Provider JWT is reused for ~50 min (Apple allows up to 60).
_jwt_cache: dict = {"token": None, "exp": 0.0}


def _key_pem() -> str | None:
    if settings.apns_key:
        return settings.apns_key
    if settings.apns_key_path:
        try:
            with open(settings.apns_key_path) as f:
                return f.read()
        except OSError:
            log.warning("APNS_KEY_PATH set but unreadable: %s", settings.apns_key_path)
    return None


def push_enabled() -> bool:
    return bool(
        _key_pem()
        and settings.apns_key_id
        and settings.apns_team_id
        and settings.apns_topic
    )


def _provider_jwt(key_pem: str, now: float) -> str:
    if _jwt_cache["token"] and _jwt_cache["exp"] > now:
        return _jwt_cache["token"]
    token = jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(now)},
        key_pem,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )
    _jwt_cache["token"] = token
    _jwt_cache["exp"] = now + 3000  # 50 minutes
    return token


def _build_payload(title: str, body: str, data: dict | None, badge: int) -> bytes:
    aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    payload: dict = {"aps": aps}
    if data:
        # Custom keys for deep-linking (channel_id, root_id, message_id, …).
        for k, v in data.items():
            if v is not None:
                payload[k] = v
    return json.dumps(payload).encode()


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> None:
    key_pem = _key_pem()
    if not push_enabled() or key_pem is None:
        return
    async with SessionLocal() as db:
        tokens = (
            await db.scalars(
                select(DeviceToken).where(DeviceToken.user_id == user_id)
            )
        ).all()
        if not tokens:
            return
        host = _HOST_SANDBOX if settings.apns_sandbox else _HOST_PROD
        provider = _provider_jwt(key_pem, time.time())
        content = _build_payload(title, body, data, badge)
        headers = {
            "authorization": f"bearer {provider}",
            "apns-topic": settings.apns_topic,
            "apns-push-type": "alert",
        }
        dead: list[str] = []
        async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
            for dt in tokens:
                try:
                    resp = await client.post(
                        f"{host}/3/device/{dt.token}", headers=headers, content=content
                    )
                except httpx.HTTPError as e:
                    log.warning("APNs send failed: %s", e)
                    continue
                if resp.status_code == 200:
                    continue
                reason = ""
                try:
                    reason = resp.json().get("reason", "")
                except Exception:
                    pass
                if resp.status_code == 410 or reason in (
                    "BadDeviceToken",
                    "Unregistered",
                    "DeviceTokenNotForTopic",
                ):
                    dead.append(dt.token)
                else:
                    log.warning(
                        "APNs %s (%s) for token %s…",
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
    """Fire-and-forget an APNs push to all of a user's devices. `badge` is the
    unread total (computed by the caller in the same transaction). No-op unless
    APNs is configured; never blocks or raises into the caller."""
    if not push_enabled():
        return
    asyncio.create_task(_safe_deliver(user_id, title, body, data, badge))
