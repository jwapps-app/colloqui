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


@pytest.mark.asyncio
async def test_0025_upgrades_an_old_database_without_failing():
    """0025 on a database shaped like a long-lived deployment: created before
    spaces existed, without the value checks, and holding one value the checks
    would reject. It must backfill the default space, add every constraint,
    leave the odd row alone (reporting it rather than failing the deploy), and
    be safe to run again."""
    import uuid

    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {MIG_DB}"))
    await admin.dispose()
    url = _with_db(ADMIN_URL, MIG_DB)
    checks = ["ck_space_members_role", "ck_channel_members_role",
              "ck_channel_notify_prefs_level", "ck_reminders_recurrence"]
    boss, worker, chan, dm = (uuid.uuid4() for _ in range(4))
    try:
        r = _alembic(url, "upgrade", "0024")
        assert r.returncode == 0, r.stderr[-3000:]

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            # A fresh 0001 builds today's schema; put back what 0024 really had.
            for name, table in zip(checks, ["space_members", "channel_members",
                                            "channel_notify_prefs", "reminders"]):
                await conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
            await conn.execute(text("ALTER TABLE spaces ALTER COLUMN created_by SET NOT NULL"))
            for uid, name, is_admin, age in ((boss, "boss", True, 2), (worker, "worker", False, 1)):
                await conn.execute(text(
                    "INSERT INTO users (id, username, display_name, is_admin, disabled, "
                    "created_at, badge_channel_messages) VALUES "
                    f"(:id, :n, :n, :a, false, now() - interval '{age} days', true)"),
                    {"id": uid, "n": name, "a": is_admin})
            for cid, name, is_dm in ((chan, "general", False), (dm, None, True)):
                await conn.execute(text(
                    "INSERT INTO channels (id, name, is_private, is_dm, position, created_by, "
                    "created_at) VALUES (:id, :n, false, :dm, 0, :u, now())"),
                    {"id": cid, "n": name, "dm": is_dm, "u": boss})
            await conn.execute(text(
                "INSERT INTO channel_members (channel_id, user_id, role, joined_at) "
                "VALUES (:c, :u, 'legacy-role', now())"), {"c": chan, "u": boss})
        await engine.dispose()

        r = _alembic(url, "upgrade", "head")
        assert r.returncode == 0, r.stderr[-3000:]
        assert "ck_channel_members_role" in r.stderr and "legacy-role" not in r.stderr

        async def state(conn):
            spaces = (await conn.execute(text(
                "SELECT id, name, is_default, created_by FROM spaces"))).fetchall()
            members = dict((await conn.execute(text(
                "SELECT user_id, role FROM space_members"))).fetchall())
            placed = dict((await conn.execute(text(
                "SELECT id, space_id FROM channels"))).fetchall())
            valid = dict((await conn.execute(text(
                "SELECT conname, convalidated FROM pg_constraint WHERE contype = 'c'"))).fetchall())
            nullable = (await conn.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'spaces' AND column_name = 'created_by'"))).scalar()
            return spaces, members, placed, valid, nullable

        engine = create_async_engine(url)
        async with engine.connect() as conn:
            spaces, members, placed, valid, nullable = await state(conn)
        assert len(spaces) == 1
        space_id, name, is_default, creator = spaces[0]
        assert (name, is_default, creator) == ("Main", True, boss)  # the oldest account
        assert members == {boss: "manager", worker: "member"}
        assert placed == {chan: space_id, dm: None}                 # DMs have no space
        assert nullable == "YES"
        # Every check exists; only the one with an offending old row is unvalidated.
        assert {n: valid[n] for n in checks} == {
            "ck_space_members_role": True, "ck_channel_members_role": False,
            "ck_channel_notify_prefs_level": True, "ck_reminders_recurrence": True}
        # ...and even that one already refuses new bad values.
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO channel_members (channel_id, user_id, role, joined_at) "
                    "VALUES (:c, :u, 'nonsense', now())"), {"c": chan, "u": worker})
        await engine.dispose()

        # Running it again changes nothing (no second "Main", no error).
        for step in (("downgrade", "0024"), ("upgrade", "head")):
            r = _alembic(url, *step)
            assert r.returncode == 0, r.stderr[-3000:]
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            again = await state(conn)
        await engine.dispose()
        assert (again[0], again[1], again[2]) == (spaces, members, placed)
    finally:
        admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {MIG_DB} WITH (FORCE)"))
        await admin.dispose()
