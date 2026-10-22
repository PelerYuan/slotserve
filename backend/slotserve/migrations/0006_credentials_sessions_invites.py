"""Credential family (W1-05, A06): server-side sessions, invite links, API key scope/expiry/environment keys,
the approval state of people who signed up with an invite, and the source of each gateway request."""

import sqlite3

from . import MigrationReport
from ._util import add_column, count, index_exists, table_exists

VERSION = 6

SESSIONS = """
CREATE TABLE sessions (
    id INTEGER NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    user_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    ip VARCHAR(64),
    user_agent VARCHAR(255),
    revoked_at DATETIME,
    revoked_reason VARCHAR(16),
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)
"""

INVITES = """
CREATE TABLE invites (
    id INTEGER NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    prefix VARCHAR(16) NOT NULL,
    note VARCHAR(64) NOT NULL,
    created_by INTEGER,
    created_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    max_uses INTEGER NOT NULL,
    uses INTEGER DEFAULT '0' NOT NULL,
    requires_approval BOOLEAN DEFAULT '0' NOT NULL,
    last_used_at DATETIME,
    revoked_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL
)
"""

INDEXES = {
    "ix_sessions_user_id": "CREATE INDEX ix_sessions_user_id ON sessions (user_id)",
    "ix_sessions_token_hash": "CREATE UNIQUE INDEX ix_sessions_token_hash ON sessions (token_hash)",
    "ix_invites_token_hash": "CREATE UNIQUE INDEX ix_invites_token_hash ON invites (token_hash)",
    "ix_api_keys_devenv_session_id": "CREATE INDEX ix_api_keys_devenv_session_id ON api_keys (devenv_session_id)",
}

COLUMNS = [
    ("api_keys", "kind", "VARCHAR(8) NOT NULL DEFAULT 'api'"),
    ("api_keys", "scope", "VARCHAR(64) NOT NULL DEFAULT 'llm'"),
    ("api_keys", "expires_at", "DATETIME"),
    ("api_keys", "devenv_session_id", "INTEGER REFERENCES devenv_sessions (id) ON DELETE SET NULL"),
    ("api_keys", "last_ip", "VARCHAR(64)"),
    ("users", "approval_pending", "BOOLEAN NOT NULL DEFAULT '0'"),
    ("users", "invite_id", "INTEGER"),
    ("usage_logs", "source", "VARCHAR(16) NOT NULL DEFAULT 'api'"),
]


def up(con: sqlite3.Connection) -> MigrationReport | None:
    report = MigrationReport()
    for table, ddl in (("sessions", SESSIONS), ("invites", INVITES)):
        if not table_exists(con, table):
            con.execute(ddl)
            report.affected[f"{table} (created)"] = 0
    for table, column, ddl in COLUMNS:
        if add_column(con, table, column, ddl):
            report.affected[f"{table}.{column} (added)"] = count(con, table)
    for name, sql in INDEXES.items():
        if not index_exists(con, name):
            con.execute(sql)
    if report.affected:
        # Existing keys keep working unchanged: kind api, scope llm, no expiry.
        report.notes.append("browser sign-ins made before this version end once (sessions are now stored)")
    return report if report.affected else None
