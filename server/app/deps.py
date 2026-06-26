from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import API_KEY_PREFIX, ApiKey
from .models import Session as AuthSession
from .models import User
from .security import RateLimiter, hash_token

auth_limiter = RateLimiter(limit=30, window_seconds=60)


def client_ip(request: Request) -> str:
    # Behind a Cloudflare Tunnel the socket peer is always the tunnel, so the
    # real client IP comes from CF-Connecting-IP (Cloudflare sets it and strips
    # any client-supplied copy; the origin isn't reachable except via the
    # tunnel). Falls back to the socket peer for direct/LAN/dev access.
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_auth(request: Request) -> None:
    if not auth_limiter.allow(client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")


async def user_from_token(db: AsyncSession, token: str | None) -> User | None:
    if not token:
        return None
    now = datetime.now(timezone.utc)
    # API keys (machine-to-machine) act as their bound user. Long-lived, but
    # revocable; only the hash is stored, same as session tokens.
    if token.startswith(API_KEY_PREFIX):
        key = await db.scalar(
            select(ApiKey).where(ApiKey.token_hash == hash_token(token))
        )
        if key is None or key.revoked_at is not None:
            return None
        user = await db.get(User, key.user_id)
        if user is None or user.disabled:
            return None
        key.last_used_at = now
        return user
    session = await db.scalar(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )
    if session is None or session.revoked or session.expires_at < now:
        return None
    user = await db.get(User, session.user_id)
    if user is None or user.disabled:
        return None
    session.last_seen_at = now
    return user


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
    user = await user_from_token(db, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
