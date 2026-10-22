"""In-app inbox and health check state (W1-04): the ``notifications`` and ``check_state`` tables.

Both are new tables, so nothing existing is touched; the frozen schema is exactly what ``create_all`` produces.
"""

import sqlite3

from . import MigrationReport
from ._util import apply_schema

VERSION = 5

SCHEMA = """
CREATE TABLE notifications (
    id INTEGER NOT NULL,
    user_id INTEGER,
    event VARCHAR(48) NOT NULL,
    category VARCHAR(16) NOT NULL,
    level VARCHAR(8) NOT NULL,
    title_key VARCHAR(64) NOT NULL,
    params_json TEXT,
    message TEXT NOT NULL,
    link VARCHAR(255),
    interruption_id INTEGER,
    created_at DATETIME NOT NULL,
    read_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_notifications_user_read ON notifications (user_id, read_at);
CREATE INDEX ix_notifications_created_at ON notifications (created_at);
CREATE TABLE check_state (
    id VARCHAR(64) NOT NULL,
    level VARCHAR(8) NOT NULL,
    since DATETIME NOT NULL,
    last_run DATETIME,
    title_key VARCHAR(64),
    params_json TEXT,
    evidence TEXT,
    alerted_at DATETIME,
    dismissed_by INTEGER,
    dismissed_reason TEXT,
    dismissed_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(dismissed_by) REFERENCES users (id) ON DELETE SET NULL
);
"""


def up(con: sqlite3.Connection) -> MigrationReport | None:
    report = apply_schema(con, SCHEMA)
    return report if report.affected else None
