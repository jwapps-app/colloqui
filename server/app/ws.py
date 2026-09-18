import asyncio
import time
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .db import SessionLocal
from .deps import user_from_token
from .models import ChannelMember, User, utcnow
from .security import RateLimiter, hash_token

router = APIRouter()

# A real person has a handful of devices (phone, iPad, Mac, PWA); cap resident
# sockets per user so a leaky or stuck client can't grow the connection map
# (and the cost of every broadcast) without bound.
MAX_SOCKETS_PER_USER = 10
# Bound each WebSocket send. Fan-out is awaited inline on the message-send
# request path, so one backpressured client must not stall the sender.
SEND_TIMEOUT = 5.0
# How often a long-lived socket re-validates its token (catches expiry).
REVALIDATE_SECONDS = 300

_bg: set[asyncio.Task] = set()


def _fire(coro) -> None:
    """Fire-and-forget with a strong ref (the loop holds tasks only weakly)."""
    async def _safe():
        try:
            await coro
        except Exception:
            pass
    task = asyncio.create_task(_safe())
    _bg.add(task)
    task.add_done_callback(_bg.discard)


class ConnectionManager:
    """Tracks live sockets per user with an active/away state; events fan out to
    channel members. A user's effective presence is `online` if any of their
    sockets is active, `away` if all are idle/backgrounded, else `offline`."""

    def __init__(self) -> None:
        # user_id -> {socket: 'active' | 'away'}
        self._conns: dict[uuid.UUID, dict[WebSocket, str]] = {}
        # socket -> hash of the session/API-key token it authenticated with, so
        # revoking that credential can close exactly the sockets it opened.
        self._token_of: dict[WebSocket, str] = {}

    def add(self, user_id: uuid.UUID, ws: WebSocket, token_hash: str = "") -> None:
        conns = self._conns.setdefault(user_id, {})
        # Over the per-user cap, evict the OLDEST socket (dict order = insertion
        # order) and close it; its own disconnect handler then no-ops on remove.
        while len(conns) >= MAX_SOCKETS_PER_USER:
            oldest = next(iter(conns))
            conns.pop(oldest, None)
            self._token_of.pop(oldest, None)
            _fire(oldest.close(code=4409))
        conns[ws] = "active"
        self._token_of[ws] = token_hash

    def remove(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        self._token_of.pop(ws, None)
        conns = self._conns.get(user_id)
        if conns is None:
            return
        conns.pop(ws, None)
        if not conns:
            del self._conns[user_id]

    async def disconnect_token(self, token_hash: str) -> None:
        """Close every socket that authenticated with this (now revoked) token."""
        for user_id, conns in list(self._conns.items()):
            for ws in list(conns):
                if self._token_of.get(ws) == token_hash:
                    self.remove(user_id, ws)
                    _fire(ws.close(code=4401))

    async def disconnect_user_except(self, user_id: uuid.UUID, keep_hash: str) -> None:
        """Close all of a user's sockets except those on `keep_hash` (the
        session making a credential change stays connected)."""
        for ws in list(self._conns.get(user_id, {})):
            if self._token_of.get(ws) != keep_hash:
                self.remove(user_id, ws)
                _fire(ws.close(code=4401))

    def set_state(self, user_id: uuid.UUID, ws: WebSocket, state: str) -> None:
        conns = self._conns.get(user_id)
        if conns is not None and ws in conns:
            conns[ws] = "away" if state == "away" else "active"

    def presence_of(self, user_id: uuid.UUID) -> str:
        conns = self._conns.get(user_id)
        if not conns:
            return "offline"
        return "online" if any(s == "active" for s in conns.values()) else "away"

    def presence_map(self) -> dict[str, str]:
        """Snapshot of everyone currently connected, as id -> online|away."""
        return {str(uid): self.presence_of(uid) for uid in self._conns}

    async def _send_one(self, user_id: uuid.UUID, ws: WebSocket, payload: Any) -> None:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            # A stalled/backpressured socket: drop it so it can't hold up any
            # fan-out again. The client reconnects on its own.
            self.remove(user_id, ws)
            _fire(ws.close(code=4408))
        except Exception:
            pass  # dead socket; the disconnect handler cleans it up

    async def send_to_users(self, user_ids: Iterable[uuid.UUID], payload: Any) -> None:
        # All sockets concurrently, each individually bounded: a delivery pass
        # costs at most one SEND_TIMEOUT, not one per stalled socket in series.
        sends = [
            self._send_one(user_id, ws, payload)
            for user_id in set(user_ids)
            for ws in list(self._conns.get(user_id, {}))
        ]
        if sends:
            await asyncio.gather(*sends, return_exceptions=True)

    def is_online(self, user_id: uuid.UUID) -> bool:
        return user_id in self._conns

    def online_user_ids(self) -> list[uuid.UUID]:
        return list(self._conns.keys())

    async def broadcast(self, payload: Any) -> None:
        await self.send_to_users(self.online_user_ids(), payload)

    async def disconnect_user(self, user_id: uuid.UUID) -> None:
        """Force-close every socket a user holds (e.g. account disabled)."""
        for ws in list(self._conns.get(user_id, {})):
            try:
                await ws.close(code=4403)
            except Exception:
                pass


manager = ConnectionManager()


# Cap unauthenticated connection attempts per client IP so an attacker can't
# flood sockets (each held open through the auth grace period). Generous enough
# for a real client's reconnect storms.
_ws_connect_limiter = RateLimiter(limit=60, window_seconds=60)


def _ws_client_ip(ws: WebSocket) -> str:
    cf = ws.headers.get("cf-connecting-ip")
    if cf:
        return cf.split(",")[0].strip()
    return ws.client.host if ws.client else "unknown"


@router.websocket("/api/v1/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # The session token arrives in the first message rather than the URL,
    # so it never lands in access logs.
    await ws.accept()
    if not _ws_connect_limiter.allow(_ws_client_ip(ws)):
        await ws.close(code=4429)  # too many connection attempts
        return
    try:
        # Short auth grace: a real client sends its token immediately. Keeping it
        # brief bounds how long an unauthenticated socket can squat a connection.
        first = await asyncio.wait_for(ws.receive_json(), timeout=3)
    except Exception:
        await ws.close(code=4401)
        return

    token = first.get("token") if isinstance(first, dict) else None
    async with SessionLocal() as db:
        user = await user_from_token(db, token)
        await db.commit()
    if user is None:
        await ws.close(code=4401)
        return

    before = manager.presence_of(user.id)
    manager.add(user.id, ws, hash_token(token))
    await ws.send_json({"type": "ready", "presence": manager.presence_map()})
    after = manager.presence_of(user.id)
    if after != before:
        await manager.broadcast(
            {"type": "presence", "user_id": str(user.id), "state": after}
        )
    last_typing = 0.0
    last_check = time.monotonic()
    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
            # A socket authenticates once at connect. Revocations close it
            # explicitly (see disconnect_token); expiry and anything missed is
            # caught by re-validating the token periodically on inbound traffic
            # (clients ping regularly, so this fires on a live connection).
            if time.monotonic() - last_check > REVALIDATE_SECONDS:
                last_check = time.monotonic()
                async with SessionLocal() as db:
                    still = await user_from_token(db, token)
                    await db.commit()
                if still is None:
                    await ws.close(code=4401)
                    return
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif data.get("type") == "presence":
                # Client reports this socket as active or away (tab focus/idle).
                before = manager.presence_of(user.id)
                manager.set_state(user.id, ws, str(data.get("state")))
                after = manager.presence_of(user.id)
                if after != before:
                    await manager.broadcast(
                        {"type": "presence", "user_id": str(user.id), "state": after}
                    )
            elif data.get("type") == "typing":
                now = time.monotonic()
                if now - last_typing < 1.0:  # throttle typing fan-out
                    continue
                last_typing = now
                try:
                    channel_id = uuid.UUID(str(data.get("channel_id")))
                except (ValueError, TypeError):
                    continue
                async with SessionLocal() as db:
                    members = (
                        await db.scalars(
                            select(ChannelMember.user_id).where(
                                ChannelMember.channel_id == channel_id
                            )
                        )
                    ).all()
                # One query: derive membership from the member list instead of a
                # separate db.get, then everyone but the typist gets the ping.
                if user.id not in members:
                    continue
                others = [uid for uid in members if uid != user.id]
                await manager.send_to_users(
                    others,
                    {
                        "type": "typing",
                        "channel_id": str(channel_id),
                        "user": {"id": str(user.id), "display_name": user.display_name},
                    },
                )
    except WebSocketDisconnect:
        pass
    finally:
        before = manager.presence_of(user.id)
        manager.remove(user.id, ws)
        after = manager.presence_of(user.id)
        if after != before:
            if after == "offline":
                # Their last socket dropped: stamp "last seen" for the UI.
                async with SessionLocal() as db:
                    u = await db.get(User, user.id)
                    if u is not None:
                        u.last_seen_at = utcnow()
                        await db.commit()
            await manager.broadcast(
                {"type": "presence", "user_id": str(user.id), "state": after}
            )
