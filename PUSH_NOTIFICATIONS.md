# Push Notifications — Colloqui

How Colloqui delivers iOS push notifications through the self-hosted
**push-relay** (one shared Apple `.p8` key + central Grafana metrics for all our
apps), **without disturbing the existing PWA web-push path**.

The relay is already deployed and configured for this app
(`com.jworthington.colloqui`). This doc covers:
1. The backend change (reroute the existing APNs path through the relay).
2. The brand-new native iOS app (built from scratch).

---

## What already exists (and what's changing)

Colloqui already has TWO independent notification transports, joined only at the
`notify.notify_user()` dispatch seam:

```
                       notify.notify_user()           ← unchanged
                        ├── push.schedule(...)     →  APNs   (device_tokens)     ← THIS path changes
                        └── webpush.schedule(...)  →  Web Push (push_subscriptions) ← UNTOUCHED
```

- **APNs path** = `app/push.py` + `DeviceToken`/`device_tokens` table. Today it
  signs a JWT and POSTs straight to Apple. **We reroute it through the relay.**
- **Web-push path** = `app/webpush.py` + `PushSubscription`/`push_subscriptions`
  table + `static/sw.js`. **We do not touch it.** Different table, different code,
  different transport — PWA notifications keep working exactly as before.

Since `notify_user()` still calls both `push.schedule()` and
`webpush.schedule()`, and we keep `push.schedule()`'s signature identical, the
seam doesn't change. Only what `push.schedule` does *internally* changes.

---

## Backend change 1 — `app/push.py` (reroute to the relay)

Replace the file's body. The public surface (`push_enabled()`, `schedule()`)
stays the same, so `notify.py` needs no changes. The `.p8` key, `PyJWT`, and
Apple-host logic are gone — the relay does the signing now.

```python
"""Native iOS push via APNs, delivered through the self-hosted push-relay
(one shared signing key + central metrics for all our apps). Implementation
behind the single `notify.notify_user()` dispatch seam.

No-op until PUSH_RELAY_* settings are configured. Fire-and-forget off the
request path; tokens the relay reports dead are pruned.
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

# Reasons the relay returns (passed through from Apple) that mean "drop it".
_DEAD = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def push_enabled() -> bool:
    return bool(
        settings.push_relay_url
        and settings.push_relay_api_key
        and settings.apns_topic            # bundle id the relay routes on
    )


async def _deliver(user_id, title, body, data, badge) -> None:
    if not push_enabled():
        return
    async with SessionLocal() as db:
        tokens = (
            await db.scalars(select(DeviceToken).where(DeviceToken.user_id == user_id))
        ).all()
        if not tokens:
            return
        url = settings.push_relay_url.rstrip("/") + "/notify"
        headers = {"X-API-Key": settings.push_relay_api_key}
        custom = {k: v for k, v in (data or {}).items() if v is not None} or None
        dead: list[str] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for dt in tokens:
                payload = {
                    "bundle_id": settings.apns_topic,
                    "device_token": dt.token,
                    "title": title,
                    "body": body,
                    "badge": badge,
                    "sandbox": dt.environment == "sandbox",  # per-token environment
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
                    log.warning("relay %s (%s) for token %s…",
                                resp.status_code, reason, dt.token[:8])
        if dead:
            await db.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
            await db.commit()


async def _safe_deliver(user_id, title, body, data, badge) -> None:
    try:
        await _deliver(user_id, title, body, data, badge)
    except Exception:
        log.exception("push delivery failed for user %s", user_id)


def schedule(user_id: uuid.UUID, title: str, body: str,
             data: dict | None = None, badge: int = 0) -> None:
    """Fire-and-forget an APNs push (via the relay) to all of a user's devices.
    No-op unless the relay is configured; never blocks or raises into the caller."""
    if not push_enabled():
        return
    asyncio.create_task(_safe_deliver(user_id, title, body, data, badge))
```

---

## Backend change 2 — `app/config.py` (settings)

Add the relay settings. Keep `apns_topic` (now used as the bundle id the relay
routes on). The `apns_key*`, `apns_key_id`, `apns_team_id`, `apns_sandbox`
settings are no longer used by the relay path and can be removed later.

```python
# Native iOS push is now delivered via the push-relay.
push_relay_url: str = ""       # e.g. http://192.168.1.42:8088  (or https://push.<domain>)
push_relay_api_key: str = ""   # the relay's API_KEY_COLLOQUI — secret, env only
apns_topic: str = "com.jworthington.colloqui"   # bundle id the relay routes on
```

Set the secrets in the backend's environment (Portainer stack vars / `.env`),
never in code:

```
PUSH_RELAY_URL=http://192.168.1.42:8088
PUSH_RELAY_API_KEY=<the relay's API_KEY_COLLOQUI value>
APNS_TOPIC=com.jworthington.colloqui
```

---

## Backend change 3 — per-token environment (sandbox vs production)

A debug build run from Xcode produces a **sandbox** token; TestFlight/App Store
produce **production** tokens. They are not interchangeable, so we store which
each token is and let the relay route per-token.

`app/models.py` — add one column to `DeviceToken`:
```python
environment: Mapped[str] = mapped_column(String(16), default="production")  # "sandbox" | "production"
```

Alembic migration:
```python
def upgrade():
    op.add_column("device_tokens",
        sa.Column("environment", sa.String(16), nullable=False, server_default="production"))

def downgrade():
    op.drop_column("device_tokens", "environment")
```

`app/routes/devices.py` — accept and store `environment` on register (default
`"production"` so existing callers keep working).

---

## The new iOS app (from scratch)

### Xcode setup
- **Bundle identifier:** `com.jworthington.colloqui` (must match the relay + `APNS_TOPIC`).
- Add the **Push Notifications** capability (and **Background Modes → Remote
  notifications** only if you need silent pushes). The App ID already has APNs
  enabled under Team `9V4688726K`.

### Register + report environment
```swift
import UserNotifications
import UIKit

func registerForPush() {
    UNUserNotificationCenter.current()
        .requestAuthorization(options: [.alert, .badge, .sound]) { granted, _ in
            guard granted else { return }
            DispatchQueue.main.async { UIApplication.shared.registerForRemoteNotifications() }
        }
}

func application(_ app: UIApplication,
                 didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    let token = deviceToken.map { String(format: "%02x", $0) }.joined()
    #if DEBUG
    let environment = "sandbox"      // build run from Xcode
    #else
    let environment = "production"   // TestFlight / App Store
    #endif
    // Authenticated POST to your existing endpoint, e.g.:
    //   POST /devices  { "token": token, "platform": "ios", "environment": environment }
    Task { await ColloquiAPI.registerDevice(token: token, environment: environment) }
}
```

The app posts to Colloqui's **existing** `register_device` route — it never
talks to the relay directly, and never holds the relay API key.

---

## Why the PWA push is safe

- Web push lives entirely in `app/webpush.py` + the `push_subscriptions` table +
  `static/sw.js`. None of those are touched here.
- The only shared code, `notify.notify_user()`, still calls
  `webpush.schedule(...)` exactly as before — we only change what
  `push.schedule(...)` does internally.
- Different table (`device_tokens` vs `push_subscriptions`), different transport
  (relay/APNs vs VAPID/browser), different failure handling. They cannot
  interfere.

---

## Environments cheat-sheet

| Build | Token type | Stored `environment` | Relay sends `sandbox` |
|-------|-----------|----------------------|-----------------------|
| Run from Xcode (debug) | sandbox | `"sandbox"` | `true` |
| TestFlight / App Store | production | `"production"` | `false` |

---

## Day-one test loop (phone plugged into the Mac)

1. Set `PUSH_RELAY_URL` + `PUSH_RELAY_API_KEY` + `APNS_TOPIC` in the backend env; redeploy.
2. Run the **debug** iOS build onto the plugged-in phone → it registers a token
   with `environment="sandbox"`.
3. Trigger any notification (a DM, mention, or reminder) → `notify_user()` →
   `push.schedule()` → relay → Apple **sandbox** → 📱.
4. Confirm on the relay's Grafana dashboard — "Total notifications sent" ticks up
   and Colloqui appears under "Notifications per app."
5. The PWA keeps getting web-push the whole time, unchanged.

Manual one-off check from your Mac:
```bash
curl -X POST http://192.168.1.42:8088/notify \
  -H "X-API-Key: $PUSH_RELAY_API_KEY" -H "Content-Type: application/json" \
  -d '{"bundle_id":"com.jworthington.colloqui","device_token":"<hex>","title":"Test","body":"Hi","sandbox":true}'
```

---

## Checklist

- [ ] `app/push.py` rerouted to the relay (signature unchanged)
- [ ] `push_relay_url` / `push_relay_api_key` / `apns_topic` in config + env
- [ ] `environment` column added to `DeviceToken` (+ migration)
- [ ] `register_device` stores `environment`
- [ ] **`webpush.py` / `notify.py` / `push_subscriptions` left untouched**
- [ ] New iOS app: bundle id `com.jworthington.colloqui`, Push capability, registers token + environment to `/devices`
- [ ] Relay reachable from the backend (LAN IP, or public hostname via Cloudflare Tunnel if off-network)
