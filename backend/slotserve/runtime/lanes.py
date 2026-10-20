"""One inference process and scheduler per lane (a group of GPUs)."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from sqlalchemy import select, update

from ..config import Settings
from ..db import SessionLocal
from ..orm import Lane, LlmModel, Reservation, WaitlistEntry
from .gpu import gpu_stats
from .manager import LlamaRuntime
from .scheduler import EventSink, Scheduler

log = logging.getLogger(__name__)


@dataclass
class LaneContext:
    id: int
    name: str
    devices: list[int] | None
    position: int
    runtime: LlamaRuntime
    scheduler: Scheduler
    task: asyncio.Task | None = field(default=None, repr=False)


async def ensure_default_lane() -> None:
    """First start (or upgrade): one lane with every detected GPU, owning existing bookings and the default model."""
    async with SessionLocal() as db:
        lane = (await db.execute(select(Lane).order_by(Lane.position, Lane.id).limit(1))).scalar_one_or_none()
        if lane is None:
            indices = [str(g.index) for g in await gpu_stats()]
            default = (
                await db.execute(
                    select(LlmModel.id).where(LlmModel.is_default.is_(True), LlmModel.deleted_at.is_(None)).limit(1)
                )
            ).scalar_one_or_none()
            name = "GPU " + ",".join(indices) if indices else "GPU"
            lane = Lane(name=name, gpu_indices=",".join(indices), default_model_id=default, position=0)
            db.add(lane)
            await db.flush()
            log.info("created lane %r for GPU(s) %s", name, lane.gpu_indices or "(all)")
        await db.execute(update(Reservation).where(Reservation.lane_id.is_(None)).values(lane_id=lane.id))
        await db.execute(update(WaitlistEntry).where(WaitlistEntry.lane_id.is_(None)).values(lane_id=lane.id))
        await db.commit()


class LaneManager:
    def __init__(self, settings: Settings, events: EventSink | None = None):
        self.settings = settings
        self.events = events
        self.contexts: dict[int, LaneContext] = {}
        self._run = False
        self._sync_lock = asyncio.Lock()

    # ---- lookup -------------------------------------------------------------

    def ordered(self) -> list[LaneContext]:
        return sorted(self.contexts.values(), key=lambda c: (c.position, c.id))

    @property
    def primary(self) -> LaneContext:
        return self.ordered()[0]

    def get(self, lane_id: int | None) -> LaneContext:
        if lane_id is not None and lane_id in self.contexts:
            return self.contexts[lane_id]
        return self.primary

    def poke(self, lane_id: int | None = None) -> None:
        if lane_id is None:
            for ctx in self.contexts.values():
                ctx.scheduler.poke()
        elif lane_id in self.contexts:
            self.contexts[lane_id].scheduler.poke()

    def loaded_model_ids(self) -> set[int]:
        return {c.runtime.model_id for c in self.contexts.values() if c.runtime.model_id is not None}

    # ---- lifecycle ----------------------------------------------------------

    async def start(self, run: bool = True) -> None:
        self._run = run
        await self.sync()

    async def sync(self) -> None:
        """Brings the running processes in line with the ``lanes`` table."""
        async with self._sync_lock:
            async with SessionLocal() as db:
                rows = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())
            wanted = {lane.id: lane for lane in rows}
            for lane_id in [i for i in self.contexts if i not in wanted]:
                await self._remove(lane_id)
            for lane in rows:
                ctx = self.contexts.get(lane.id)
                if ctx is None:
                    await self._add(lane)
                    continue
                ctx.name, ctx.position = lane.name, lane.position
                if ctx.devices != lane.devices:
                    ctx.devices = lane.devices
                    ctx.runtime.devices = lane.devices
                    ctx.scheduler.request_restart()
                ctx.scheduler.poke()

    async def _add(self, lane: Lane) -> None:
        runtime = LlamaRuntime(self.settings, lane_id=lane.id, devices=lane.devices)
        scheduler = Scheduler(runtime, self.settings, self.events, lane_id=lane.id)
        await scheduler.restore_manual()
        ctx = LaneContext(lane.id, lane.name, lane.devices, lane.position, runtime, scheduler)
        if self._run:
            ctx.task = asyncio.create_task(scheduler.run(), name=f"scheduler-lane{lane.id}")
        self.contexts[lane.id] = ctx

    async def _remove(self, lane_id: int) -> None:
        ctx = self.contexts.pop(lane_id)
        if ctx.task is not None:
            ctx.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ctx.task
        await ctx.runtime.shutdown()

    async def shutdown(self) -> None:
        for lane_id in list(self.contexts):
            await self._remove(lane_id)
