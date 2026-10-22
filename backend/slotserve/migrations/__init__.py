"""Numbered schema migrations.

Every schema change is one file ``NNNN_slug.py`` in this package (``0004_models_args_pending.py``) with

    VERSION = 4                                   # == the number in the file name
    def up(con: sqlite3.Connection) -> MigrationReport | None: ...

``db.prepare()`` runs the pending ones in order on a plain sqlite3 connection, all inside one transaction, after
taking a snapshot (``backups/slotserve-v<old>-to-v<new>-<stamp>/``); ``PRAGMA user_version`` is the number of
the last one applied. Rules:

- ``up`` must be idempotent: check whether the table/column/index exists before creating it (helpers in
  ``_util``). Branches renumber their migrations when they merge, so a step may meet a database that already has
  its change.
- Use frozen SQL, never the current ORM: the ORM moves on, a migration must keep doing what it did.
- Do not commit, roll back or change ``PRAGMA foreign_keys``; ``prepare`` owns the transaction, runs with foreign
  keys off (table rebuilds must not cascade) and refuses to commit when the steps introduced foreign key errors.
- Never drop rows silently: put what was dropped into the report (it ends up in ``data/migration_report.json``).
- A new ORM table or column needs a migration; ``tests/migrations`` fails when an upgraded fixture database does
  not match a freshly created one.
"""

import importlib
import pkgutil
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType

FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+$")


@dataclass
class MigrationReport:
    """What one step did. ``dropped`` lists the primary keys of rows that could not be carried over."""

    affected: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, list] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"affected": self.affected, "dropped": self.dropped, "notes": self.notes}


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], MigrationReport | None]


def _load(module: ModuleType, number: int, name: str) -> Migration:
    if getattr(module, "VERSION", None) != number:
        raise RuntimeError(f"migration {name}: VERSION must be {number}")
    if not callable(getattr(module, "up", None)):
        raise RuntimeError(f"migration {name}: no up(con) function")
    return Migration(number, name, module.up)


def discover() -> list[Migration]:
    """All migrations, ordered; numbering must be contiguous from 1."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        match = FILE_RE.match(info.name)
        if not match:
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        found.append(_load(module, int(match.group(1)), info.name))
    found.sort(key=lambda m: m.version)
    numbers = [m.version for m in found]
    if numbers != list(range(1, len(found) + 1)):
        raise RuntimeError(f"migration numbers must be 1..n without gaps or duplicates, got {numbers}")
    return found
