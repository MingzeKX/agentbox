"""Async database plumbing (engine, session factory, first-run schema creation)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agent.config import settings
from agent.registry.tables import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

DDL_EXTENSIONS = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(settings.db_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on exception."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(create: bool = True) -> None:
    """Create extensions and tables.  Idempotent."""
    engine = get_engine()
    async with engine.begin() as conn:
        for ddl in DDL_EXTENSIONS:
            await conn.execute(text(ddl))
        if create:
            await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def ping() -> tuple[bool, str]:
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            version = (await conn.execute(text("select version()"))).scalar_one()
            ext = (
                await conn.execute(
                    text("select string_agg(extname, ',' order by extname) from pg_extension where extname in ('vector','pg_trgm')")
                )
            ).scalar_one()
        return True, f"{version} | extensions: {ext or 'none'}"
    except Exception as exc:  # pragma: no cover - depends on environment
        return False, str(exc)


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
