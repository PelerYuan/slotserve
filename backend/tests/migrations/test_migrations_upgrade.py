"""Numbered migrations: real release databases upgrade without losing rows, with a snapshot, all or nothing."""

import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest
from migration_helpers import RELEASES, fresh_database, load_fixture, row_counts, schema, sha256, user_version

from slotserve import __version__, migrations
from slotserve import db as dbmod
from slotserve.migrations import Migration, MigrationReport

BACKEND = Path(__file__).resolve().parents[2]


def _data(tmp_path: Path, release: str) -> Path:
    return load_fixture(release, tmp_path / "data" / "slotserve.db")


@pytest.mark.parametrize("release", RELEASES)
def test_release_database_upgrades_to_current_schema(tmp_path, release):
    path = _data(tmp_path, release)
    before = row_counts(path)
    old_version = user_version(path)
    backups = tmp_path / "data" / "backups"

    assert dbmod.prepare(path, backups) == old_version

    # a snapshot of everything as it was, named after both versions
    [snap] = backups.glob("slotserve-v*-to-v*")
    assert snap.name.startswith(f"slotserve-vschema{old_version}-to-v{__version__}+schema{dbmod.SCHEMA_VERSION}-")
    assert row_counts(snap / "slotserve.db") == before
    assert json.loads((snap / "manifest.json").read_text())["from"] == f"schema{old_version}"
    # every row is still there, and the schema is exactly what a new installation gets
    after = row_counts(path)
    assert {t: after[t] for t in before} == before
    assert schema(path) == schema(fresh_database(tmp_path / "fresh.db"))
    assert user_version(path) == dbmod.SCHEMA_VERSION
    report = json.loads((tmp_path / "data" / "migration_report.json").read_text())
    assert report["dropped_rows"] == 0 and report["snapshot"] == str(snap)
    assert [s["version"] for s in report["steps"]] == list(range(old_version + 1, dbmod.SCHEMA_VERSION + 1))
    con = sqlite3.connect(path)
    assert [r[0] for r in con.execute("SELECT version FROM schema_history ORDER BY id")] == [
        s["version"] for s in report["steps"]
    ]
    assert con.execute("SELECT value FROM schema_meta WHERE key='app_version'").fetchone()[0] == __version__
    assert not con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()

    # the next start has nothing to do: no second snapshot
    assert dbmod.prepare(path, backups) is None
    assert len(list(backups.iterdir())) == 1


@pytest.mark.parametrize("release", ["fresh", *RELEASES])
def test_migrations_are_idempotent(tmp_path, release):
    """Every step can meet a database that already has its change (branches renumber when they merge)."""
    if release == "fresh":
        path = fresh_database(tmp_path / "slotserve.db")
    else:
        path = _data(tmp_path, release)
        dbmod.prepare(path, tmp_path / "backups")
    expected, counts = schema(path), row_counts(path)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys=OFF")
    for step in dbmod.MIGRATIONS:
        assert step.up(con) is None, step.name
    con.close()
    assert schema(path) == expected and row_counts(path) == counts


def test_new_database_is_stamped(tmp_path):
    path = fresh_database(tmp_path / "slotserve.db")
    assert user_version(path) == dbmod.SCHEMA_VERSION
    con = sqlite3.connect(path)
    assert con.execute("SELECT version, name FROM schema_history").fetchall() == [(dbmod.SCHEMA_VERSION, "create")]
    con.close()
    assert dbmod.prepare(path, tmp_path / "backups") is None
    assert not (tmp_path / "backups").exists()  # nothing changed: no snapshot


def test_failed_migration_leaves_database_untouched(tmp_path, monkeypatch):
    """A step that fails (here: a NOT NULL column SQLite cannot add) rolls back every step before it."""
    path = _data(tmp_path, "v0.8.0")
    digest = sha256(path)

    def broken(con):
        con.execute("ALTER TABLE users ADD COLUMN required TEXT NOT NULL")

    steps = [*dbmod.MIGRATIONS, Migration(dbmod.SCHEMA_VERSION + 1, "9999_broken", broken)]
    monkeypatch.setattr(dbmod, "MIGRATIONS", steps)
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", len(steps))
    with pytest.raises(sqlite3.OperationalError, match="NOT NULL"):
        dbmod.prepare(path, tmp_path / "data" / "backups")
    assert sha256(path) == digest
    assert user_version(path) == 1
    assert len(list((tmp_path / "data" / "backups").glob("slotserve-v*-to-v*"))) == 1  # the snapshot stays
    assert not (tmp_path / "data" / "migration_report.json").exists()


def test_newer_database_is_refused(tmp_path):
    path = fresh_database(tmp_path / "data" / "slotserve.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=999")
    con.close()
    digest = sha256(path)
    with pytest.raises(dbmod.SchemaTooNew, match="newer SlotServe"):
        dbmod.prepare(path, tmp_path / "data" / "backups")
    with pytest.raises(dbmod.SchemaTooNew):
        dbmod.check(path, tmp_path / "data" / "backups")
    assert sha256(path) == digest and not (tmp_path / "data" / "backups").exists()

    # the server itself exits with an error instead of starting
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "SLOTSERVE_DATA_DIR": str(tmp_path / "data"),
        "SLOTSERVE_MODELS_DIR": str(tmp_path / "models"),
        "SLOTSERVE_PORT": "1",  # never reached
    }
    proc = subprocess.run(
        [sys.executable, "-m", "slotserve"], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "schema version 999" in proc.stderr


def test_cli_does_not_migrate_but_the_next_start_does(tmp_path):
    """Running reset-password/set-admin before the first start of a new version must not skip the snapshot."""
    path = _data(tmp_path, "v0.8.0")
    digest = sha256(path)
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "SLOTSERVE_DATA_DIR": str(tmp_path / "data"),
        "SLOTSERVE_MODELS_DIR": str(tmp_path / "models"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "slotserve", "set-admin", "alice"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0 and "Start the server once" in proc.stderr
    assert sha256(path) == digest
    assert not (tmp_path / "data" / "backups").exists()

    assert dbmod.prepare(path, tmp_path / "data" / "backups") == 1
    assert len(list((tmp_path / "data" / "backups").glob("slotserve-v*-to-v*"))) == 1


def test_new_slotserve_version_takes_a_snapshot(tmp_path):
    path = fresh_database(tmp_path / "data" / "slotserve.db")
    (tmp_path / "data" / "secret_key").write_text("k")
    (tmp_path / "data" / "devenv").mkdir()
    (tmp_path / "data" / "devenv" / "ssh_host_ed25519").write_bytes(b"key")
    con = sqlite3.connect(path)
    con.execute("UPDATE schema_meta SET value='0.0.1' WHERE key='app_version'")
    con.commit()
    con.close()

    assert dbmod.prepare(path, tmp_path / "data" / "backups") is None  # no schema step ran
    [snap] = (tmp_path / "data" / "backups").iterdir()
    assert snap.name.startswith(f"slotserve-v0.0.1+schema{dbmod.SCHEMA_VERSION}-to-v{__version__}+schema")
    assert (snap / "secret_key").read_text() == "k"
    assert (snap / "devenv" / "ssh_host_ed25519").read_bytes() == b"key"
    con = sqlite3.connect(path)
    assert con.execute("SELECT value FROM schema_meta WHERE key='app_version'").fetchone()[0] == __version__
    con.close()
    assert dbmod.prepare(path, tmp_path / "data" / "backups") is None
    assert len(list((tmp_path / "data" / "backups").iterdir())) == 1


def test_dropped_rows_are_reported(tmp_path):
    """Dev environment sessions of a build after 0.8.0 whose booking is gone cannot be kept: they are listed."""
    path = _data(tmp_path, "v0.8.0")
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE devenv_sessions (id INTEGER NOT NULL PRIMARY KEY, reservation_id INTEGER NOT NULL UNIQUE,
        user_id INTEGER NOT NULL, template_id INTEGER, lane_id INTEGER, image VARCHAR(255) NOT NULL,
        state VARCHAR(16) NOT NULL, detail TEXT, progress FLOAT, error TEXT, container_id VARCHAR(80),
        container_name VARCHAR(80), ssh_port INTEGER, created_at DATETIME NOT NULL, started_at DATETIME,
        stopped_at DATETIME)"""
    )
    con.execute("CREATE INDEX ix_devenv_sessions_user_id ON devenv_sessions (user_id)")
    con.executemany(
        "INSERT INTO devenv_sessions (id, reservation_id, user_id, image, state, created_at) "
        "VALUES (?, ?, 2, 'img', 'stopped', '2026-01-01 00:00:00')",
        [(4, 1), (5, 99)],  # booking 99 does not exist
    )
    con.commit()
    con.close()

    dbmod.prepare(path, tmp_path / "data" / "backups")
    report = json.loads((tmp_path / "data" / "migration_report.json").read_text())
    assert report["dropped_rows"] == 1
    [step] = [s for s in report["steps"] if s["dropped"]]
    assert step["dropped"] == {"devenv_sessions": [5]} and step["notes"]
    con = sqlite3.connect(path)
    assert con.execute("SELECT id, reservation_id FROM devenv_sessions").fetchall() == [(4, 1)]
    cols = {r[1]: r[3] for r in con.execute("PRAGMA table_info(devenv_sessions)")}
    assert cols["reservation_id"] == 0 and "kind" in cols
    con.close()
    assert schema(path) == schema(fresh_database(tmp_path / "fresh.db"))


async def test_unmigrated_database_is_not_opened(tmp_path):
    """``create_all`` never patches an old schema on its own any more: ``prepare`` has to run first."""
    path = load_fixture("v0.8.0", tmp_path / "slotserve.db")
    dbmod.configure(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        with pytest.raises(dbmod.MigrationRequired):
            await dbmod.create_all()
    finally:
        await dbmod.dispose()


def test_migration_files_are_numbered():
    assert [m.version for m in dbmod.MIGRATIONS] == list(range(1, dbmod.SCHEMA_VERSION + 1))
    module = types.ModuleType("x")
    module.VERSION = 3
    module.up = lambda con: MigrationReport()
    with pytest.raises(RuntimeError, match="VERSION must be 4"):
        migrations._load(module, 4, "0004_x")
