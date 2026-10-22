"""Shared helpers for the migration tests: fixture databases and schema comparison."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path

from slotserve import db as dbmod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "db"
RELEASES = ["v0.2.0", "v0.6.0", "v0.7.0", "v0.8.0"]  # anonymised databases made by each release's own code


def load_fixture(release: str, path: Path, *, wal: bool = True) -> Path:
    """Restores a fixture dump into ``path`` (WAL mode like a running SlotServe leaves it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / f"{release}.sql").read_text(encoding="utf-8"))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.close()
    return path


def row_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def schema(path: Path) -> dict:
    """Tables with their columns (type, NOT NULL, primary key) and the named indexes: what queries depend on."""
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        result = {
            t: {r[1]: (r[2].upper(), bool(r[3]), r[5]) for r in con.execute(f'PRAGMA table_info("{t}")')}
            for t in tables
        }
        indexes = con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
        result["(indexes)"] = sorted(indexes.fetchall())
        return result
    finally:
        con.close()


def fresh_database(path: Path) -> Path:
    """A database created by the current code, the way a new installation gets one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dbmod.configure(f"sqlite+aiosqlite:///{path.as_posix()}")

    async def run():
        try:
            await dbmod.create_all()
        finally:
            await dbmod.dispose()

    asyncio.run(run())
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def user_version(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
