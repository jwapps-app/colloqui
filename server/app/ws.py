import asyncio
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .db import SessionLocal
from .deps import user_from_token
from .models import ChannelMember

router = APIRouter()


class ConnectionManager:
    """Tracks live sockets per user; events fan out to channel members."""

    def __init__(self) -> None:
        self._sockets: dict[uuid.UUID, set[WebSocket]] = {}

    def add(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        self._sockets.setdefault(user_id, set()).add(ws)

    def remove(self, user_id: uuid.UUID, ws: WebSocket) -> None:
        conns = self._sockets.get(user_id)
        if conns is None:
            return
        conns.discard(ws)
        if not conns:
            del self._sockets[user_id]

    async def send_to_users(self, user_ids: Iterable[uuid.UUID], payload: Any) -> None:
        for user_id in set(user_ids):
            for ws in list(self._sockets.get(user_id, ())):
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass  # dead socket; the disconnect handler cleans it up

    def is_online(self, user_id: uuid.UUID) -> bool:
        return user_id in self._sockets

    def online_user_ids(self) -> list[uuid.UUID]:
        return list(self._sockets.keys())

    async def broadcast(self, payload: Any) -> None:
        await self.send_to_users(self.online_user_ids(), payload)

    async def disconnect_user(self, user_id: uuid.UUID) -> None:
        """Force-close every socket a user holds (e.g. account disabled)."""
        for ws in list(self._sockets.get(user_id, ())):
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

    came_online = not manager.is_online(user.id)
    manager.add(user.id, ws)
    await ws.send_json(
        {"type": "ready", "online": [str(u) for u in manager.online_user_ids()]}
    )
    if came_online:
        await manager.broadcast(
            {"type": "presence", "user_id": str(user.id), "online": True}
        )
    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
            elif data.get("type") == "typing":
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
        manager.remove(user.id, ws)
        if not manager.is_online(user.id):
            await manager.broadcast(
                {"type": "presence", "user_id": str(user.id), "online": False}
            )
