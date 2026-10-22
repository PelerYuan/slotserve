"""Dev environment hardening (W1-01).

- ``devenv_templates.compat``: templates whose image needs setuid programs run without no-new-privileges
  (compatibility mode, D14); the built-in desktop (webtop, s6-overlay) is one.
- ``devenv_shared_members``: who joined a lane's shared idle environment. Only they get an account there, with
  their keys only (no password hashes in the container any more, R1-10). Everyone who could log in with a key
  before the upgrade is carried over for every lane, so existing users keep their access.
- The built-in VS Code template runs code-server with ``--auth password`` (the proxy signs people in, R1-08) unless
  admins changed its command.
"""

import sqlite3
from datetime import UTC, datetime

from . import MigrationReport
from ._util import add_column, count, index_exists, table_exists

VERSION = 7

OLD_VSCODE = "code-server --bind-addr 0.0.0.0:8080 --auth none --disable-telemetry --disable-update-check /workspace"
NEW_VSCODE = (
    "code-server --bind-addr 0.0.0.0:8080 --auth password --disable-telemetry --disable-update-check /workspace"
)
NEW_VSCODE_ENV = '{"HASHED_PASSWORD": "{token_sha256}"}'

MEMBERS = """CREATE TABLE devenv_shared_members (
	lane_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	joined_at DATETIME NOT NULL,
	PRIMARY KEY (lane_id, user_id),
	FOREIGN KEY(lane_id) REFERENCES lanes (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)"""
MEMBERS_INDEX = "CREATE INDEX ix_devenv_shared_members_user_id ON devenv_shared_members (user_id)"


def up(con: sqlite3.Connection) -> MigrationReport | None:
    report = MigrationReport()
    if add_column(con, "devenv_templates", "compat", "BOOLEAN"):
        con.execute("UPDATE devenv_templates SET compat = 0")
        con.execute("UPDATE devenv_templates SET compat = 1 WHERE builtin_key = 'desktop'")
        report.affected["devenv_templates.compat (added)"] = count(con, "devenv_templates")
    if not table_exists(con, "devenv_shared_members"):
        con.execute(MEMBERS)
        now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
        con.execute(
            """INSERT INTO devenv_shared_members (lane_id, user_id, joined_at)
               SELECT lanes.id, users.id, ? FROM lanes, users
               WHERE users.disabled_at IS NULL AND EXISTS (SELECT 1 FROM ssh_keys WHERE ssh_keys.user_id = users.id)""",
            (now,),
        )
        report.affected["devenv_shared_members (created)"] = count(con, "devenv_shared_members")
        report.notes.append(
            "shared idle environments: people with an SSH key were carried over as members of every lane;"
            " password logins there are off from now on"
        )
    changed = con.execute(
        "UPDATE devenv_templates SET command = ?, env = ? WHERE builtin_key = 'vscode' AND command = ? AND env IS NULL",
        (NEW_VSCODE, NEW_VSCODE_ENV, OLD_VSCODE),
    ).rowcount
    if changed:
        report.affected["devenv_templates (built-in VS Code signs in through the proxy)"] = changed
    if not index_exists(con, "ix_devenv_shared_members_user_id"):
        con.execute(MEMBERS_INDEX)
    return report if report.affected else None
