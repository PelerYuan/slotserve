"""SlotServe 0.3, exclusive bookings: ``reservations.model_id`` becomes nullable; preset and series columns.

Databases from SlotServe <= 0.2 (``user_version`` 0). SQLite cannot drop a NOT NULL constraint with ALTER TABLE,
so the table is rebuilt (create new, copy, drop old, rename: other tables' foreign keys keep pointing at it).
"""

import sqlite3

from . import MigrationReport
from ._util import columns, count

VERSION = 1


def up(con: sqlite3.Connection) -> MigrationReport | None:
    cols = columns(con, "reservations")
    if not cols or not cols["model_id"][3]:  # no such table (created later), or already nullable
        return None
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
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_reservations_{column} ON reservations ({column})")
    return MigrationReport(affected={"reservations": count(con, "reservations")})
