"""Native iOS push via APNs, delivered through the self-hosted push-relay
(one shared signing key + central metrics for all our apps). This is the
implementation behind the single `notify.notify_user()` dispatch seam.

Push is a silent no-op until the PUSH_RELAY_* settings are configured, so the
server runs fine without them. Sends are fire-and-forget off the request path.

Relay contract (POST {relay}/notify, X-API-Key):
  200  {"status": "sent", ...}             delivered to Apple
  410  {"detail": R, "reason": R}          the token is fatally bad; R is one of
                                           BadDeviceToken / Unregistered /
                                           DeviceTokenNotForTopic
  413  payload over Apple's 4 KB limit     (we truncate first, so never expected)
  422  validation error; `detail` is a LIST that echoes request values
  429  rate limited (may carry Retry-After)
  502  any other upstream failure, deliberately generic

A token is pruned ONLY on a 410 whose `reason` is exactly one of the three
strings above. Nothing else is ever searched for those words: a 422 echoes the
message text back, so a chat message that merely says "Unregistered" must not
be able to delete a live token. BadDeviceToken is also what Apple answers when
the sandbox flag doesn't match the token's real environment, so that one gets a
single retry against the other environment before the token is given up on.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import delete, select, update

from .config import settings
from .db import SessionLocal
from .models import DeviceToken

log = logging.getLogger("push")

# The `reason` values on a relay 410 that mean the token is fatally bad.
_DEAD = frozenset({"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"})
# ...of which this one is ambiguous: Apple also answers it for a perfectly good
# token sent to the wrong environment (a TestFlight/App Store token recorded as
# sandbox, or a debug build recorded as production).
_WRONG_ENV = "BadDeviceToken"

# Alert text limits. Apple caps the whole APNs payload at 4096 bytes and the
# relay answers 413 over that — the notification would simply be lost. No lock
# screen shows more than a few lines, so cap the body by characters, then make
# sure title + body + custom data also fit a byte budget that leaves room for
# the relay's own `aps` envelope (sound, badge, mutable-content, key names).
_TITLE_MAX_CHARS = 100  # same clamp notify_user() applies to the inbox title
_BODY_MAX_CHARS = 300
_ALERT_MAX_BYTES = 3500
_ELLIPSIS = "…"
# Never leave a cut ending on whitespace or a zero-width joiner (the glue inside
# multi-part emoji) — a joiner with nothing after it renders as garbage.
_DANGLING = "\u200d \t\r\n"

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


def _wire_size(title: str, body: str, custom: dict | None) -> int:
    """Bytes the alert text + custom data cost in the APNs payload. The relay
    serializes with json.dumps' default ASCII escaping, where every non-ASCII
    character costs 6 bytes (12 for emoji and other astral characters) — always
    at least its UTF-8 size, so this bounds the raw UTF-8 size as well."""
    return len(json.dumps({"title": title, "body": body, "custom_data": custom or {}}))


def _clip(text: str, max_chars: int) -> str:
    """Cap `text` at max_chars characters, ellipsis included. Python strings
    index by code point, so a slice can never split a character's bytes; also
    drop a dangling joiner/space so the cut doesn't end on half an emoji
    sequence."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip(_DANGLING) + _ELLIPSIS


def _fit_alert(title: str, body: str, custom: dict | None) -> tuple[str, str]:
    """Shorten the alert text (never the custom data, never the stored message)
    so the push fits Apple's payload limit. Text already within the limits is
    returned untouched."""
    title = _clip(title, _TITLE_MAX_CHARS)
    body = _clip(body, _BODY_MAX_CHARS)
    if _wire_size(title, body, custom) <= _ALERT_MAX_BYTES:
        return title, body
    # Multi-byte text (emoji, CJK) can blow the byte budget well inside the
    # character cap: keep the longest prefix that fits, one whole character at
    # a time.
    room = _ALERT_MAX_BYTES - _wire_size(title, _ELLIPSIS, custom)
    keep = 0
    for ch in body:
        room -= len(json.dumps(ch)) - 2  # minus the surrounding quotes
        if room < 0:
            break
        keep += 1
    return title, body[:keep].rstrip(_DANGLING) + _ELLIPSIS


@dataclass(frozen=True)
class _Outcome:
    token: str
    sent: bool = False
    dead: bool = False
    # Set when a retry proved the stored environment wrong: (was, actually is).
    environment_fix: tuple[str, str] | None = None


def _fatal_reason(resp: httpx.Response) -> str | None:
    """The token-fatal reason the relay reported, or None. Only a 410 with an
    exact `reason` counts — never a substring of `detail`, which on a 422
    echoes the message text back at us."""
    if resp.status_code != 410:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) and reason in _DEAD else None


def _log_failure(resp: httpx.Response, token: str) -> None:
    if resp.status_code == 429:
        log.warning(
            "relay rate-limited us (429, Retry-After: %s) for token %s…",
            resp.headers.get("Retry-After", "not given"),
            token[:8],
        )
        return
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("detail"), str):
            # Strings only: a 422's list detail echoes the message text, which
            # has no business in the logs.
            detail = data["detail"][:200]
    except ValueError:
        pass
    log.warning("relay %s (%s) for token %s…", resp.status_code, detail, token[:8])


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
) -> _Outcome:
    """Send one push and report what happened to it."""
    payload: dict = {
        "bundle_id": settings.apns_topic,
        "device_token": token,
        "title": title,
        "body": body,
        "badge": badge,
        # Per-token environment: debug builds register sandbox tokens.
        "sandbox": environment == "sandbox",
        # Run the app's Notification Service Extension on delivery — it dedups
        # against locally scheduled fallback reminders (harmless otherwise: if
        # the extension is missing or times out, iOS shows the push as-is).
        "mutable_content": True,
    }
    if custom:
        payload["custom_data"] = custom
    try:
        resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        log.warning("relay send failed: %s", e)
        return _Outcome(token)
    if resp.status_code == 200:
        return _Outcome(token, sent=True)
    reason = _fatal_reason(resp)
    if reason is None:
        _log_failure(resp, token)
        return _Outcome(token)
    if reason != _WRONG_ENV:
        return _Outcome(token, dead=True)

    # BadDeviceToken: Apple refused it (nothing was delivered, so trying again
    # cannot double-notify). Try the other environment exactly once before
    # deleting what may be a perfectly good token.
    other = "production" if environment == "sandbox" else "sandbox"
    try:
        resp = await client.post(
            url, json={**payload, "sandbox": other == "sandbox"}, headers=headers
        )
    except httpx.HTTPError as e:
        log.warning("relay send failed on the %s retry: %s", other, e)
        return _Outcome(token)  # inconclusive — keep the token
    if resp.status_code == 200:
        log.info("token %s… is a %s token (was stored as %s)", token[:8], other, environment)
        return _Outcome(token, sent=True, environment_fix=(environment, other))
    if _fatal_reason(resp) is not None:
        return _Outcome(token, dead=True)  # bad in both environments
    _log_failure(resp, token)
    return _Outcome(token)  # inconclusive — keep the token


async def _deliver(
    user_id: uuid.UUID, title: str, body: str, data: dict | None, badge: int
) -> int:
    """Push to every device the user has; returns how many the relay accepted."""
    if not push_enabled():
        return 0
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
        return 0

    url = settings.push_relay_url.rstrip("/") + "/notify"
    headers = {"X-API-Key": settings.push_relay_api_key}
    # Custom keys for deep-linking (channel_id, root_id, message_id, …).
    custom = {k: v for k, v in (data or {}).items() if v is not None} or None
    # Only the alert text is shortened; the custom keys go out exactly as given.
    title, body = _fit_alert(title, body, custom)

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
    outcomes = [r for r in results if isinstance(r, _Outcome)]
    for r in results:
        if isinstance(r, BaseException):
            log.warning("push send raised: %r", r)
    dead = [o.token for o in outcomes if o.dead]
    fixes = [o for o in outcomes if o.environment_fix]
    if dead or fixes:
        async with SessionLocal() as db:
            if dead:
                await db.execute(delete(DeviceToken).where(DeviceToken.token.in_(dead)))
            for o in fixes:
                was, now = o.environment_fix
                # Only if it still says what we read: the app may have
                # re-registered with its own idea of the environment meanwhile.
                await db.execute(
                    update(DeviceToken)
                    .where(DeviceToken.token == o.token, DeviceToken.environment == was)
                    .values(environment=now)
                )
            await db.commit()
    return sum(o.sent for o in outcomes)


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
