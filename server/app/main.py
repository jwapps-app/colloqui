import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
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
)

# Schema is managed by Alembic (`alembic upgrade head` runs at container start,
# see the Dockerfile). The lifespan only does app-level data seeding.


async def migrate_existing_to_default_space() -> None:
    """One-time: if channels exist but no space does, create a default 'Main'
    space, enroll all users, and move existing channels into it. Idempotent —
    skips once any space exists."""
    from sqlalchemy import func, select, update

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

_ws_origin = settings.origin.replace("https://", "wss://").replace("http://", "ws://")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; "
    "frame-src blob:; object-src 'none'; "
    f"connect-src 'self' {_ws_origin}; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    else:
        # Static files: always revalidate (cheap 304s via ETag) so clients
        # pick up new app.js/css on plain reload instead of serving stale UI.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


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
