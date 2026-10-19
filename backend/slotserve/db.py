from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite and always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(expire_on_commit=False)


def configure(database_url: str) -> AsyncEngine:
    global _engine
    _engine = create_async_engine(database_url)

    @event.listens_for(_engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    SessionLocal.configure(bind=_engine)
    return _engine


async def create_all() -> None:
    from . import orm  # noqa: F401  (register tables)

    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
