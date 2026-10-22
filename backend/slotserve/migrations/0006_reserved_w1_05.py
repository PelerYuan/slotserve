"""Placeholder for the number reserved by W1-05 (credentials, sessions, invites); does nothing.

Migration numbers must be contiguous, and this branch's own step is 0007. Delete this file when W1-05's real 0006
is merged (or renumber 0007 if W1-05 ships without a migration).
"""

import sqlite3

from . import MigrationReport

VERSION = 6


def up(con: sqlite3.Connection) -> MigrationReport | None:
    return None
