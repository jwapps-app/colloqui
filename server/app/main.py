import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401 — registers tables on Base.metadata
from . import ws
from .config import settings
from .db import engine
from .notify import reminder_loop
from .routes import (
    admin,
    auth,
    calsync,
    channels,
    devices,
    files,
    messages,
    notifications,
    reminders,
    spaces,
    users,
    webhooks,
    webpush,
)

# Schema is managed by Alembic (`alembic upgrade head` runs at container start,
# see the Dockerfile). The lifespan only does app-level data seeding.


async def migrate_existing_to_default_space() -> None:
    """One-time: if channels exist but no space does, create a default 'Main'
    space, enroll all users, and move existing channels into it. Idempotent —
    skips once any space exists."""
    from sqlalchemy import select, update

    from .db import SessionLocal
    from .models import Channel, Space, SpaceMember, User

    async with SessionLocal() as db:
        if await db.scalar(select(Space.id).limit(1)) is not None:
            return
        users = (await db.scalars(select(User).order_by(User.created_at))).all()
        if not users:
            return  # fresh server; first registration creates the space
        space = Space(name="Main", is_default=True, created_by=users[0].id)
        db.add(space)
        await db.flush()
        for u in users:
            db.add(
                SpaceMember(
                    space_id=space.id,
                    user_id=u.id,
                    role="manager" if u.is_admin else "member",
                )
            )
        await db.execute(
            update(Channel)
            .where(Channel.is_dm == False, Channel.space_id.is_(None))  # noqa: E712
            .values(space_id=space.id)
        )
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema migrations have already run (Alembic, at container start).
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    # The image runs as a non-root user; a host bind mount that Docker created
    # as root is unwritable. Say so loudly at startup instead of failing the
    # first upload (and the VAPID key write) with an opaque 500.
    if not os.access(settings.upload_dir, os.W_OK):
        logging.getLogger("colloqui").error(
            "UPLOAD_DIR %s is not writable by the service user (uid %s): uploads, "
            "avatars and the auto-generated VAPID key will fail. Fix the host "
            "directory's ownership/permissions to match the container user.",
            settings.upload_dir, os.getuid(),
        )
    # Resolve VAPID keys (env, else auto-generate + persist) so web push works
    # out of the box with zero config.
    from . import webpush as _webpush

    _webpush.ensure_keys()
    await migrate_existing_to_default_space()
    scheduler = asyncio.create_task(reminder_loop())
    yield
    scheduler.cancel()
    with suppress(asyncio.CancelledError):
        await scheduler
    await engine.dispose()


docs_kwargs = (
    {} if settings.dev_mode else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(title="api", lifespan=lifespan, **docs_kwargs)
# Compress text responses (app.js ~150KB, style.css, JSON). Skips already-small
# bodies and non-compressible binaries (images) automatically.
app.add_middleware(GZipMiddleware, minimum_size=600)

_ws_origin = settings.origin.replace("https://", "wss://").replace("http://", "ws://")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; "
    "frame-src blob:; object-src 'none'; "
    # PDF.js runs its parser in a same-origin web worker (blob: covers the
    # fallback path where it wraps the worker in a blob URL).
    "worker-src 'self' blob:; "
    f"connect-src 'self' {_ws_origin}; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


# Request-body caps, enforced on the bytes ACTUALLY RECEIVED (not just a
# declared Content-Length, which a chunked or mislabelled request can omit).
# JSON-ish bodies are tiny (a message, a login) — 256KB is generous. Anything
# else (multipart uploads) is bounded by the file limit plus form overhead, so
# an unauthenticated caller can't stream unbounded data into the parser before
# the upload handler's own checks run.
_MAX_JSON_BODY = 256 * 1024
_MAX_OTHER_BODY = (settings.max_file_size_mb + 1) * 1024 * 1024


class BodyLimitMiddleware:
    """Pure-ASGI so it can wrap `receive` and count body bytes as they arrive."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        ctype = headers.get(b"content-type", b"").decode("latin-1").lower()
        # Any JSON flavour (application/json, application/problem+json, …) gets
        # the small cap; multipart/other bodies get the upload cap.
        limit = _MAX_OTHER_BODY if ctype.startswith("multipart/") else _MAX_JSON_BODY
        clen = headers.get(b"content-length", b"").decode("latin-1")
        if clen.isdigit() and int(clen) > limit:
            from starlette.responses import JSONResponse

            resp = JSONResponse({"detail": "Request body too large"}, status_code=413)
            return await resp(scope, receive, send)

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the app's own body read, so FastAPI's
                    # exception handling turns it into a clean 413.
                    from fastapi import HTTPException

                    raise HTTPException(413, "Request body too large")
            return message

        return await self.app(scope, limited_receive, send)


app.add_middleware(BodyLimitMiddleware)


# Only genuine static assets get the year-long immutable cache; a `?v=` on any
# other URL (e.g. the private calendar feed) must not turn it public/immutable.
_STATIC_ASSET = re.compile(r"\.(js|mjs|css|png|ico|svg|json|woff2?)$")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    # Force HTTPS for a year (ignored over plain http, so safe for LAN/dev).
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000"
    )
    # Disable powerful features the app never uses.
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    )
    # Isolate our browsing context from any opener/popups.
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/calendar/") or path.startswith("/hooks/"):
        # API responses and the private calendar feed must never be cached by
        # a shared cache or survive in a browser after sign-out.
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.query_params.get("v") and _STATIC_ASSET.search(path):
        # Version-stamped assets (app.js?v=N, icons?v=N) never change under a
        # given URL, so cache them for a year and skip the revalidation round
        # trip. A new release bumps ?v=, which is a fresh URL, so updates land.
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    else:
        # index.html, sw.js, manifest.json: always revalidate (cheap 304 via
        # ETag) so a new release's ?v= bump is picked up on the next reload.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": settings.app_version}


for router in (
    auth.router,
    users.router,
    calsync.router,
    spaces.router,
    channels.router,
    messages.router,
    files.router,
    reminders.router,
    notifications.router,
    devices.router,
    webpush.router,
    admin.router,
    webhooks.router,
    webhooks.public_router,
    ws.router,
):
    app.include_router(router)

# Built-in web client (also serves as the dev/reference client for the API).
app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static"
)
