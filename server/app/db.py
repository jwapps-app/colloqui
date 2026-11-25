import inspect
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from .config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,   # drop dead connections instead of erroring a request
    pool_recycle=3600,    # recycle hourly so an idle timeout can't strand a conn
    # Explicit pool sizing: a channel-wide send fans out one notify per member
    # plus per-delivery push sessions, which the 5+10 default could exhaust
    # under a burst and then block on pool_timeout.
    pool_size=10,
    max_overflow=20,
    # Per-statement timeout (asyncpg): a stuck or runaway query can't hold a
    # connection indefinitely. Generous enough for any legitimate query here.
    connect_args={"command_timeout": 30},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ---- after-commit work -------------------------------------------------------
# Anything that tells the outside world about a database change (a WebSocket
# event, an outgoing webhook, a push, deleting a blob whose row is gone) must
# happen only once that change is durably saved. Otherwise a client told to
# refetch reads the old state, and a failed commit leaves everyone informed of
# something that never happened. Code never does such work inline: it queues it
# here, and the queue runs when the transaction commits and is dropped if it
# rolls back. The helpers in events.py are the usual way in.

_AFTER_COMMIT = "after_commit"
_log = logging.getLogger("colloqui.db")


def after_commit(db: AsyncSession, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run `fn(*args, **kwargs)` (sync or async) after `db`'s current
    transaction commits; never, if it rolls back."""
    db.info.setdefault(_AFTER_COMMIT, []).append((fn, args, kwargs))


async def run_after_commit(db: AsyncSession) -> None:
    """Run and clear everything queued. Call only right after a commit. A
    failing item is logged, not raised: the data is saved, so a side effect
    that could not be delivered must not turn the request into an error."""
    queued = db.info.pop(_AFTER_COMMIT, [])
    for fn, args, kwargs in queued:
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.exception("after-commit step %r failed", getattr(fn, "__name__", fn))


async def commit(db: AsyncSession) -> None:
    """Commit now and run the queued after-commit work. Use this, never a bare
    `db.commit()`, when a handler has to commit before it returns."""
    await db.commit()
    await run_after_commit(db)


@sa_event.listens_for(Session, "after_rollback")
def _discard_after_commit(session: Session) -> None:
    session.info.pop(_AFTER_COMMIT, None)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            # The rollback hook above only fires if a transaction was open;
            # a request that failed before touching the database must not
            # leave anything queued either.
            session.info.pop(_AFTER_COMMIT, None)
            raise
        await run_after_commit(session)


# How every handler gets its session:  `db: AsyncSession = DB`
#
# scope="function" makes get_db finish (commit, then the after-commit work) as
# soon as the handler returns, BEFORE the response is sent. With the default
# scope the commit ran after the response had left, so a client acting on that
# response at once (create a user then sign in as them, create a channel then
# reload the list) could not see its own change yet, and a failed commit was a
# 200 for something that was never saved. Always use this object, never a bare
# Depends(get_db): one shared definition is also what makes a handler and its
# dependencies (the signed-in user) share one session.
DB = Depends(get_db, scope="function")
