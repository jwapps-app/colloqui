import asyncio
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


# JSON bodies are tiny (a message, a login); nothing legitimate needs more than
# this. Caps the unauthenticated attack surface (e.g. /verify) against oversized
# or deeply-nested JSON. Multipart file uploads set their own content-type and
# are size-limited in the upload handler, so they're exempt.
_MAX_JSON_BODY = 256 * 1024


@app.middleware("http")
async def limit_json_body(request: Request, call_next):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        clen = request.headers.get("content-length")
        if clen is not None and clen.isdigit() and int(clen) > _MAX_JSON_BODY:
            from starlette.responses import JSONResponse

            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    return await call_next(request)


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
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.query_params.get("v"):
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
