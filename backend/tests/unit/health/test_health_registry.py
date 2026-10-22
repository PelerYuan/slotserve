"""Check registry pieces that need no app: titles, task supervision, backup schedule and targets."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slotserve.services import health, maintenance


def test_titles_render_params_in_both_languages():
    health.register_titles({"x_test": ("Lane {lanes} failed ({count})", "通道 {lanes} 失败（{count}）")})
    assert health.title("x_test", {"lanes": ["a", "b"], "count": 2}) == "Lane a, b failed (2)"
    assert health.title("x_test", {"lanes": ["a"]}, "zh-CN") == "通道 a 失败（{count}）"
    assert health.title("unknown_key") == "unknown_key"


def test_check_result_rejects_unknown_levels():
    with pytest.raises(ValueError):
        health.CheckResult("fatal", "x")


async def test_supervised_restarts_a_crashed_loop_and_counts_logged_errors(monkeypatch):
    monkeypatch.setattr(health, "RESTART_DELAYS", (0,))
    health.reset_runtime_state()
    runs = []

    async def loop():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        for _ in range(3):  # a loop that swallows its errors and logs them
            health.logging.getLogger("slotserve.services.fake").error("tick failed")
        await asyncio.sleep(3600)

    task = asyncio.create_task(health.supervised("fake", loop))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(runs) == 2 and health.TASKS["fake"].recent_errors(datetime.now(UTC)) >= 4:
            break
    info = health.TASKS["fake"]
    assert len(runs) == 2 and info.restarts == 1 and info.state == "running"
    assert info.recent_errors(datetime.now(UTC)) >= 4 and info.last_error == "tick failed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert info.state == "cancelled"
    health.reset_runtime_state()


def test_backup_due_follows_the_backup_hour(settings, tmp_path):
    s = settings(data_dir=tmp_path, backup_keep=7, backup_hour=3, timezone="UTC")
    (tmp_path / "slotserve.db").write_bytes(b"")
    assert maintenance.backup_due(s, datetime(2026, 10, 22, 12, tzinfo=UTC))  # none yet
    (tmp_path / "backups" / "slotserve-daily-20261022-020000").mkdir(parents=True)
    assert not maintenance.backup_due(s, datetime(2026, 10, 22, 2, 30, tzinfo=UTC))
    assert not maintenance.backup_due(s, datetime(2026, 10, 22, 4, tzinfo=UTC))  # < 6 h after the last one
    assert maintenance.backup_due(s, datetime(2026, 10, 22, 8, 1, tzinfo=UTC))
    s.backup_hour = -1
    assert not maintenance.backup_due(s, datetime(2026, 10, 23, 1, tzinfo=UTC))
    assert maintenance.backup_due(s, datetime(2026, 10, 23, 2, tzinfo=UTC))


def test_backup_names_kinds_and_targets(tmp_path):
    bundle = tmp_path / "slotserve-manual-20261022-101500"
    bundle.mkdir()
    assert maintenance.kind_of(bundle) == "manual" and maintenance.is_backup(bundle)
    assert maintenance.stamp_of(bundle) == datetime(2026, 10, 22, 10, 15, tzinfo=UTC)
    assert maintenance.kind_of(Path("slotserve-v0.8.0+schema1-to-v0.10+schema5-20261022-101500")) == "upgrade"
    assert maintenance.parse_target("") is None
    assert maintenance.parse_target("storage:3") == ("storage", 3)
    assert maintenance.parse_target("/mnt/nas/slotserve") == ("path", Path("/mnt/nas/slotserve"))
    for bad in ("storage:x", "relative/dir"):
        with pytest.raises(maintenance.BackupError):
            maintenance.parse_target(bad)
    assert maintenance.key_fingerprint("a") != maintenance.key_fingerprint("b")
