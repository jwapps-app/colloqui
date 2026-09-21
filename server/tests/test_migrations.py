"""Migrations must work on a FRESH database in both directions.

The normal suite builds its schema with create_all, so it can't catch a
migration that breaks a brand-new deployment (0001 creates the full current
schema from the live models, so later migrations must tolerate objects that
already exist) or a downgrade that assumes upgrade-path object names. This
runs the real alembic CLI against a dedicated empty database:
upgrade head -> downgrade base -> upgrade head.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import ADMIN_URL, _with_db

MIG_DB = "app_migtest"
SERVER_DIR = Path(__file__).resolve().parent.parent


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.asyncio
async def test_fresh_install_round_trips():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {MIG_DB}"))
    await admin.dispose()
    url = _with_db(ADMIN_URL, MIG_DB)
    try:
        for step in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
            r = _alembic(url, *step)
            assert r.returncode == 0, f"alembic {' '.join(step)} failed:\n{r.stderr[-3000:]}"
        cur = _alembic(url, "current")
        assert "(head)" in cur.stdout, cur.stdout + cur.stderr
    finally:
        admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)"))
        await admin.dispose()
