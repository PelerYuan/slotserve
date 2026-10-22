"""Development builds after 0.8.0: ``devenv_sessions`` had NOT NULL booking columns (shared sessions have none).

The table is rebuilt in its current shape. Sessions whose booking or user no longer exists cannot be carried over
(their foreign keys would be broken); their ids are reported instead of being dropped silently.
"""

import sqlite3

from . import MigrationReport
from ._util import columns, count, table_exists

VERSION = 2

TABLE = """CREATE TABLE devenv_sessions_new (
    id INTEGER NOT NULL,
    kind VARCHAR(16),
    reservation_id INTEGER,
    user_id INTEGER,
    project_id INTEGER,
    template_id INTEGER,
    lane_id INTEGER,
    image VARCHAR(255) NOT NULL,
    state VARCHAR(16) NOT NULL,
    detail TEXT,
    progress DOUBLE,
    error TEXT,
    container_id VARCHAR(80),
    container_name VARCHAR(80),
    ssh_port INTEGER,
    web TEXT,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    stopped_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (reservation_id),
    FOREIGN KEY(reservation_id) REFERENCES reservations (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(project_id) REFERENCES devenv_projects (id) ON DELETE SET NULL,
    FOREIGN KEY(template_id) REFERENCES devenv_templates (id) ON DELETE SET NULL,
    FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE SET NULL
)"""


def up(con: sqlite3.Connection) -> MigrationReport | None:
    old = columns(con, "devenv_sessions")
    if not old or not old["reservation_id"][3]:  # no such table, or already the shared-session shape
        return None
    con.execute(TABLE)
    new = columns(con, "devenv_sessions_new")
    keep = ", ".join(f'"{c}"' for c in old if c in new)
    # a booking session whose booking or owner is gone has nothing to belong to any more
    valid = " AND ".join(
        f"{column} IN (SELECT id FROM {table})" if table_exists(con, table) else "0"
        for column, table in (("reservation_id", "reservations"), ("user_id", "users"))
    )
    dropped = [row[0] for row in con.execute(f"SELECT id FROM devenv_sessions WHERE NOT ({valid}) ORDER BY id")]
    con.execute(f"INSERT INTO devenv_sessions_new ({keep}) SELECT {keep} FROM devenv_sessions WHERE {valid}")
    con.execute("DROP TABLE devenv_sessions")
    con.execute("ALTER TABLE devenv_sessions_new RENAME TO devenv_sessions")
    con.execute("CREATE INDEX IF NOT EXISTS ix_devenv_sessions_user_id ON devenv_sessions (user_id)")
    report = MigrationReport(affected={"devenv_sessions": count(con, "devenv_sessions")})
    if dropped:
        report.dropped["devenv_sessions"] = dropped
        report.notes.append("dev environment sessions whose booking or user was already deleted were not kept")
    return report
