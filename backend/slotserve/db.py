import json
import logging
import shutil
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.types import TypeDecorator

from . import __version__, migrations

log = logging.getLogger(__name__)


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

# A transaction that writes these tables commits with ``synchronous=FULL`` (survives a power cut, not only a
# crash); everything else uses NORMAL, which in WAL mode can lose the last commits on power loss but never breaks
# consistency. SQLite only switches outside a transaction, so this applies when such a row is part of the
# transaction's first flush (the usual case: one add + commit); a later flush in an open transaction stays NORMAL.
DURABLE_TABLES = frozenset({"users", "api_keys", "ssh_keys", "audit_logs", "app_settings"})


def configure(database_url: str) -> AsyncEngine:
    global _engine
    _engine = create_async_engine(database_url)

    @event.listens_for(_engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(_engine.sync_engine, "checkout")
    def _normal_sync(dbapi_conn, _record, _proxy):
        # a connection that last wrote a durable table comes back with FULL; every transaction starts at NORMAL
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    SessionLocal.configure(bind=_engine)
    return _engine


@event.listens_for(Session, "before_flush")
def _durable_commit(session: Session, _context, _instances) -> None:
    """Switches this transaction's connection to ``synchronous=FULL`` when it writes a DURABLE_TABLES row."""
    if session.info.get("durable"):
        return
    if not any(
        getattr(o, "__tablename__", None) in DURABLE_TABLES for o in (*session.new, *session.dirty, *session.deleted)
    ):
        return
    session.info["durable"] = True
    conn = session.connection()
    if not getattr(conn.connection.driver_connection, "in_transaction", True):
        conn.exec_driver_sql("PRAGMA synchronous=FULL")


@event.listens_for(Session, "after_transaction_end")
def _durable_done(session: Session, _transaction) -> None:
    session.info.pop("durable", None)


# ---- schema migrations -------------------------------------------------------
#
# ``PRAGMA user_version`` is the number of the last migration applied (slotserve/migrations/NNNN_*.py); 0 is a
# database from SlotServe <= 0.2, 1 one from 0.3 up to the point numbering began. ``prepare`` runs before the
# async engine opens the file: it refuses a database newer than this code, takes a snapshot whenever the schema
# or the SlotServe version changes, and runs the pending migrations in one transaction (all or nothing).
# A brand-new database is created by ``create_all`` and stamped with the current version.

MIGRATIONS = migrations.discover()
SCHEMA_VERSION = len(MIGRATIONS)
REPORT_NAME = "migration_report.json"

_BOOKKEEPING = (
    "CREATE TABLE IF NOT EXISTS schema_meta (key VARCHAR(64) NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS schema_history (
        id INTEGER NOT NULL PRIMARY KEY,
        version INTEGER NOT NULL,
        name VARCHAR(128) NOT NULL,
        app_version VARCHAR(32) NOT NULL,
        applied_at VARCHAR(32) NOT NULL,
        snapshot TEXT,
        report TEXT
    )""",
)


class SchemaError(RuntimeError):
    """The database cannot be used by this code as it is; the message says what to do."""


class SchemaTooNew(SchemaError):
    pass


class MigrationRequired(SchemaError):
    pass


def _user_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return {r[0] for r in rows} - {"schema_meta", "schema_history"}


def _app_version(con: sqlite3.Connection) -> str | None:
    try:
        row = con.execute("SELECT value FROM schema_meta WHERE key='app_version'").fetchone()
    except sqlite3.OperationalError:  # no bookkeeping yet
        return None
    return row[0] if row else None


def _too_new(db_path: Path, version: int, backup_dir: Path) -> SchemaTooNew:
    return SchemaTooNew(
        f"{db_path} has schema version {version}, but SlotServe {__version__} only knows up to {SCHEMA_VERSION}: "
        "it was used by a newer SlotServe. Run that newer version again, or restore the snapshot taken before "
        f"the upgrade (the newest slotserve-v*-to-v* folder in {backup_dir}: copy its files back into the data "
        "directory) before starting this version."
    )


def _label(app: str | None, schema: int) -> str:
    return f"{app}+schema{schema}" if app else f"schema{schema}"


def _snapshot(con: sqlite3.Connection, data_dir: Path, backup_dir: Path, old: str, new: str) -> Path:
    """Copies the database, ``secret_key`` and the dev environment host keys into a new folder in ``backup_dir``."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    final = backup_dir / f"slotserve-v{old}-to-v{new}-{stamp}"
    partial = final.with_name(final.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    target = sqlite3.connect(partial / "slotserve.db")
    try:
        with target:
            con.backup(target)
    finally:
        target.close()
    if (data_dir / "secret_key").is_file():
        shutil.copy2(data_dir / "secret_key", partial / "secret_key")
    if (data_dir / "devenv").is_dir():
        shutil.copytree(data_dir / "devenv", partial / "devenv")
    manifest = {"from": old, "to": new, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    partial.rename(final)
    return final


def _fk_problems(con: sqlite3.Connection) -> set[tuple]:
    return {tuple(row[:3]) for row in con.execute("PRAGMA foreign_key_check")}


def _record(execute, version: int, name: str, snapshot: Path | None, report: dict | None) -> None:
    """Notes one step in ``schema_history``; ``execute`` is ``sqlite3.Connection.execute`` or ``exec_driver_sql``."""
    for ddl in _BOOKKEEPING:
        execute(ddl)
    execute(
        "INSERT INTO schema_history (version, name, app_version, applied_at, snapshot, report) VALUES (?,?,?,?,?,?)",
        (
            version,
            name,
            __version__,
            datetime.now(UTC).isoformat(timespec="seconds"),
            str(snapshot) if snapshot else None,
            json.dumps(report) if report else None,
        ),
    )
    execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('app_version', ?)", (__version__,))


def check(db_path: Path, backup_dir: Path) -> int:
    """The schema version of an existing database; raises SchemaTooNew for one this code cannot open."""
    if not db_path.is_file():
        return SCHEMA_VERSION
    con = sqlite3.connect(db_path)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if not _user_tables(con):
            return SCHEMA_VERSION
    finally:
        con.close()
    if version > SCHEMA_VERSION:
        raise _too_new(db_path, version, backup_dir)
    return version


def prepare(db_path: Path, backup_dir: Path, *, migrate: bool = True) -> int | None:
    """Brings an existing database up to SCHEMA_VERSION; call before ``configure``/``create_all``.

    Returns the schema version it was migrated from, or ``None`` when no migration ran. Raises SchemaTooNew for
    a database from a newer SlotServe, and with ``migrate=False`` (tools that must not upgrade, like the CLI)
    MigrationRequired instead of migrating. Any failure rolls back every step: the file is left as it was.
    """
    if not db_path.is_file():
        return None
    data_dir = db_path.parent
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if not _user_tables(con):
            return None  # empty file: create_all makes a new database
        if version > SCHEMA_VERSION:
            raise _too_new(db_path, version, backup_dir)
        old_app = _app_version(con)
        if version == SCHEMA_VERSION and old_app == __version__:
            return None
        if version < SCHEMA_VERSION and not migrate:
            raise MigrationRequired(
                f"{db_path} has schema version {version}; SlotServe {__version__} needs {SCHEMA_VERSION}. "
                "Start the server once (it takes a snapshot and upgrades the database), then run this again."
            )
        if not migrate:
            return None  # only the SlotServe version changed: the server records it (with a snapshot) on start

        snapshot = _snapshot(con, data_dir, backup_dir, _label(old_app, version), _label(__version__, SCHEMA_VERSION))
        log.info("database snapshot before the upgrade: %s", snapshot)
        if version == SCHEMA_VERSION:  # same schema, different SlotServe: only remember the new version
            con.execute("BEGIN IMMEDIATE")
            _record(con.execute, version, "app_version", snapshot, None)
            con.execute("COMMIT")
            return None

        steps = [m for m in MIGRATIONS if m.version > version]
        con.execute("PRAGMA foreign_keys=OFF")  # table rebuilds must not cascade
        con.execute("PRAGMA synchronous=FULL")
        con.execute("BEGIN IMMEDIATE")
        results = []
        try:
            fk_before = _fk_problems(con)
            for step in steps:
                report = step.up(con) or migrations.MigrationReport()
                results.append({"version": step.version, "name": step.name, **report.as_dict()})
                _record(con.execute, step.version, step.name, snapshot, results[-1])
            fk_new = _fk_problems(con) - fk_before
            if fk_new:
                raise RuntimeError(f"foreign key check failed after migration: {sorted(fk_new)[:5]}")
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            log.error("database upgrade failed and was rolled back; the snapshot is in %s", snapshot)
            raise
        _write_report(data_dir, version, old_app, snapshot, results)
        return version
    finally:
        con.close()


def _write_report(data_dir: Path, version: int, old_app: str | None, snapshot: Path, steps: list[dict]) -> None:
    dropped = sum(len(ids) for step in steps for ids in step["dropped"].values())
    report = {
        "from": _label(old_app, version),
        "to": _label(__version__, SCHEMA_VERSION),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "snapshot": str(snapshot),
        "dropped_rows": dropped,
        "steps": steps,
    }
    (data_dir / REPORT_NAME).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if dropped:
        log.warning("the upgrade could not keep %s row(s); see %s", dropped, data_dir / REPORT_NAME)


async def create_all() -> None:
    """Creates a new database (stamped with SCHEMA_VERSION); for an existing one, checks it was migrated."""
    from . import orm  # noqa: F401  (register tables)

    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(_create_or_verify)


def _create_or_verify(conn) -> None:
    version = conn.exec_driver_sql("PRAGMA user_version").scalar()
    names = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    fresh = not ({r[0] for r in names} - {"schema_meta", "schema_history"})
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(f"database schema version {version} is newer than this code ({SCHEMA_VERSION})")
    if not fresh and version < SCHEMA_VERSION:
        raise MigrationRequired(f"database schema version {version} needs db.prepare() first ({SCHEMA_VERSION})")
    Base.metadata.create_all(conn)
    if fresh:
        _record(conn.exec_driver_sql, SCHEMA_VERSION, "create", None, None)
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
        return
    missing = []
    for table in Base.metadata.sorted_tables:
        have = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
        missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    if missing:
        raise SchemaError(f"columns without a migration: {', '.join(missing)} (add one in slotserve/migrations)")


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
