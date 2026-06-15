"""Test harness: each test runs against a freshly-created throwaway database
so tests never touch real data. Auth is created directly (a user + session
token) since the WebAuthn ceremony can't run headless — the same shortcut the
manual probe testing used."""

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Base Postgres URL (the test DB name is swapped in per-session).
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://app:app@db:5432/app",
)
TEST_DB = "app_test"


def _with_db(url: str, dbname: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + dbname


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_test_db():
    # Drop/recreate a dedicated test database, then point the app at it.
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(
            text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        )
        await conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    await admin.dispose()
    os.environ["DATABASE_URL"] = _with_db(ADMIN_URL, TEST_DB)
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_upload_dir(tmp_path_factory):
    """Point uploads (and the auto-generated VAPID key file) at a throwaway dir
    so tests never write into the repo's server tree."""
    from app.config import settings

    settings.upload_dir = str(tmp_path_factory.mktemp("data"))
    yield


@pytest.fixture(autouse=True)
def _reset_webpush():
    """Keep VAPID key state from leaking between tests — startup ensure_keys()
    doesn't run under ASGITransport, so web push should stay off by default."""
    yield
    from app import webpush

    webpush.reset_cache()


@pytest_asyncio.fixture
async def app_module(_create_test_db):
    # Rebuild the engine with NullPool bound to *this* test's event loop, so
    # connections are never reused across loops (which pytest-asyncio creates
    # per test). Then create a clean schema.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import db as dbmod
    from app.config import settings
    from app.db import Base

    dbmod.engine = create_async_engine(settings.database_url, poolclass=NullPool)
    dbmod.SessionLocal = async_sessionmaker(dbmod.engine, expire_on_commit=False)

    async with dbmod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    from app.main import app

    yield app
    await dbmod.engine.dispose()


@pytest_asyncio.fixture
async def client(app_module):
    transport = ASGITransport(app=app_module)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def make_user(app_module):
    """Factory: create a user + session, return (token, user_id)."""
    import hashlib
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import Session as AuthSession
    from app.models import User, utcnow

    created = []

    async def _make(username, *, is_admin=False):
        token = "tok-" + uuid.uuid4().hex
        async with SessionLocal() as db:
            user = User(username=username, display_name=username, is_admin=is_admin)
            db.add(user)
            await db.flush()
            db.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()
            created.append(user.id)
            return token, user.id

    return _make


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
