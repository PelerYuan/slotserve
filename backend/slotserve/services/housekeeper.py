"""Periodic jobs: booking reminders, "ready" notices, the waitlist and disk checks."""

import asyncio
import logging
import shutil
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..errors import ApiError
from ..orm import Reservation, SentNotification, User, WaitlistEntry
from ..runtime.manager import RuntimeState
from . import booking, maintenance

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 15


async def once(db: AsyncSession, key: str) -> bool:
    """Claims a one-off notification key; ``False`` if it was already sent."""
    if await db.get(SentNotification, key) is not None:
        return False
    db.add(SentNotification(key=key))
    await db.flush()
    return True


class Housekeeper:
    def __init__(self, app_state):
        self.state = app_state
        self.settings: Settings = app_state.settings
        self._wake = asyncio.Event()
        self._last_disk_check: datetime | None = None
        self._last_retention: datetime | None = None
        self._backup_failed_at: datetime | None = None

    def poke(self) -> None:
        self._wake.set()

    @property
    def notifier(self):
        return self.state.notifier

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")
            try:
                await asyncio.wait_for(self._wake.wait(), INTERVAL_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with SessionLocal() as db:
            await self._expire_pending(db, now)
            booked = await process_waitlist(db, self.settings, self.notifier, now)
            await self._reminders(db, now)
            await self._ready(db)
            await self._disk(db, now)
            await db.execute(delete(SentNotification).where(SentNotification.created_at < now - timedelta(days=60)))
            await db.commit()
            await self.notifier.retry_due(db)
            if self._last_retention is None or now - self._last_retention >= timedelta(hours=6):
                self._last_retention = now
                stats = await maintenance.apply_retention(db, self.settings, now)
                if any(stats.values()):
                    log.info("retention cleanup: %s", stats)
        if booked:
            self.state.lanes.poke()
        await self._backup(now)

    async def _backup(self, now: datetime) -> None:
        if self._backup_failed_at and now - self._backup_failed_at < timedelta(hours=1):
            return
        if not maintenance.backup_due(self.settings, now):
            return
        try:
            path = await asyncio.to_thread(maintenance.make_backup, self.settings, "daily")
            log.info("database backup written to %s", path)
        except Exception:
            self._backup_failed_at = now
            log.exception("database backup failed")

    async def _expire_pending(self, db: AsyncSession, now: datetime) -> None:
        """Requests still unreviewed when they would start lapse and free their time."""
        rows = await db.execute(select(Reservation).where(Reservation.status == "pending", Reservation.start_at <= now))
        expired = list(rows.scalars())
        for r in expired:
            r.status = "expired"
        if expired:
            await db.commit()
        for r in expired:
            self.notifier.emit("booking_expired", user_id=r.user_id, reservation_id=r.id)

    async def _reminders(self, db: AsyncSession, now: datetime) -> None:
        lead = timedelta(minutes=self.settings.reminder_minutes)
        starting = await db.execute(
            select(Reservation).where(
                Reservation.status == "booked", Reservation.start_at > now, Reservation.start_at <= now + lead
            )
        )
        for r in starting.scalars():
            if await once(db, f"res:{r.id}:start:{r.start_at.isoformat()}"):
                self.notifier.emit("reminder_start", user_id=r.user_id, reservation_id=r.id)
        ending = await db.execute(
            select(Reservation).where(
                Reservation.status == "booked",
                Reservation.start_at <= now,
                Reservation.end_at > now,
                Reservation.end_at <= now + lead,
            )
        )
        for r in ending.scalars():
            # Short bookings would get "ends soon" right after "ready"; skip those under twice the lead time.
            if r.end_at - r.start_at >= 2 * lead and await once(db, f"res:{r.id}:end:{r.end_at.isoformat()}"):
                self.notifier.emit("reminder_end", user_id=r.user_id, reservation_id=r.id)

    async def _ready(self, db: AsyncSession) -> None:
        for ctx in self.state.lanes.ordered():
            await self._lane_ready(db, ctx.scheduler.target, ctx.runtime)

    async def _lane_ready(self, db: AsyncSession, target, runtime) -> None:
        if target is None or target.reservation is None:
            return
        r = target.reservation
        devenv = r.devenv_template_id is not None or r.devenv_project_id is not None
        if target.reason == "exclusive" and not devenv and runtime.state == RuntimeState.STOPPED:
            if await once(db, f"res:{r.id}:exclusive"):
                self.notifier.emit("exclusive_ready", user_id=r.user_id, reservation_id=r.id)
        elif (
            target.reason in ("reservation", "preload", "preload-early")
            and runtime.state == RuntimeState.READY
            and runtime.spec == target.spec
            and await once(db, f"res:{r.id}:ready:{target.spec}")
        ):
            self.notifier.emit("model_ready", user_id=r.user_id, reservation_id=r.id)

    async def _disk(self, db: AsyncSession, now: datetime) -> None:
        if self._last_disk_check and now - self._last_disk_check < timedelta(hours=1):
            return
        self._last_disk_check = now
        try:
            free_gb = shutil.disk_usage(self.settings.models_dir).free / 1e9
        except OSError:
            return
        if free_gb < max(2 * self.settings.download_min_free_gb, 5) and await once(db, f"disk:{now.date()}"):
            self.notifier.emit("disk_low", free=f"{free_gb:.1f}")


_waitlist_lock = asyncio.Lock()


async def process_waitlist(db: AsyncSession, settings: Settings, notifier, now: datetime) -> bool:
    """Hands freed time to waitlisted people in the order they joined. Returns whether anything was booked."""
    async with _waitlist_lock:
        return await _process_waitlist(db, settings, notifier, now)


async def _process_waitlist(db: AsyncSession, settings: Settings, notifier, now: datetime) -> bool:
    booked_any = False
    entries = (
        (await db.execute(select(WaitlistEntry).where(WaitlistEntry.status == "waiting").order_by(WaitlistEntry.id)))
        .scalars()
        .all()
    )
    for e in entries:
        start = max(e.start_at, now)
        if e.end_at - start < booking.MIN_LENGTH:
            e.status, e.resolved_at = "expired", now
            continue
        if await booking.find_conflict(db, start, e.end_at, lane_id=e.lane_id) is not None:
            continue
        user = await db.get(User, e.user_id)
        if user is None or user.disabled_at is not None:
            e.status, e.resolved_at = "cancelled", now
            continue
        if not e.auto_book:
            e.status, e.resolved_at = "notified", now
            notifier.emit("waitlist_free", user_id=e.user_id, when=_window(notifier, start, e.end_at))
            continue
        async with booking.booking_lock:
            if await booking.find_conflict(db, start, e.end_at, lane_id=e.lane_id) is not None:
                continue  # taken in the meantime; keep waiting
            try:
                await booking.resolve_model(db, e.model_id, e.preset_id)
                await booking.check_quota(db, settings, user, [(start, e.end_at)])
                if e.lane_id is not None:
                    from ..orm import Lane
                    from . import sharing

                    lane = await db.get(Lane, e.lane_id)
                    if lane is not None:
                        await sharing.check_booking(db, settings, lane, [(start, e.end_at)], user.id)
            except ApiError:
                e.status, e.resolved_at = "notified", now
                notifier.emit("waitlist_free", user_id=e.user_id, when=_window(notifier, start, e.end_at))
                continue
            r = Reservation(
                lane_id=e.lane_id,
                user_id=e.user_id,
                model_id=e.model_id,
                preset_id=e.preset_id,
                start_at=start,
                end_at=e.end_at,
                note=e.note,
            )
            db.add(r)
            await db.flush()
            e.status, e.resolved_at, e.reservation_id = "booked", now, r.id
            booked_any = True
            await db.commit()
        notifier.emit("waitlist_booked", user_id=e.user_id, reservation_id=r.id)
    await db.commit()
    return booked_any


def _window(notifier, start: datetime, end: datetime) -> str:
    return f"{notifier.fmt_time(start)} – {notifier.fmt_time(end)}"
