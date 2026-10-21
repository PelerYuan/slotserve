import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

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
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


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


# ---- schema migrations -------------------------------------------------------
#
# ``PRAGMA user_version`` holds the schema version. Version 0 is every database
# created before versioning (SlotServe <= 0.2). Steps that SQLite cannot express
# with ALTER TABLE (changing a column's NULL constraint) rebuild the table; new
# tables and new nullable columns are added by ``create_all`` afterwards.

SCHEMA_VERSION = 1


def _m1_reservation_model_optional(con: sqlite3.Connection) -> None:
    """Exclusive bookings: ``reservations.model_id`` becomes nullable; add preset and series columns."""
    con.execute(
        """CREATE TABLE reservations_new (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            model_id INTEGER REFERENCES models (id) ON DELETE RESTRICT,
            preset_id INTEGER REFERENCES model_presets (id) ON DELETE SET NULL,
            series_id VARCHAR(36),
            start_at DATETIME NOT NULL,
            end_at DATETIME NOT NULL,
            status VARCHAR(16) NOT NULL,
            note TEXT NOT NULL,
            released_at DATETIME,
            created_at DATETIME NOT NULL
        )"""
    )
    con.execute(
        """INSERT INTO reservations_new
           (id, user_id, model_id, start_at, end_at, status, note, released_at, created_at)
           SELECT id, user_id, model_id, start_at, end_at, status, note, released_at, created_at FROM reservations"""
    )
    con.execute("DROP TABLE reservations")
    con.execute("ALTER TABLE reservations_new RENAME TO reservations")
    for column in ("user_id", "model_id", "series_id", "start_at", "end_at", "status"):
        con.execute(f"CREATE INDEX ix_reservations_{column} ON reservations ({column})")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {0: _m1_reservation_model_optional}


def prepare(db_path: Path, backup_dir: Path) -> int | None:
    """Upgrades an existing database in place, after copying it to ``backup_dir``.

    Returns the version that was upgraded from, or ``None`` when nothing had to be done.
    Runs on a plain sqlite3 connection before the async engine opens the file.
    """
    if not db_path.is_file():
        return None
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        has_schema = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if not has_schema or version >= SCHEMA_VERSION:
            return None
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = sqlite3.connect(backup_dir / f"slotserve-v{version}-{stamp}.db")
        with target:
            con.backup(target)
        target.close()

        con.execute("PRAGMA foreign_keys=OFF")  # table rebuilds must not cascade
        con.execute("BEGIN")
        try:
            for step in range(version, SCHEMA_VERSION):
                MIGRATIONS[step](con)
            problems = con.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key check failed after migration: {problems[:5]}")
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return version
    finally:
        con.close()


async def create_all() -> None:
    from . import orm  # noqa: F401  (register tables)

    assert _engine is not None
    async with _engine.begin() as conn:
        rebuilt = await conn.run_sync(_detach_strict_devenv_sessions)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        if rebuilt:
            await conn.run_sync(_refill_devenv_sessions, rebuilt)
        await conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")


def _detach_strict_devenv_sessions(conn) -> list[str]:
    """Pre-release 0.9 databases: ``devenv_sessions`` had NOT NULL booking columns (shared sessions have none).

    The old table is renamed so ``create_all`` makes the new one; returns the columns to copy over.
    """
    info = list(conn.exec_driver_sql('PRAGMA table_info("devenv_sessions")'))
    if not any(row[1] == "reservation_id" and row[3] for row in info):
        return []
    for (name,) in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='devenv_sessions' AND sql IS NOT NULL"
    ).all():
        conn.exec_driver_sql(f'DROP INDEX "{name}"')
    conn.exec_driver_sql("ALTER TABLE devenv_sessions RENAME TO devenv_sessions_old")
    return [row[1] for row in info]


def _refill_devenv_sessions(conn, columns: list[str]) -> None:
    keep = {row[1] for row in conn.exec_driver_sql('PRAGMA table_info("devenv_sessions")')}
    cols = ", ".join(f'"{c}"' for c in columns if c in keep)
    conn.exec_driver_sql(
        f"INSERT INTO devenv_sessions ({cols}) SELECT {cols} FROM devenv_sessions_old"
        " WHERE reservation_id IN (SELECT id FROM reservations) AND user_id IN (SELECT id FROM users)"
    )
    conn.exec_driver_sql("DROP TABLE devenv_sessions_old")


def _add_missing_columns(conn) -> None:
    """Minimal forward migration: add nullable columns that newer versions introduced."""
    for table in Base.metadata.sorted_tables:
        existing = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                raise RuntimeError(f"cannot auto-migrate NOT NULL column {table.name}.{column.name}")
            ddl = column.type.compile(dialect=conn.dialect)
            conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
