from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
