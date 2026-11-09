from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from aegis.config import get_settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    return create_async_engine(url, **kwargs)


def init_engine(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _engine, _factory
    _engine = make_engine(url or get_settings().database_url)
    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _factory


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return _factory or init_engine()


def set_sessionmaker(factory: async_sessionmaker[AsyncSession]) -> None:
    """Used by tests to inject an isolated database."""
    global _factory
    _factory = factory


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = _factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, roll back on any error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
