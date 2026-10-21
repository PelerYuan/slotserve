"""Usage sampling: once a minute, what every GPU of every lane did and for whom.

Each sample stores utilization, memory and power next to the lane's occupant at that moment
(a booking and its kind, shared use while nobody books, or nothing). Old minute rows are
folded into hourly rows by ``roll_up`` (called from the retention job).

The GPU reader is injectable (``stats``) so this works without GPUs or nvidia-smi. Lanes on other machines
(node agents) are measured with the metrics their agents report.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, utcnow
from ..orm import DevSession, Reservation, UsageSample
from ..runtime import gpu as gpu_module
from ..runtime.manager import RuntimeState
from ..schemas import GpuOut
from . import queries

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 60
# After a short stall (busy event loop, suspended VM) the next sample also covers the missed minutes;
# longer gaps (restarts, clock jumps) stay unaccounted rather than being guessed.
MAX_CATCH_UP_SECONDS = 5 * 60

StatsFn = Callable[[], Awaitable[list[GpuOut]]]


@dataclass(frozen=True)
class Occupant:
    kind: str  # see orm.USAGE_KINDS
    user_id: int | None = None
    reservation_id: int | None = None
    model_name: str | None = None


FREE = Occupant("free")

# Extra ways to recognise shared use of an otherwise unbooked lane (e.g. a lane's idle mode):
# ``async probe(db, lane_context, now) -> Occupant | None``; the first non-None answer wins.
IdleProbe = Callable[[AsyncSession, object, datetime], Awaitable[Occupant | None]]


def booking_kind(r: Reservation) -> str:
    if r.devenv_template_id is not None:
        return "devenv"
    return "gpu" if r.model_id is None else "model"


def lane_gpus(devices: list[int] | None, gpus: list[GpuOut]) -> list[GpuOut]:
    """The GPUs of a lane (``None``: every GPU) among those that reported metrics."""
    if devices is None:
        return list(gpus)
    wanted = set(devices)
    return [g for g in gpus if g.index in wanted]


def gpu_count(devices: list[int] | None, detected: int) -> int:
    """How many GPUs a booking of the lane holds (at least one, also on machines without metrics)."""
    return len(devices) if devices else max(1, detected)


async def attribute(db: AsyncSession, ctx, now: datetime, probes: list[IdleProbe] = ()) -> Occupant:
    """Who held the lane at ``now``, and as what."""
    r = await queries.active_reservation(db, now, ctx.id)
    runtime = ctx.runtime
    serving = runtime.model_id is not None and runtime.state in (RuntimeState.LOADING, RuntimeState.READY)
    if r is not None:
        kind = booking_kind(r)
        return Occupant(kind, r.user_id, r.id, runtime.model_name if kind == "model" and serving else None)
    for probe in probes:
        found = await probe(db, ctx, now)
        if found is not None:
            return found
    shared = (
        await db.execute(
            select(DevSession.id).where(DevSession.lane_id == ctx.id, DevSession.state == "running").limit(1)
        )
    ).scalar_one_or_none()
    if shared is not None:
        return Occupant("idle_devenv")
    if serving:
        return Occupant("idle_model", model_name=runtime.model_name)
    return FREE


class UsageSampler:
    def __init__(self, app_state, stats: StatsFn | None = None):
        self.state = app_state
        self._stats = stats
        self.probes: list[IdleProbe] = []
        self._last: datetime | None = None
        self.current: dict[int, Occupant] = {}  # lane id -> occupant at the last sample

    async def stats(self) -> list[GpuOut]:
        """This machine's GPUs."""
        if self._stats is not None:
            return await self._stats()
        return await gpu_module.gpu_stats()  # looked up at call time so tests can replace it

    async def node_stats(self, ctx, local: list[GpuOut]) -> list[GpuOut]:
        """GPU metrics of the machine a lane runs on (empty while a node is offline: occupancy only)."""
        if getattr(ctx, "node_id", None) is None:
            return local
        nodes = getattr(self.state, "nodes", None)
        executor = nodes.find(ctx.node_id) if nodes is not None else None
        return await executor.gpu_stats() if executor is not None else []

    async def run(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("GPU usage sample failed")
            now = utcnow()
            await asyncio.sleep(max(1.0, SAMPLE_SECONDS - now.second - now.microsecond / 1e6 + 0.5))

    async def sample(self, now: datetime | None = None) -> int:
        """Records one minute (once per minute, also across restarts). Returns the number of rows written."""
        now = now or utcnow()
        minute = now.replace(second=0, microsecond=0)
        seconds = SAMPLE_SECONDS
        if self._last is not None and minute > self._last:
            gap = (minute - self._last).total_seconds()
            if gap <= MAX_CATCH_UP_SECONDS:
                seconds = int(gap)
        lanes = self.state.lanes.ordered()
        gpus = await self.stats()
        busy_pct = self.state.settings.usage_busy_percent
        written = 0
        async with SessionLocal() as db:
            done = (
                await db.execute(
                    select(func.count(UsageSample.id)).where(UsageSample.start == minute, ~UsageSample.hourly)
                )
            ).scalar_one()
            if done:
                return 0  # this minute was recorded already (restart, or the clock went back)
            for ctx in lanes:
                occ = await attribute(db, ctx, now, self.probes)
                self.current[ctx.id] = occ
                own = lane_gpus(ctx.devices, await self.node_stats(ctx, gpus))
                for g in own or [None]:
                    db.add(_row(minute, seconds, ctx.id, occ, g, busy_pct))
                    written += 1
            await db.commit()
        self._last = minute
        return written


def _row(start: datetime, seconds: int, lane_id: int, occ: Occupant, g: GpuOut | None, busy_pct: int) -> UsageSample:
    row = UsageSample(
        start=start,
        hourly=False,
        seconds=seconds,
        resource="gpu",
        lane_id=lane_id,
        gpu_index=-1,
        kind=occ.kind,
        user_id=occ.user_id,
        reservation_id=occ.reservation_id,
        model_name=occ.model_name,
        metered_seconds=0,
        util_seconds=0.0,
        busy_seconds=0,
        energy_wh=0.0,
    )
    if g is not None:
        row.gpu_index = g.index
        row.metered_seconds = seconds
        row.util_seconds = float(g.utilization_pct) * seconds
        row.busy_seconds = seconds if g.utilization_pct >= busy_pct else 0
        row.energy_wh = (g.power_w or 0.0) * seconds / 3600
        row.mem_peak_mb = g.memory_used_mb
        row.mem_total_mb = g.memory_total_mb
    return row


async def roll_up(db: AsyncSession, keep_days: int, now: datetime) -> int:
    """Folds minute rows older than ``keep_days`` (whole hours) into hourly rows; returns rows removed."""
    if keep_days <= 0:
        return 0
    cutoff = (now - timedelta(days=keep_days)).replace(minute=0, second=0, microsecond=0)
    hour = func.strftime("%Y-%m-%d %H:00:00", UsageSample.start)
    keys = (
        UsageSample.resource,
        UsageSample.lane_id,
        UsageSample.gpu_index,
        UsageSample.kind,
        UsageSample.user_id,
        UsageSample.reservation_id,
    )
    rows = (
        await db.execute(
            select(
                hour,
                *keys,
                func.max(UsageSample.model_name),
                func.sum(UsageSample.seconds),
                func.sum(UsageSample.metered_seconds),
                func.sum(UsageSample.util_seconds),
                func.sum(UsageSample.busy_seconds),
                func.sum(UsageSample.energy_wh),
                func.max(UsageSample.mem_peak_mb),
                func.max(UsageSample.mem_total_mb),
                func.sum(UsageSample.cpu_core_seconds),
            )
            .where(UsageSample.start < cutoff, ~UsageSample.hourly)
            .group_by(hour, *keys)
        )
    ).all()
    for h, resource, lane_id, gpu, kind, uid, rid, model, sec, metered, util, busy, wh, peak, total, cores in rows:
        db.add(
            UsageSample(
                start=queries.as_utc(h),
                hourly=True,
                seconds=int(sec or 0),
                resource=resource,
                lane_id=lane_id,
                gpu_index=gpu,
                kind=kind,
                user_id=uid,
                reservation_id=rid,
                model_name=model,
                metered_seconds=int(metered or 0),
                util_seconds=float(util or 0),
                busy_seconds=int(busy or 0),
                energy_wh=float(wh or 0),
                mem_peak_mb=peak,
                mem_total_mb=total,
                cpu_core_seconds=cores,
            )
        )
    result = await db.execute(delete(UsageSample).where(UsageSample.start < cutoff, ~UsageSample.hourly))
    return result.rowcount or 0
