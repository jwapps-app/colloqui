import base64
import hashlib
import secrets
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except Argon2Error:
        return False


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_invite_code() -> str:
    return secrets.token_urlsafe(12)


def hash_token(token: str) -> str:
    # Tokens/codes are high-entropy random values, so a fast hash is fine
    # (slow hashes like Argon2 only matter for low-entropy human passwords).
    return hashlib.sha256(token.encode()).hexdigest()


class ExpiringStore:
    """In-memory store for short-lived WebAuthn challenges.

    Single-process only — fine while the API runs as one uvicorn process.
    Move to the database or Redis if the API is ever scaled out.
    """

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._items: dict[str, tuple[float, Any]] = {}

    def put(self, value: Any) -> str:
        self._prune()
        key = new_token()
        self._items[key] = (time.monotonic() + self.ttl, value)
        return key

    def pop(self, key: str) -> Any | None:
        item = self._items.pop(key, None)
        if item is None:
            return None
        expires, value = item
        if time.monotonic() > expires:
            return None
        return value

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._items.items() if exp < now]:
            del self._items[key]


class RateLimiter:
    """Sliding-window in-memory rate limiter. Single-process only."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        allowed = len(hits) < self.limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits
        return allowed
