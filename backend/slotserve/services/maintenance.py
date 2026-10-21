"""Daily database backups and data retention."""

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm import AuditLog, DownloadTask, ModelLoadEvent, UsageDaily, UsageLog, WaitlistEntry
from . import usage_sampler

log = logging.getLogger(__name__)

BACKUP_RE = re.compile(r"^slotserve-(daily|manual)-\d{8}-\d{6}\.db$")
LOAD_EVENTS_KEPT = 50  # per model; estimates only use the last few


# ---- backups -------------------------------------------------------------------


def backup_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def list_backups(settings: Settings) -> list[Path]:
    root = backup_dir(settings)
    if not root.is_dir():
        return []
    files = [p for p in root.iterdir() if p.is_file() and p.suffix == ".db"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def make_backup(settings: Settings, kind: str = "daily") -> Path:
    """Consistent online copy using SQLite's backup API (safe while the app is writing)."""
    root = backup_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = root / f"slotserve-{kind}-{stamp}.db"
    src = sqlite3.connect(settings.data_dir / "slotserve.db")
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    prune_backups(settings)
    return target


def prune_backups(settings: Settings) -> None:
    """Keeps the newest ``backup_keep`` automatic and manual backups; upgrade backups are never removed."""
    keep = settings.backup_keep
    if keep <= 0:
        return
    for kind in ("daily", "manual"):
        old = [p for p in list_backups(settings) if p.name.startswith(f"slotserve-{kind}-")][keep:]
        for p in old:
            p.unlink(missing_ok=True)


def backup_due(settings: Settings, now: datetime) -> bool:
    if settings.backup_keep <= 0 or not (settings.data_dir / "slotserve.db").is_file():
        return False
    daily = [p for p in list_backups(settings) if p.name.startswith("slotserve-daily-")]
    if not daily:
        return True
    newest = datetime.fromtimestamp(daily[0].stat().st_mtime, UTC)
    return now - newest >= timedelta(hours=24)


# ---- retention -----------------------------------------------------------------


async def apply_retention(db: AsyncSession, settings: Settings, now: datetime) -> dict[str, int]:
    """Rolls old request rows up into ``usage_daily`` and deletes rows past their retention."""
    stats: dict[str, int] = {}
    if settings.usage_retention_days > 0:
        cutoff = now - timedelta(days=settings.usage_retention_days)
        day = func.strftime("%Y-%m-%d", UsageLog.created_at)
        rows = (
            await db.execute(
                select(
                    day,
                    UsageLog.user_id,
                    UsageLog.model_name,
                    func.count(UsageLog.id),
                    func.sum(UsageLog.status_code >= 400),
                    func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                    func.coalesce(func.sum(UsageLog.duration_ms), 0),
                )
                .where(UsageLog.created_at < cutoff)
                .group_by(day, UsageLog.user_id, UsageLog.model_name)
            )
        ).all()
        for d, uid, model, n, failed, prompt, completion, ms in rows:
            model = model or ""
            existing = await db.get(UsageDaily, (d, uid, model))
            if existing is None:
                existing = UsageDaily(
                    date=d,
                    user_id=uid,
                    model_name=model,
                    requests=0,
                    failed=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    duration_ms=0,
                )
                db.add(existing)
            existing.requests += n
            existing.failed += int(failed or 0)
            existing.prompt_tokens += prompt
            existing.completion_tokens += completion
            existing.duration_ms += ms
        result = await db.execute(delete(UsageLog).where(UsageLog.created_at < cutoff))
        stats["usage_rows"] = result.rowcount or 0
    stats["usage_samples"] = await usage_sampler.roll_up(db, settings.usage_sample_days, now)
    if settings.audit_retention_days > 0:
        cutoff = now - timedelta(days=settings.audit_retention_days)
        result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        stats["audit_rows"] = result.rowcount or 0

    result = await db.execute(
        delete(DownloadTask).where(
            DownloadTask.status.in_(("done", "failed", "cancelled")),
            DownloadTask.finished_at < now - timedelta(days=90),
        )
    )
    stats["downloads"] = result.rowcount or 0
    result = await db.execute(
        delete(WaitlistEntry).where(
            WaitlistEntry.status.in_(("booked", "expired", "cancelled", "notified")),
            WaitlistEntry.end_at < now - timedelta(days=30),
        )
    )
    stats["waitlist"] = result.rowcount or 0

    model_ids = (await db.execute(select(ModelLoadEvent.model_id).distinct())).scalars().all()
    pruned = 0
    for mid in model_ids:
        keep = (
            select(ModelLoadEvent.id)
            .where(ModelLoadEvent.model_id == mid)
            .order_by(ModelLoadEvent.started_at.desc())
            .limit(LOAD_EVENTS_KEPT)
        )
        result = await db.execute(
            delete(ModelLoadEvent).where(ModelLoadEvent.model_id == mid, ModelLoadEvent.id.not_in(keep))
        )
        pruned += result.rowcount or 0
    stats["load_events"] = pruned
    await db.commit()
    return stats
