"""Telling the outside world that something changed.

Handlers never call the socket manager or the webhook dispatcher directly.
They call these, which queue the delivery on the session so it happens only
after the change is committed (see db.after_commit). Purely transient signals
that describe no stored state (typing, presence, pings) are the one exception
and stay in ws.py.
"""
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import webhooks_out
from .db import after_commit
from .ws import manager


async def _send(user_ids: list[uuid.UUID], payload: Any) -> None:
    await manager.send_to_users(user_ids, payload)


def publish(db: AsyncSession, user_ids: Iterable[uuid.UUID], payload: dict) -> None:
    """Queue a WebSocket event for these users."""
    after_commit(db, _send, list(user_ids), payload)


def emit(db: AsyncSession, event_type: str, data: dict) -> None:
    """Queue an integration event (outgoing webhooks, see INTEGRATION.md)."""
    after_commit(db, webhooks_out.dispatch_event, event_type, data)


def disconnect_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Close every socket the user holds, once their lockout is saved."""
    after_commit(db, manager.disconnect_user, user_id)


def disconnect_user_except(db: AsyncSession, user_id: uuid.UUID, keep_hash: str) -> None:
    """Close the user's sockets except those on `keep_hash` (the session that
    is making a credential change stays connected)."""
    after_commit(db, manager.disconnect_user_except, user_id, keep_hash)


def disconnect_token(db: AsyncSession, token_hash: str) -> None:
    """Close the sockets opened with a session or API key, once it is revoked."""
    after_commit(db, manager.disconnect_token, token_hash)
