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

router = APIRouter()


class ConnectionManager:
    """Tracks live sockets per user with an active/away state; events fan out to
    channel members. A user's effective presence is `online` if any of their
    sockets is active, `away` if all are idle/backgrounded, else `offline`."""

    def __init__(self) -> None:
        # user_id -> {socket: 'active' | 'away'}
        self._conns: dict[uuid.UUID, dict[WebSocket, str]] = {}

    def add(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        self._conns.setdefault(user_id, {})[ws] = "active"

    def remove(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        conns = self._conns.get(user_id)
        if conns is None:
            return
        conns.pop(ws, None)
        if not conns:
            del self._conns[user_id]

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

    async def send_to_users(self, user_ids: Iterable[uuid.UUID], payload: Any) -> None:
        for user_id in set(user_ids):
            for ws in list(self._conns.get(user_id, {})):
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass  # dead socket; the disconnect handler cleans it up

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


@router.websocket("/api/v1/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # The session token arrives in the first message rather than the URL,
    # so it never lands in access logs.
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=4401)
        return

    async with SessionLocal() as db:
        user = await user_from_token(db, first.get("token") if isinstance(first, dict) else None)
        await db.commit()
    if user is None:
        await ws.close(code=4401)
        return

    before = manager.presence_of(user.id)
    manager.add(user.id, ws)
    await ws.send_json({"type": "ready", "presence": manager.presence_map()})
    after = manager.presence_of(user.id)
    if after != before:
        await manager.broadcast(
            {"type": "presence", "user_id": str(user.id), "state": after}
        )
    last_typing = 0.0
    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
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
                    if await db.get(ChannelMember, (channel_id, user.id)) is None:
                        continue
                    others = [
                        uid
                        for uid in (
                            await db.scalars(
                                select(ChannelMember.user_id).where(
                                    ChannelMember.channel_id == channel_id
                                )
                            )
                        ).all()
                        if uid != user.id
                    ]
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
