"""Machines as people choose them: what each node has, how busy it is and what is free on it.

Every machine (the built-in local node and every node agent) is summarized from what it reported (hardware with the
agent's hello, live load with its metrics; the local node reads its own) and from the control plane's own records
(lanes, bookings, CPU-only environments, model files). ``MachineMonitor`` keeps the local readings fresh and a short
load history per machine for the details view.

Availability, per machine:

* GPU lanes: free now (no running booking) or held (by whom, until when); today's and tomorrow's bookings;
* CPU compute left for work without a GPU: the machine's logical cores minus the cores GPU lanes reserve
  (``devenv_cpu_cores_per_gpu`` per GPU, the same layout ``services.devenv_cpu`` applies) minus what running
  CPU-only environments claim; memory minus what is in use or claimed, whichever is more.

Extension point: ``EXTRAS`` functions add optional fields (owner, sharing hours, paused sharing) to every summary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..orm import HOLDING_STATUSES, DevSession, Lane, LlmModel, Node, Reservation, UsageSample, User
from ..schemas import GpuOut
from . import inventory, system

log = logging.getLogger(__name__)

HISTORY_SECONDS = 60.0  # one load history point per machine and minute
HISTORY_POINTS = 180  # three hours
LOCAL_LOAD_SECONDS = 2.0  # local readings are reused for this long
LOCAL_HARDWARE_SECONDS = 600.0
BUSY_CPU_PERCENT = 85.0


# ---- schemas ---------------------------------------------------------------------------------------------------------


class DiskOut(BaseModel):
    label: str  # models | docker
    path: str
    total_bytes: int
    free_bytes: int


class DockerOut(BaseModel):
    available: bool = False
    version: str | None = None
    runtimes: list[str] = []
    gpu_runtime: bool = False  # the NVIDIA container runtime is installed


class GpuDriverOut(BaseModel):
    vendor: str | None = None
    driver_version: str | None = None
    cuda_version: str | None = None


class MachineGpuOut(BaseModel):
    index: int
    name: str
    vendor: str | None = None
    memory_total_mb: int
    memory_used_mb: int | None  # None while the machine is offline
    utilization_pct: int | None
    temperature_c: int | None = None
    power_w: float | None = None
    lane_id: int | None = None  # the lane these GPUs belong to
    lane_name: str | None = None


class MachineBookingOut(BaseModel):
    id: int
    lane_id: int
    start_at: datetime
    end_at: datetime
    username: str
    mine: bool
    status: str  # booked | pending
    kind: str  # model | exclusive | devenv


class MachineLaneOut(BaseModel):
    id: int
    name: str
    gpus: list[int]
    gpu_memory_mb: int | None  # total memory of the lane's GPUs (None: unknown)
    gpu_memory_used_mb: int | None
    state: str  # the lane's model server: stopped | loading | ready | ...
    model_name: str | None
    free_now: bool  # nobody's booking runs on it now (and the machine is online)
    holder: str | None = None  # whose booking runs now
    holder_mine: bool = False
    held_until: datetime | None = None
    next_start_at: datetime | None = None  # the next booking that has not started yet
    bookings: list[MachineBookingOut] = []  # today's and tomorrow's (local time of the caller)


class CapacityOut(BaseModel):
    gpus: int
    gpus_free: int  # GPUs of lanes that are free now
    lanes: int
    lanes_free: int
    gpu_memory_total_mb: int
    gpu_memory_free_mb: int | None  # on all GPUs, by the last reading
    max_lane_gpu_memory_mb: int | None  # the largest lane: what one booking can get
    cpu_cores: int | None  # logical cores
    cpu_cores_lanes: int  # reserved for whoever uses the GPU lanes
    cpu_cores_envs: float  # claimed by running CPU-only environments
    cpu_cores_free: float | None  # left for work without a GPU
    memory_total_mb: int | None
    memory_used_mb: int | None
    memory_reserved_mb: int  # claimed by running CPU-only environments
    memory_free_mb: int | None
    cpu_envs_running: int
    cpu_envs: bool  # CPU-only environments can be started here


class MachineModelOut(BaseModel):
    id: int
    name: str
    size_bytes: int


class MachineOut(BaseModel):
    id: int
    name: str
    is_local: bool
    status: str  # online | offline | never | mismatch
    online: bool
    last_seen_at: datetime | None
    kind: str  # gpu (has GPUs) | cpu (CPU only)
    hostname: str | None = None
    os: str | None = None
    arch: str | None = None
    cpu_model: str | None = None
    cpu_physical_cores: int | None = None
    cpu_logical_cores: int | None = None
    cpu_percent: float | None = None
    load: float | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    disks: list[DiskOut] = []
    docker: DockerOut = DockerOut()
    gpu_driver: GpuDriverOut | None = None
    gpus: list[MachineGpuOut] = []
    lanes: list[MachineLaneOut] = []
    capacity: CapacityOut
    models: list[MachineModelOut] = []
    busy: bool = False  # CPU above BUSY_CPU_PERCENT
    # Filled by EXTRAS (sharing schedules and owners); None / False until something provides them.
    owner: str | None = None
    shared_hours: str | None = None
    sharing_paused: bool = False


class HistoryPointOut(BaseModel):
    at: datetime
    cpu_percent: float | None
    memory_used_mb: int | None
    gpu_util_pct: float | None  # average over the machine's GPUs
    gpu_memory_used_mb: int | None


class GpuHourOut(BaseModel):
    start: datetime
    util_pct: float | None


class MachineHistoryOut(BaseModel):
    node_id: int
    interval_seconds: int
    points: list[HistoryPointOut]  # the last few hours, one per minute while the control plane ran
    gpu_hours: list[GpuHourOut]  # GPU utilization of the machine's lanes per hour, last 24 hours (usage samples)


# Extra fields per machine, e.g. ``{"owner": "alice", "shared_hours": "Mo-Fr 19:00-08:00", "sharing_paused": False}``.
Extras = Callable[[AsyncSession, list[int]], Awaitable[dict[int, dict[str, Any]]]]
EXTRAS: list[Extras] = []


# ---- capacity (pure) ---------------------------------------------------------------------------------------------


def cpu_capacity(
    cores: int | None,
    lane_gpus: list[int],
    cores_per_gpu: int,
    env_cores: list[float],
    memory_total_mb: int | None,
    memory_used_mb: int | None,
    env_memory_mb: list[int],
) -> dict[str, Any]:
    """CPU compute left on a machine for work without a GPU.

    ``lane_gpus``: GPUs of each lane on the machine; ``env_cores`` / ``env_memory_mb``: what each running CPU-only
    environment there claims (its limit, or what it uses when it has none).
    """
    from ..services.devenv_cpu import layout_cores

    lanes_cores = 0
    pool: int | None = None
    if cores:
        layout = layout_cores(cores, list(enumerate(lane_gpus)), cores_per_gpu)
        lanes_cores = sum(len(c) for c in layout.lanes.values())
        pool = len(layout.pool)
    claimed = round(sum(env_cores), 2)
    reserved_mb = int(sum(env_memory_mb))
    memory_free = None
    if memory_total_mb:
        memory_free = max(0, memory_total_mb - max(memory_used_mb or 0, reserved_mb))
    return {
        "cpu_cores": cores,
        "cpu_cores_lanes": lanes_cores,
        "cpu_cores_envs": claimed,
        "cpu_cores_free": round(max(0.0, pool - claimed), 2) if pool is not None else None,
        "memory_total_mb": memory_total_mb,
        "memory_used_mb": memory_used_mb,
        "memory_reserved_mb": reserved_mb,
        "memory_free_mb": memory_free,
    }


def lane_gpu_memory(devices: list[int] | None, gpus: list[GpuOut]) -> tuple[int | None, int | None]:
    """(total, used) memory of a lane's GPUs on its machine; a lane without GPU indices has all of them."""
    mine = [g for g in gpus if devices is None or g.index in devices]
    if not mine:
        return None, None
    return sum(g.memory_total_mb for g in mine), sum(g.memory_used_mb for g in mine)


def booking_kind(r: Reservation) -> str:
    if r.devenv_template_id or r.devenv_project_id:
        return "devenv"
    return "model" if r.model_id else "exclusive"


def day_window(now: datetime, tz_offset: int, days: int = 2) -> tuple[datetime, datetime]:
    """[start of today, start of the day after ``days``) in the caller's local time (minutes east of UTC)."""
    local = now + timedelta(minutes=tz_offset)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=tz_offset)
    return midnight, midnight + timedelta(days=days)


# ---- live readings and history -------------------------------------------------------------------------------------


class NodeView:
    """What is known about one machine right now: static facts, live load and GPUs."""

    def __init__(self, info: dict, hardware: dict, inventory_: dict, metrics: dict, gpus: list[GpuOut], online: bool):
        self.info = info
        self.hardware = hardware
        self.inventory = inventory_
        self.metrics = metrics
        self.gpus = gpus
        self.online = online


class MachineMonitor:
    """Keeps this machine's readings fresh and a short load history of every machine."""

    def __init__(self, app_state):
        self.state = app_state
        self.probe = system.LoadProbe()
        self.history: dict[int, deque[dict]] = {}
        self._local_load: dict = {}
        self._local_at = 0.0
        self._hardware: dict = {}
        self._hardware_at: float | None = None
        self._lock = asyncio.Lock()

    async def local_hardware(self) -> dict:
        now = time.monotonic()
        if self._hardware_at is None or now - self._hardware_at > LOCAL_HARDWARE_SECONDS:
            self._hardware_at = now
            local = self.state.nodes.local

            async def docker_info() -> dict | None:
                return await local.docker().info()

            with contextlib.suppress(Exception):
                self._hardware = await system.hardware(self.state.settings.models_dir, docker_info)
        return self._hardware

    async def local_load(self) -> dict:
        async with self._lock:
            now = time.monotonic()
            if not self._local_load or now - self._local_at > LOCAL_LOAD_SECONDS:
                disks = (await self.local_hardware()).get("disks")
                self._local_load = await asyncio.to_thread(self.probe.read, disks)
                self._local_at = now
            return self._local_load

    async def view(self, node: Node, executor) -> NodeView:
        """``executor``: the node's executor (``None`` when the registry does not know it)."""
        if node.is_local:
            local = self.state.nodes.local
            return NodeView(
                system.machine_info(),
                await self.local_hardware(),
                {"models_dir": str(self.state.settings.models_dir)},
                await self.local_load(),
                await local.gpu_stats(),
                True,
            )
        saved: dict = {}
        if node.inventory:
            with contextlib.suppress(ValueError):
                saved = json.loads(node.inventory)
        info = (executor.info if executor is not None and executor.info else None) or saved.get("info") or {}
        inv = (executor.inventory if executor is not None and executor.inventory else None) or saved.get("inventory")
        online = bool(executor is not None and executor.online)
        metrics = executor.metrics if online else {}
        gpus = [GpuOut(**g) for g in (metrics.get("gpus") or (inv or {}).get("gpus") or [])]
        return NodeView(info, info.get("hardware") or {}, inv or {}, metrics, gpus, online)

    async def sample(self) -> None:
        """One history point for every online machine."""
        from ..db import SessionLocal

        async with SessionLocal() as db:
            nodes = list((await db.execute(select(Node).where(Node.revoked_at.is_(None)))).scalars())
        at = utcnow()
        for node in nodes:
            executor = self.state.nodes.find(node.id) if not node.is_local else self.state.nodes.local
            try:
                view = await self.view(node, executor)
            except Exception:  # one machine must not stop the others
                log.debug("reading machine %s failed", node.name, exc_info=True)
                continue
            if not view.online:
                continue
            gpus = view.gpus
            point = {
                "at": at,
                "cpu_percent": view.metrics.get("cpu_percent"),
                "memory_used_mb": view.metrics.get("memory_used_mb"),
                "gpu_util_pct": round(sum(g.utilization_pct for g in gpus) / len(gpus), 1) if gpus else None,
                "gpu_memory_used_mb": sum(g.memory_used_mb for g in gpus) if gpus else None,
            }
            self.history.setdefault(node.id, deque(maxlen=HISTORY_POINTS)).append(point)

    async def run(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sampling machine load failed")
            await asyncio.sleep(HISTORY_SECONDS)


def monitor(app_state) -> MachineMonitor:
    found = getattr(app_state, "machines", None)
    if found is None:
        found = app_state.machines = MachineMonitor(app_state)
    return found


# ---- summaries -----------------------------------------------------------------------------------------------------


def _state(snap) -> str:
    if snap is None:
        return "stopped"
    return snap.state.value if hasattr(snap.state, "value") else str(snap.state)


def _status(node: Node, online: bool) -> str:
    if node.is_local or online:
        return "online"
    if node.last_error and "protocol" in node.last_error:
        return "mismatch"
    return "offline" if node.last_seen_at is not None else "never"


async def summarize(
    db: AsyncSession, app_state, user: User, tz_offset: int = 0, node_ids: list[int] | None = None
) -> list[MachineOut]:
    """Every machine that is not revoked (or the given ones), local node first."""
    settings = app_state.settings
    registry = app_state.nodes
    lanes_manager = getattr(app_state, "lanes", None)
    cpu = getattr(getattr(app_state, "devenv", None), "cpu", None)
    mon = monitor(app_state)
    now = utcnow()
    stmt = select(Node).where(Node.revoked_at.is_(None)).order_by(Node.is_local.desc(), Node.name)
    if node_ids is not None:
        stmt = stmt.where(Node.id.in_(node_ids))
    nodes = list((await db.execute(stmt)).scalars())
    local_id = registry.local.node_id
    all_lanes = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())

    def lane_node(lane: Lane) -> int | None:
        return local_id if lane.node_id is None else lane.node_id

    # bookings that run now, and today's and tomorrow's
    day_start, day_end = day_window(now, tz_offset)
    rows = list(
        (
            await db.execute(
                select(Reservation)
                .where(
                    Reservation.status.in_(HOLDING_STATUSES),
                    Reservation.end_at > min(now, day_start),
                    Reservation.start_at < day_end,
                )
                .order_by(Reservation.start_at)
            )
        ).scalars()
    )
    upcoming = dict(
        (
            await db.execute(
                select(Reservation.lane_id, func.min(Reservation.start_at))
                .where(Reservation.status == "booked", Reservation.start_at > now)
                .group_by(Reservation.lane_id)
            )
        ).all()
    )
    names = dict((await db.execute(select(User.id, User.username))).all())

    # CPU-only environments that hold a place
    from ..services.devenv_cpu import ACTIVE_STATES, KIND

    envs = list(
        (
            await db.execute(select(DevSession).where(DevSession.kind == KIND, DevSession.state.in_(ACTIVE_STATES)))
        ).scalars()
    )

    # models present per node
    mapping = await inventory.nodes_by_model(db, local_id)
    models = list(
        (
            await db.execute(
                select(LlmModel)
                .where(LlmModel.deleted_at.is_(None), LlmModel.status == "ready")
                .order_by(LlmModel.name)
            )
        ).scalars()
    )

    extras: dict[int, dict[str, Any]] = {}
    for fn in EXTRAS:
        with contextlib.suppress(Exception):
            for nid, values in (await fn(db, [n.id for n in nodes])).items():
                extras.setdefault(nid, {}).update(values)

    out: list[MachineOut] = []
    for node in nodes:
        executor = registry.local if node.is_local else registry.find(node.id)
        view = await mon.view(node, executor)
        online = view.online
        hw, info, metrics = view.hardware, view.info, view.metrics
        mine = [lane for lane in all_lanes if lane_node(lane) == node.id]
        lane_of_gpu: dict[int, Lane] = {}
        for lane in mine:
            for idx in lane.devices if lane.devices is not None else [g.index for g in view.gpus]:
                lane_of_gpu.setdefault(idx, lane)
        vendor = (hw.get("gpu_driver") or {}).get("vendor")
        gpus = [
            MachineGpuOut(
                index=g.index,
                name=g.name,
                vendor=vendor,
                memory_total_mb=g.memory_total_mb,
                memory_used_mb=g.memory_used_mb if online else None,
                utilization_pct=g.utilization_pct if online else None,
                temperature_c=g.temperature_c if online else None,
                power_w=g.power_w if online else None,
                lane_id=lane_of_gpu[g.index].id if g.index in lane_of_gpu else None,
                lane_name=lane_of_gpu[g.index].name if g.index in lane_of_gpu else None,
            )
            for g in view.gpus
        ]

        lane_out: list[MachineLaneOut] = []
        for lane in mine:
            total, used = lane_gpu_memory(lane.devices, view.gpus)
            ctx = lanes_manager.contexts.get(lane.id) if lanes_manager is not None else None
            snap = ctx.runtime.snapshot() if ctx is not None else None
            lane_rows = [r for r in rows if r.lane_id == lane.id]
            running = next((r for r in lane_rows if r.status == "booked" and r.start_at <= now < r.end_at), None)
            lane_out.append(
                MachineLaneOut(
                    id=lane.id,
                    name=lane.name,
                    gpus=lane.devices or [],
                    gpu_memory_mb=total,
                    gpu_memory_used_mb=used if online else None,
                    state=_state(snap),
                    model_name=snap.model_name if snap else None,
                    free_now=online and running is None,
                    holder=names.get(running.user_id) if running else None,
                    holder_mine=bool(running and running.user_id == user.id),
                    held_until=running.end_at if running else None,
                    next_start_at=upcoming.get(lane.id),
                    bookings=[
                        MachineBookingOut(
                            id=r.id,
                            lane_id=lane.id,
                            start_at=r.start_at,
                            end_at=r.end_at,
                            username=names.get(r.user_id, "?"),
                            mine=r.user_id == user.id,
                            status=r.status,
                            kind=booking_kind(r),
                        )
                        for r in lane_rows
                        if r.end_at > day_start and r.start_at < day_end
                    ],
                )
            )

        node_envs = [e for e in envs if (local_id if e.node_id is None else e.node_id) == node.id]
        env_cores, env_mem = [], []
        for e in node_envs:
            live = cpu.live.get(e.id) if cpu is not None else None
            if settings.devenv_cpu_cores > 0:
                env_cores.append(settings.devenv_cpu_cores)
            else:
                env_cores.append(live.cpu if live else 0.0)
            if settings.devenv_cpu_memory_gb > 0:
                env_mem.append(int(settings.devenv_cpu_memory_gb * 1024))
            else:
                env_mem.append(live.memory // 2**20 if live else 0)
        cores = hw.get("cpu_logical_cores") or info.get("cpu_cores") or None
        memory_total = metrics.get("memory_total_mb") or hw.get("memory_total_mb") or info.get("memory_total_mb")
        memory_used = metrics.get("memory_used_mb") if online else None
        cap = cpu_capacity(
            cores,
            [len(lane.devices) if lane.devices else max(1, len(view.gpus)) for lane in mine],
            settings.devenv_cpu_cores_per_gpu,
            env_cores,
            memory_total,
            memory_used,
            env_mem,
        )
        lanes_free = [lane for lane in lane_out if lane.free_now]
        gpu_total = sum(g.memory_total_mb for g in view.gpus)
        capacity = CapacityOut(
            gpus=len(view.gpus),
            gpus_free=sum(len(lane.gpus) or len(view.gpus) for lane in lanes_free),
            lanes=len(lane_out),
            lanes_free=len(lanes_free),
            gpu_memory_total_mb=gpu_total,
            gpu_memory_free_mb=(gpu_total - sum(g.memory_used_mb for g in view.gpus)) if online and view.gpus else None,
            max_lane_gpu_memory_mb=max((lane.gpu_memory_mb or 0 for lane in lane_out), default=0) or None,
            cpu_envs_running=len(node_envs),
            cpu_envs=bool(cpu is not None and cpu.enabled() and cpu.runs_on(node.id)),
            **cap,
        )

        disks = [DiskOut(**d) for d in (metrics.get("disks") or hw.get("disks") or [])]
        if not node.is_local and not disks and view.inventory.get("models_dir"):
            free = view.inventory.get("models_free_bytes")
            if free is not None:
                disks = [DiskOut(label="models", path=view.inventory["models_dir"], total_bytes=0, free_bytes=free)]
        docker = hw.get("docker") or {}
        last_seen = now if online else node.last_seen_at
        if executor is not None and not node.is_local and online and getattr(executor, "last_seen", 0):
            last_seen = now - timedelta(seconds=max(0.0, time.monotonic() - executor.last_seen))
        cpu_percent = metrics.get("cpu_percent") if online else None
        node_models = [
            MachineModelOut(id=m.id, name=m.name, size_bytes=m.size_bytes)
            for m in models
            if node.id in inventory.nodes_of(m.id, mapping, local_id)
        ]
        machine = MachineOut(
            id=node.id,
            name=node.name,
            is_local=node.is_local,
            status=_status(node, online),
            online=online,
            last_seen_at=last_seen,
            kind="gpu" if view.gpus else "cpu",
            hostname=info.get("hostname"),
            os=hw.get("os") or info.get("os"),
            arch=hw.get("arch") or info.get("arch"),
            cpu_model=hw.get("cpu_model"),
            cpu_physical_cores=hw.get("cpu_physical_cores"),
            cpu_logical_cores=cores,
            cpu_percent=cpu_percent,
            load=metrics.get("load") if online else None,
            memory_total_mb=memory_total,
            memory_used_mb=memory_used,
            net_rx_bps=metrics.get("net_rx_bps") if online else None,
            net_tx_bps=metrics.get("net_tx_bps") if online else None,
            disks=disks,
            docker=DockerOut(
                available=bool(docker.get("available")),
                version=docker.get("version"),
                runtimes=docker.get("runtimes") or [],
                gpu_runtime=bool(docker.get("gpu_runtime")),
            ),
            gpu_driver=GpuDriverOut(**hw["gpu_driver"]) if hw.get("gpu_driver") else None,
            gpus=gpus,
            lanes=lane_out,
            capacity=capacity,
            models=node_models,
            busy=cpu_percent is not None and cpu_percent >= BUSY_CPU_PERCENT,
            **{k: v for k, v in extras.get(node.id, {}).items() if k in ("owner", "shared_hours", "sharing_paused")},
        )
        out.append(machine)
    return out


async def history(db: AsyncSession, app_state, node: Node) -> MachineHistoryOut:
    """The machine's recent load (in memory) and its lanes' GPU utilization per hour (usage samples)."""
    mon = monitor(app_state)
    points = [HistoryPointOut(**p) for p in mon.history.get(node.id, [])]
    local_id = app_state.nodes.local.node_id
    lane_ids = [
        lane.id
        for lane in (await db.execute(select(Lane))).scalars()
        if (local_id if lane.node_id is None else lane.node_id) == node.id
    ]
    hours: list[GpuHourOut] = []
    if lane_ids:
        now = utcnow()
        since = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        bucket = func.strftime("%Y-%m-%dT%H:00:00", UsageSample.start)
        found = {
            b: (u, m)
            for b, u, m in (
                await db.execute(
                    select(bucket, func.sum(UsageSample.util_seconds), func.sum(UsageSample.metered_seconds))
                    .where(
                        UsageSample.start >= since,
                        UsageSample.resource == "gpu",
                        UsageSample.lane_id.in_(lane_ids),
                    )
                    .group_by(bucket)
                )
            ).all()
        }
        for i in range(24):
            start = since + timedelta(hours=i)
            util, metered = found.get(start.strftime("%Y-%m-%dT%H:00:00"), (0.0, 0))
            hours.append(GpuHourOut(start=start, util_pct=round(util / metered, 1) if metered else None))
    return MachineHistoryOut(node_id=node.id, interval_seconds=int(HISTORY_SECONDS), points=points, gpu_hours=hours)
