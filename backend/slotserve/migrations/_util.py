"""Idempotent building blocks for migrations (plain sqlite3, no ORM)."""

import sqlite3

from . import MigrationReport


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """``{name: PRAGMA table_info row}`` (cid, name, type, notnull, dflt_value, pk); empty if the table is missing."""
    return {row[1]: row for row in con.execute(f'PRAGMA table_info("{table}")')}


def index_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is not None


def add_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE ADD COLUMN`` unless the column exists. ``ddl`` is the type plus constraints, e.g. ``TEXT``.

    SQLite only adds a NOT NULL column together with a non-NULL DEFAULT. Returns whether it was added.
    """
    if column in columns(con, table):
        return False
    con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    return True


def count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def apply_schema(con: sqlite3.Connection, schema: str) -> MigrationReport:
    """Brings the database up to a frozen schema: creates missing tables and indexes, adds missing columns.

    ``schema`` is the ``CREATE TABLE``/``CREATE INDEX`` script of the target (as a freshly created database has
    it). Existing tables keep their rows; a missing column that is NOT NULL without a DEFAULT cannot be added
    this way and raises (write an explicit rebuild step for it).
    """
    report = MigrationReport()
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(schema)
        objects = ref.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        for kind, name, _table, sql in objects:
            if kind != "table":
                continue
            if not table_exists(con, name):
                con.execute(sql)
                report.affected[f"{name} (created)"] = 0
                continue
            have = columns(con, name)
            for _cid, col, col_type, notnull, default, _pk in ref.execute(f'PRAGMA table_info("{name}")'):
                if col in have:
                    continue
                if notnull and default is None:
                    raise RuntimeError(f"cannot add NOT NULL column {name}.{col} without a default")
                ddl = col_type + (f" NOT NULL DEFAULT {default}" if notnull else "")
                if not notnull and default is not None:
                    ddl += f" DEFAULT {default}"
                add_column(con, name, col, ddl)
                report.affected[f"{name}.{col} (added)"] = count(con, name)
        for kind, name, table, sql in objects:
            if kind == "index" and not index_exists(con, name):
                have = columns(con, table)
                ref_cols = [row[2] for row in ref.execute(f'PRAGMA index_info("{name}")')]
                if all(c in have for c in ref_cols):
                    con.execute(sql)
    finally:
        ref.close()
    return report
