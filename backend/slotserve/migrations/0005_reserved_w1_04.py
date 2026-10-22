"""Placeholder for the number reserved by W1-04 (notifications / check state); does nothing.

Migration numbers must be contiguous, and this branch's own step is 0006. Delete this file when W1-04's real 0005
is merged (or renumber 0006 if W1-04 ships without a migration).
"""

import sqlite3

from . import MigrationReport

VERSION = 5


def up(con: sqlite3.Connection) -> MigrationReport | None:
    return None
