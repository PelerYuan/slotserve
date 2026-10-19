"""Decides which model should be loaded right now and drives the runtime towards it."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import LlmModel
from ..services import queries
from .manager import LlamaRuntime

log = logging.getLogger(__name__)


@dataclass
class Target:
    model: LlmModel | None
    est_load_seconds: float
    reason: str


async def compute_target(
    db: AsyncSession, settings: Settings, now: datetime, current_model_id: int | None
) -> Target:
    """Pure decision logic (see docs: "Scheduling and preloading")."""
    buffer = settings.preload_buffer_seconds

    active = await queries.active_reservation(db, now)
    if active is not None:
        model = await db.get(LlmModel, active.model_id)
        return Target(model, await queries.estimate_load_seconds(db, model, settings), "reservation")

    default = await queries.default_model(db)
    est_default = await queries.estimate_load_seconds(db, default, settings) if default else 0.0

    upcoming = await queries.next_reservation(db, now)
    if upcoming is not None:
        model = await db.get(LlmModel, upcoming.model_id)
        est_next = await queries.estimate_load_seconds(db, model, settings)
        gap = (upcoming.start_at - now).total_seconds()
        if default is None or model.id != default.id:
            if gap <= est_next + buffer:
                return Target(model, est_next, "preload")
            # Switching back to the default would leave it usable only briefly before the next switch.
            if current_model_id != (default.id if default else None) and gap <= est_default + est_next + 2 * buffer:
                return Target(model, est_next, "preload-early")

    return Target(default, est_default, "default" if default else "idle")


class Scheduler:
    def __init__(self, runtime: LlamaRuntime, settings: Settings):
        self.runtime = runtime
        self.settings = settings
        self._wake = asyncio.Event()
        self._force_restart = False
        self.last_reason: str | None = None

    def poke(self) -> None:
        """Re-evaluate immediately (after a reservation or model change)."""
        self._wake.set()

    def request_restart(self) -> None:
        self._force_restart = True
        self._wake.set()

    async def tick(self) -> None:
        async with SessionLocal() as db:
            target = await compute_target(db, self.settings, utcnow(), self.runtime.model_id)
        if target.reason != self.last_reason:
            log.info("scheduler target: %s (%s)", target.model.name if target.model else None, target.reason)
            self.last_reason = target.reason
        force, self._force_restart = self._force_restart, False
        await self.runtime.ensure(target.model, target.est_load_seconds, force=force)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.settings.scheduler_interval_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
