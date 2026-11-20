from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str, **kw) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_async_engine(url, pool_size=kw.pop("pool_size", 10), max_overflow=kw.pop("max_overflow", 5),
                                  pool_pre_ping=True, **kw)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("database not initialised")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session: commits on success, rolls back on any exception."""
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
