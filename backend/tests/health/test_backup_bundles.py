"""Backup bundles, the off-machine copy, failure alerts, restore with its reconciliation report (R1-14)."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from slotserve.config import Settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevProject, LlmModel, Storage, User
from slotserve.services import maintenance

BACKEND = Path(__file__).resolve().parents[2]


async def test_unwritable_backup_folder_alerts_and_the_age_metric_exists(app, make_client, tmp_path, monkeypatch):
    admin = await make_client("root")
    await admin.put("/api/admin/settings", json={"metrics_token": "m-token-3"})
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the backup folder's parent should be")
    monkeypatch.setattr(maintenance, "backup_dir", lambda settings: blocker / "backups")  # cannot be created
    hk = app.state.housekeeper
    hk._backup_failed_at = None
    await hk._backup(utcnow())
    await app.state.health.run_checks(force=True, ids={"backup"})
    body = (await admin.get("/api/health")).json()
    backup = next(c for c in body["checks"] if c["id"] == "backup")
    assert backup["level"] == "error" and backup["title_key"] == "backup_failed" and backup["evidence"]
    assert any("backup failed" in p for p in body["problems"])
    items = []
    for _ in range(50):
        items = (await admin.get("/api/me/notifications")).json()["items"]
        if any(i["event"] == "check_alert" and i["params"]["check"] == "backup" for i in items):
            break
        await asyncio.sleep(0.1)
    assert any(i["event"] == "check_alert" and i["params"]["check"] == "backup" for i in items)
    text = (await (await make_client()).get("/metrics", headers={"authorization": "Bearer m-token-3"})).text
    assert "slotserve_backup_age_seconds +Inf" in text
    assert 'slotserve_check_status{check="backup",group="backup"} 3' in text


async def test_stale_backup_is_an_error(app, make_client):
    admin = await make_client("root")
    settings = app.state.settings
    old = maintenance.backup_dir(settings) / "slotserve-daily-20200101-000000"
    for p in maintenance.list_backups(settings):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    old.mkdir(parents=True)
    await app.state.health.run_checks(force=True, ids={"backup_age"})
    check = next(c for c in (await admin.get("/api/health")).json()["checks"] if c["id"] == "backup_age")
    assert check["level"] == "error" and check["title_key"] == "backup_stale"


async def test_daily_bundle_is_copied_to_a_path_target(app, make_client, tmp_path):
    admin = await make_client("root")
    nas = tmp_path / "nas"
    nas.mkdir()
    r = await admin.put("/api/admin/settings", json={"backup_target": str(nas)})
    assert r.status_code == 200, r.text
    assert (await admin.put("/api/admin/settings", json={"backup_target": "nas"})).status_code == 400
    status = await maintenance.run_backup(app.state.settings, None)
    copy = nas / maintenance.OFFSITE_DIR / status["last_bundle"]
    assert (copy / "slotserve.db").is_file() and (copy / "manifest.json").is_file()
    assert status["offsite"]["last_error"] is None
    await app.state.health.run_checks(force=True, ids={"backup"})
    check = next(c for c in (await admin.get("/api/health")).json()["checks"] if c["id"] == "backup")
    assert check["level"] == "ok" and check["params"]["copied"] is True
    # a target that disappears is an error of its own
    await admin.put("/api/admin/settings", json={"backup_target": str(tmp_path / "gone")})
    await maintenance.run_backup(app.state.settings, None)
    await app.state.health.run_checks(force=True, ids={"backup"})
    check = next(c for c in (await admin.get("/api/health")).json()["checks"] if c["id"] == "backup")
    assert check["level"] == "error" and check["title_key"] == "backup_copy_failed"


async def test_bundle_is_copied_to_a_managed_storage_through_a_helper(app, make_client):
    # The housekeeper's own daily backup would race this test's explicit one; stop it first.
    housekeeper = [t for t in asyncio.all_tasks() if t.get_name() == "housekeeper"]
    for task in housekeeper:
        task.cancel()
    await asyncio.gather(*housekeeper, return_exceptions=True)
    await make_client("root")
    async with SessionLocal() as db:
        st = Storage(name="nas", kind="nfs", purpose="shared", server="nas.local", remote_path="/export/backup")
        db.add(st)
        await db.commit()
        sid = st.id
    app.state.settings.backup_target = f"storage:{sid}"
    fake = app.state.fake_docker
    fake.images.add(app.state.settings.storage_helper_image)
    status = await maintenance.run_backup(app.state.settings, app.state.devenv.docker)
    assert status["offsite"]["last_error"] is None, status
    _name, config = next((n, c) for n, c in fake.created if n == "slotserve-backup-copy")
    assert config["HostConfig"]["Mounts"][0]["Target"] == "/dst"
    assert "slotserve-backups" in config["Entrypoint"][-1]
    files = next(f for f in fake.archives.values() if any(status["last_bundle"] in k for k in f))
    assert f"incoming/{status['last_bundle']}/slotserve.db" in files


async def test_restore_puts_a_bundle_back_and_reconciles(app, make_client, tmp_path):
    await make_client("root")
    settings = app.state.settings
    async with SessionLocal() as db:
        owner = (await db.execute(select(User))).scalars().first()
        db.add(DevProject(owner_id=owner.id, name="thesis", image="ubuntu", state="paused", container_id="c" * 64))
        db.add(LlmModel(name="gone", file_path=str(tmp_path / "missing.gguf"), status="ready"))
        await db.commit()
    status = await maintenance.run_backup(settings, None, "manual", offsite=False)
    bundle = maintenance.backup_dir(settings) / status["last_bundle"]
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == 1 and manifest["rows"]["devenv_projects"] == 1
    assert manifest["kept_environments"][0]["name"] == "thesis"

    # another machine: an empty data directory with its own generated key
    other = Settings.model_construct(
        **{**settings.model_dump(), "data_dir": tmp_path / "other", "secret_key": "change-me"}
    )
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "secret_key").write_text("another-key-entirely\n", encoding="utf-8")
    (bundle / "secret_key").write_text("the-original-key-0123456789\n", encoding="utf-8")
    info = maintenance.restore(other, bundle)
    assert info["secret_key_restored"] and info["prerestore"] is None
    assert (tmp_path / "other" / "secret_key").read_text(encoding="utf-8").strip() == "the-original-key-0123456789"
    assert maintenance.restore_hold(other)
    con = sqlite3.connect(tmp_path / "other" / "slotserve.db")
    assert con.execute("SELECT name FROM devenv_projects").fetchone()[0] == "thesis"
    con.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'other' / 'slotserve.db').as_posix()}")
    fake = app.state.fake_docker
    fake.containers["d" * 64] = {
        "Id": "d" * 64,
        "Name": "slotserve-dev-99",
        "Config": {"Labels": {"slotserve.devenv": "1", "slotserve.session": "99"}},
        "Running": True,
        "ExitCode": 0,
    }
    other.devenv_enabled = True
    async with AsyncSession(engine) as db:
        report = await maintenance.reconcile(other, app.state.devenv.docker, db)
    await engine.dispose()
    assert report["docker"] == "ok"
    assert [c["name"] for c in report["containers_missing"]] == ["thesis"]
    assert [c["session"] for c in report["containers_unknown"]] == ["99"]
    assert [m["name"] for m in report["models_missing"]] == ["gone"]
    maintenance.finish_restore(other)
    assert not maintenance.restore_hold(other)


def test_restore_refuses_a_newer_schema_and_a_broken_file(tmp_path):
    from slotserve.db import SCHEMA_VERSION

    settings = Settings.model_construct(data_dir=tmp_path / "data", secret_key="k" * 20, backup_keep=7)
    newer = tmp_path / "slotserve-manual-20261022-000000"
    newer.mkdir()
    con = sqlite3.connect(newer / "slotserve.db")
    con.execute("CREATE TABLE users (id INTEGER)")
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    con.commit()
    con.close()
    for source, words in ((newer, "newer"), (tmp_path / "nothing", "not a SlotServe backup")):
        try:
            maintenance.restore(settings, source)
        except maintenance.RestoreError as exc:
            assert words in str(exc)
        else:
            raise AssertionError("restore should refuse")
    assert not (tmp_path / "data" / "slotserve.db").exists()


async def test_doctor_exit_code(app, make_client):
    await make_client("root")
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    ok = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "slotserve", "doctor"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "database" in ok.stdout and "test-secret" not in ok.stdout
    env["SLOTSERVE_LLAMA_SERVER_CMD"] = "/nonexistent/llama-server-abc"
    bad = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "slotserve", "doctor", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr
    checks = {c["id"]: c for c in json.loads(bad.stdout)}
    assert checks["engine"]["level"] == "error" and "lanes" not in checks  # server-only checks are skipped
