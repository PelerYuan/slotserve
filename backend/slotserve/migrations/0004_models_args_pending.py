"""Engine argument review (W0-02): ``models.args_pending`` holds launch arguments waiting for an admin."""

import sqlite3

from . import MigrationReport
from ._util import add_column, count

VERSION = 4


def up(con: sqlite3.Connection) -> MigrationReport | None:
    if not add_column(con, "models", "args_pending", "TEXT"):
        return None
    return MigrationReport(affected={"models.args_pending (added)": count(con, "models")})
