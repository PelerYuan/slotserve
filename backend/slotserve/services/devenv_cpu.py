"""Dev environments without a GPU ("no-GPU mode"): shared CPU compute that needs no booking.

Anyone can start a dev environment from a template, or continue a kept environment (project), without a GPU at
any time: for editing code, moving data, installing packages and CPU jobs. Such a session has ``kind`` "cpu", no
reservation and no lane. It runs until its owner (or an admin) pauses it; there is no idle timeout. Admins limit how
many run per person at once and how much each may use.

CPU fairness: every GPU lane reserves ``devenv_cpu_cores_per_gpu`` cores per GPU for whoever uses the lane (its
booking containers and shared idle environment get them as their cpuset). The remaining cores form the shared pool;
every CPU-only environment gets the whole pool as its cpuset with the same CPU weight, so idle cores are usable by
anyone, plus a hard limit in cores (NanoCpus) and memory. A memory pool caps what all CPU-only environments may claim
together; it is checked when one starts. Host cores and memory come from Docker's info API.

A project running without a GPU is switched to its booking when the booking starts: the CPU-only container is stopped
and the booking recreates it with the lane's GPUs (the usual snapshot path in ``DevEnvManager._resume``). When the
booking ends it is paused as usual, or - when its owner asked for it - continues without a GPU.

Usage: running CPU-only environments are read through Docker's stats API every few seconds (live CPU and memory for
the web page) and written once a minute as ``UsageSample`` rows of resource "cpu", kind "cpu_devenv".

Machines: a CPU-only environment runs on the machine its owner picks (the control plane's by default). Each
machine has its own layout: its cores (from its Docker, or its agent's report) minus what that machine's lanes
reserve. Another machine's Docker (stats included) is reached through the relay.

``DevEnvManager`` calls into this module from a few marked places (``self.cpu``).
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import func, select

from ..db import SessionLocal, utcnow
from ..orm import DevProject, DevSession, DevTemplate, Lane, Reservation, SshKey, UsageSample, User
from .docker_api import DockerError

log = logging.getLogger(__name__)

KIND = "cpu"
USAGE_KIND = "cpu_devenv"
# Sessions that hold a place (count towards the per-person limit and the memory pool); failed ones do not.
ACTIVE_STATES = ("waiting", "pulling", "ready", "starting", "running", "stopping")
POOL_SHARES = 512  # CPU weight of CPU-only environments (Docker's default is 1024): other work wins when contended
STATS_SECONDS = 10.0
HOST_INFO_SECONDS = 300.0


def cpu_ranges(cores: list[int]) -> str:
    """``[0, 1, 2, 5, 7, 8]`` -> ``"0-2,5,7-8"`` (Docker's cpuset format)."""
    out: list[str] = []
    for c in sorted(set(cores)):
        if out and c == _last(out) + 1:
            first = out[-1].split("-")[0]
            out[-1] = f"{first}-{c}"
        else:
            out.append(str(c))
    return ",".join(out)


def _last(ranges: list[str]) -> int:
    return int(ranges[-1].split("-")[-1])


@dataclass
class CoreLayout:
    """Which logical CPUs each GPU lane reserves, and the shared pool CPU-only environments use."""

    total: int
    lanes: dict[int, list[int]] = field(default_factory=dict)
    pool: list[int] = field(default_factory=list)


def layout_cores(total: int, lanes: list[tuple[int, int]], per_gpu: int) -> CoreLayout:
    """Gives every lane ``gpus * per_gpu`` consecutive cores (in lane order, from core 0); the rest is the pool.

    At least one core always stays in the pool. When the lanes want more than there is, each gets its share
    (at least one core while any are left).
    """
    if total <= 0:
        return CoreLayout(0)
    want = [(lane_id, max(0, gpus) * max(0, per_gpu)) for lane_id, gpus in lanes]
    room = total - 1
    asked = sum(w for _, w in want)
    if asked > room:
        # not enough cores: each lane gets its share, rounding leftovers go one by one to the lanes in order
        got = {lane_id: (w * room) // asked for lane_id, w in want}
        left = room - sum(got.values())
        while left > 0:
            hungry = [lane_id for lane_id, w in want if got[lane_id] < w]
            if not hungry:
                break
            hungry.sort(key=lambda lane_id: got[lane_id] > 0)  # lanes with nothing first
            for lane_id in hungry[:left]:
                got[lane_id] += 1
            left = room - sum(got.values())
        want = [(lane_id, got[lane_id]) for lane_id, _ in want]
    layout = CoreLayout(total)
    nxt = 0
    for lane_id, n in want:
        layout.lanes[lane_id] = list(range(nxt, nxt + n))
        nxt += n
    layout.pool = list(range(nxt, total))
    return layout


def lane_gpu_count(lane: Lane) -> int:
    """GPUs of a lane for reserving cores; a lane that does not restrict its GPUs counts as one."""
    return len(lane.devices) if lane.devices else 1


def parse_stats(st: dict) -> tuple[int | None, int, int | None, int | None]:
    """A Docker stats reading -> (CPU time in ns, memory in use, memory limit, CPUs the container may use)."""
    cpu = st.get("cpu_stats") or {}
    total = (cpu.get("cpu_usage") or {}).get("total_usage")
    mem = st.get("memory_stats") or {}
    inner = mem.get("stats") or {}
    cache = inner.get("inactive_file", inner.get("total_inactive_file", inner.get("cache", 0))) or 0
    used = max(0, int(mem.get("usage") or 0) - int(cache))
    limit = mem.get("limit")
    online = cpu.get("online_cpus")
    return (int(total) if total is not None else None), used, (int(limit) if limit else None), online


@dataclass
class Live:
    container_id: str
    cpu_ns: int
    at: float  # loop time of the reading
    cpu: float = 0.0  # cores in use since the reading before
    memory: int = 0
    memory_limit: int | None = None
    time: datetime = field(default_factory=utcnow)


@dataclass
class Pending:
    """Usage of one environment since the last minute row."""

    user_id: int | None
    core_seconds: float = 0.0
    peak: int = 0
    limit: int | None = None
    cores_limit: float | None = None


class CpuEnvs:
    """CPU-only environments: limits, CPU placement, switching to and from bookings, usage sampling."""

    def __init__(self, manager):
        self.m = manager
        self.settings = manager.settings
        self.host: dict | None = None  # this machine's {"cpus": n, "memory": bytes}
        self._host_at: float | None = None
        self._hosts: dict[int, tuple[float, dict | None]] = {}  # other machines: node -> (read at, host)
        self.live: dict[int, Live] = {}  # session id -> last reading
        self._pending: dict[int, Pending] = {}
        self._since = utcnow()
        self._task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        sampler = getattr(manager.state, "usage_sampler", None)
        if sampler is not None and hasattr(sampler, "writers"):
            sampler.writers.append(self.write_usage)

    # ---- host and CPU placement ------------------------------------------------------

    async def host_info(self, force: bool = False, node_id: int | None = None) -> dict | None:
        """Cores and memory of a machine: this one's from Docker; another's from its Docker (through the relay),
        else what its agent reported."""
        key = self.m.node_key(node_id)
        loop = asyncio.get_running_loop()
        if key is not None:
            cached = self._hosts.get(key)
            if not force and cached is not None and loop.time() - cached[0] < HOST_INFO_SECONDS:
                return cached[1]
            host = None
            executor = self.m.executor(key)
            if executor is not None and executor.online:
                try:
                    info = await executor.docker().info()
                    cpus, memory = int(info.get("NCPU") or 0), int(info.get("MemTotal") or 0)
                    host = {"cpus": cpus, "memory": memory} if cpus > 0 else None
                except Exception as exc:  # noqa: BLE001 - best effort, the agent's report is next
                    log.debug("docker info of node %s failed: %s", key, exc)
            if host is None and executor is not None:
                cpus = int(executor.info.get("cpu_cores") or 0)
                memory = int(executor.info.get("memory_total_mb") or 0) * 2**20
                host = {"cpus": cpus, "memory": memory} if cpus > 0 else None
            self._hosts[key] = (loop.time(), host)
            return host
        if not force and self._host_at is not None and loop.time() - self._host_at < HOST_INFO_SECONDS:
            return self.host
        self._host_at = loop.time()
        try:
            info = await self.m.docker.info()
            cpus, memory = int(info.get("NCPU") or 0), int(info.get("MemTotal") or 0)
            self.host = {"cpus": cpus, "memory": memory} if cpus > 0 else None
        except Exception as exc:  # noqa: BLE001 - best effort: without it nothing gets a cpuset
            log.debug("docker info failed: %s", exc)
            self.host = None
        return self.host

    async def layout(self, db, node_id: int | None = None) -> CoreLayout | None:
        """A machine's cores: what each of its lanes reserves, and the pool for CPU-only environments."""
        key = self.m.node_key(node_id)
        host = await self.host_info() if key is None else await self.host_info(node_id=key)
        if host is None:
            return None
        lanes = [  # only the lanes of that machine use its cores
            lane
            for lane in (await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars()
            if self.m.node_key(lane.node_id) == key
        ]
        return layout_cores(
            host["cpus"], [(lane.id, lane_gpu_count(lane)) for lane in lanes], self.settings.devenv_cpu_cores_per_gpu
        )

    def host_of(self, node_id: int | None) -> dict | None:
        """The last known cores and memory of a machine (no lookup)."""
        key = self.m.node_key(node_id)
        if key is None:
            return self.host
        cached = self._hosts.get(key)
        return cached[1] if cached is not None else None

    def core_limit(self, layout: CoreLayout | None) -> float | None:
        """Cores one CPU-only environment may use at most (None: no limit known)."""
        cores = self.settings.devenv_cpu_cores
        pool = len(layout.pool) if layout and layout.pool else None
        if cores > 0:
            return min(cores, pool) if pool else cores
        return float(pool) if pool else None

    async def apply_resources(self, db, s: DevSession, lane: Lane | None, host_config: dict, env: dict) -> None:
        """Called from ``DevEnvManager._plan``: CPU placement and limits of a new container (on its machine)."""
        layout = await self.layout(db, s.node_id)
        if s.kind != KIND:
            # a lane's containers (bookings, its shared idle environment) get the lane's reserved cores
            cores = layout.lanes.get(lane.id) if layout is not None and lane is not None else None
            if cores:
                host_config["CpusetCpus"] = cpu_ranges(cores)
            return
        host_config.pop("DeviceRequests", None)
        # images that set NVIDIA_VISIBLE_DEVICES=all would get every GPU where nvidia is the default runtime
        env["NVIDIA_VISIBLE_DEVICES"] = "void"
        env["SLOTSERVE_GPU"] = "0"
        for key in ("NanoCpus", "Memory"):
            host_config.pop(key, None)
        if layout is not None and layout.pool:
            host_config["CpusetCpus"] = cpu_ranges(layout.pool)
        if (limit := self.core_limit(layout)) is not None and self.settings.devenv_cpu_cores > 0:
            host_config["NanoCpus"] = int(limit * 1e9)
        if self.settings.devenv_cpu_memory_gb > 0:
            host_config["Memory"] = int(self.settings.devenv_cpu_memory_gb * 1024**3)
        host_config["CpuShares"] = POOL_SHARES

    async def refresh_limits(self, container_id: str, host_config: dict, node_id: int | None = None) -> None:
        """Called before a kept container starts again: brings its CPU placement and limits up to date."""
        body: dict = {}
        host = self.host_of(node_id)
        cpuset = host_config.get("CpusetCpus") or (f"0-{host['cpus'] - 1}" if host else None)
        if cpuset:
            body["CpusetCpus"] = cpuset
        if host_config.get("NanoCpus"):
            body["NanoCpus"] = host_config["NanoCpus"]
        if host_config.get("CpuShares"):
            body["CpuShares"] = host_config["CpuShares"]
        if host_config.get("Memory"):
            body["Memory"] = host_config["Memory"]
            body["MemorySwap"] = host_config["Memory"] * 2
        if not body:
            return
        try:
            await self.m.docker_on(node_id).update(container_id, body)
        except (DockerError, httpx.HTTPError, OSError) as exc:
            log.warning("could not update the CPU limits of container %s: %s", container_id[:12], exc)

    # ---- starting and stopping ---------------------------------------------------------

    def enabled(self) -> bool:
        return bool(self.settings.devenv_enabled and self.settings.devenv_cpu_enabled)

    def runs_on(self, node_id: int | None) -> bool:
        """Whether CPU-only environments can start on a machine (``None``: the built-in local node).

        The local node, and every other machine with an agent: its Docker is driven through the relay.
        """
        return self._is_local(node_id) or self.m.executor(node_id) is not None

    def _is_local(self, node_id: int | None) -> bool:
        nodes = getattr(self.m.state, "nodes", None)
        return node_id is None if nodes is None else nodes.is_local(node_id)

    async def active_count(self, db, user_id: int | None = None) -> int:
        stmt = select(func.count(DevSession.id)).where(DevSession.kind == KIND, DevSession.state.in_(ACTIVE_STATES))
        if user_id is not None:
            stmt = stmt.where(DevSession.user_id == user_id)
        return (await db.execute(stmt)).scalar_one()

    def memory_claimed_gb(self, active: int) -> float:
        return active * self.settings.devenv_cpu_memory_gb

    async def check_room(self, db, user_id: int) -> None:
        """Raises a DevEnvError when the person may not start another CPU-only environment now."""
        from .devenv import DevEnvError

        if not self.enabled():
            raise DevEnvError("devenv_cpu_disabled")
        limit = self.settings.devenv_cpu_per_user
        if await self.active_count(db, user_id) >= limit:
            raise DevEnvError("devenv_cpu_limit", limit=limit)
        pool, each = self.settings.devenv_cpu_memory_pool_gb, self.settings.devenv_cpu_memory_gb
        if pool > 0 and each > 0:
            claimed = self.memory_claimed_gb(await self.active_count(db))
            if claimed + each > pool:
                raise DevEnvError("devenv_cpu_memory_full", free=_gb(max(0.0, pool - claimed)), need=_gb(each))

    async def _due_booking(self, db, project_id: int) -> Reservation | None:
        now = utcnow()
        return (
            await db.execute(
                select(Reservation)
                .where(
                    Reservation.devenv_project_id == project_id,
                    Reservation.status == "booked",
                    Reservation.start_at <= now,
                    Reservation.end_at > now,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _project_open(self, db, project_id: int, exclude: int | None = None) -> bool:
        stmt = select(DevSession.id).where(DevSession.project_id == project_id, DevSession.state.in_(ACTIVE_STATES))
        if exclude is not None:
            stmt = stmt.where(DevSession.id != exclude)
        return (await db.execute(stmt.limit(1))).first() is not None

    async def start(
        self,
        db,
        user: User,
        template: DevTemplate | None = None,
        project: DevProject | None = None,
        node_id: int | None = None,
    ) -> DevSession:
        """Starts a CPU-only environment from a template, or continues a kept environment without a GPU, on a
        machine: ``node_id`` (the local node's id or None: the built-in local node), or by default a kept
        environment's own machine."""
        from .devenv import DevEnvError

        if node_id is None and project is not None:
            node_id = project.container_node_id if project.container_id else None
        if not self.runs_on(node_id):
            raise DevEnvError("devenv_cpu_node_unsupported")
        node = self.m.node_key(node_id)
        await self.check_room(db, user.id)
        has_key = (await db.execute(select(SshKey.id).where(SshKey.user_id == user.id).limit(1))).first()
        password = self.settings.devenv_password_login and user.devenv_password_hash
        if has_key is None and not password:
            raise DevEnvError("ssh_login_required" if self.settings.devenv_password_login else "ssh_key_required")
        if project is not None:
            if await self._project_open(db, project.id):
                raise DevEnvError("devenv_cpu_project_running")
            if await self._due_booking(db, project.id) is not None:
                raise DevEnvError("devenv_cpu_project_booked")
            template_id, image = project.template_id, project.image
        elif template is not None:
            template_id, image = template.id, template.image
        else:
            raise DevEnvError("not_found")
        s = DevSession(
            kind=KIND,
            user_id=user.id,
            template_id=template_id,
            project_id=project.id if project else None,
            node_id=node,
            image=image,
            state="waiting",
        )
        db.add(s)
        await db.commit()
        self.m.poke()
        return s

    async def stop(self, session_id: int) -> None:
        """Pauses a CPU-only environment (a kept one keeps its container; any other is removed)."""
        from .devenv import OPEN_STATES, DevEnvError

        async with self.m._lock, SessionLocal() as db:
            s = await db.get(DevSession, session_id)
            if s is None or s.kind != KIND or s.state not in OPEN_STATES:
                raise DevEnvError("devenv_not_running")
            await self.m._stop(db, s, "stopped")
            await db.commit()
        self.live.pop(session_id, None)
        self.m.poke()

    async def should_stop(self, db, s: DevSession) -> bool:
        """Called from ``DevEnvManager._advance`` for CPU-only sessions: its owner is gone, or its project's
        booking has started (the booking takes the container over, with the lane's GPUs)."""
        user = await db.get(User, s.user_id) if s.user_id else None
        if user is None or user.disabled_at is not None:
            return True
        return bool(s.project_id and await self._due_booking(db, s.project_id) is not None)

    async def project_busy(self, db, s: DevSession) -> bool:
        """Called from ``DevEnvManager._gpu_free``: a booking of a project waits while the project's CPU-only
        container still runs (it is stopped as soon as the booking is due)."""
        if not s.project_id or s.kind == KIND:
            return False
        busy = await db.execute(
            select(DevSession.id).where(
                DevSession.project_id == s.project_id,
                DevSession.kind == KIND,
                DevSession.id != s.id,
                DevSession.state.in_(("starting", "running", "stopping")),
            )
        )
        return busy.first() is not None

    async def follow_project_image(self, db, s: DevSession) -> None:
        """A session that has not started yet uses its project's current image (another session of the project
        may have snapshotted the container meanwhile, and the image it was created with is gone)."""
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        if project is not None and project.image != s.image:
            s.image = project.image

    async def after_booking(self, db, s: DevSession) -> None:
        """Called when a booking's session ends: continues its project without a GPU when its owner asked for it."""
        if s.kind == KIND or not s.project_id or s.started_at is None or not self.enabled():
            return  # (a booking cancelled before it started does not start anything)
        project = await db.get(DevProject, s.project_id)
        user = await db.get(User, s.user_id) if s.user_id else None
        if project is None or not project.cpu_after_booking or user is None or user.disabled_at is not None:
            return
        if await self._due_booking(db, project.id) is not None or await self._project_open(db, project.id, s.id):
            return
        from .devenv import DevEnvError

        try:
            await self.check_room(db, user.id)
        except DevEnvError as exc:
            log.info("project %s is not continued without a GPU: %s", project.id, exc.code)
            return
        db.add(
            DevSession(
                kind=KIND,
                user_id=user.id,
                template_id=project.template_id,
                project_id=project.id,
                node_id=self.m.node_key(s.node_id),  # stays on the machine of the booking
                image=project.image,
                state="waiting",
            )
        )
        await db.commit()
        self.m.poke()

    # ---- live use and usage sampling --------------------------------------------------

    def start_monitor(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="devenv-cpu-stats")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reading the use of CPU-only environments failed")
            await asyncio.sleep(STATS_SECONDS)

    async def poll(self) -> None:
        """Reads every running CPU-only environment's CPU time and memory; accumulates usage for the minute rows."""
        async with self._poll_lock:
            await self._poll()

    async def _poll(self) -> None:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        DevSession.id,
                        DevSession.container_id,
                        DevSession.user_id,
                        DevSession.started_at,
                        DevSession.node_id,
                    ).where(
                        DevSession.kind == KIND, DevSession.state == "running", DevSession.container_id.is_not(None)
                    )
                )
            ).all()
            layouts: dict[int | None, CoreLayout | None] = {}
            for node in {self.m.node_key(r[4]) for r in rows}:
                if node is None or self._online(node):
                    layouts[node] = await self.layout(db, node)
        running = {r[0] for r in rows}
        for sid in [k for k in self.live if k not in running]:
            del self.live[sid]
        loop = asyncio.get_running_loop()
        for sid, cid, user_id, started_at, node_id in rows:
            node = self.m.node_key(node_id)
            if node not in layouts:
                continue  # its machine is offline: no readings (nothing is counted for that time)
            cores_limit = self.core_limit(layouts[node])
            try:
                st = await self.m.docker_on(node).stats(cid)
            except Exception as exc:  # noqa: BLE001 - Docker or the relay; the next reading tries again
                log.debug("stats of %s failed: %s", cid, exc)
                continue
            if not st:
                continue
            cpu_ns, memory, memory_limit, online = parse_stats(st)
            if cpu_ns is None:
                continue
            now = loop.time()
            prev = self.live.get(sid)
            if prev is not None and prev.container_id == cid:
                # the counter starts again at zero when the container restarts
                delta = cpu_ns - prev.cpu_ns if cpu_ns >= prev.cpu_ns else cpu_ns
                cores = delta / 1e9 / (now - prev.at) if now > prev.at else 0.0
            else:
                # first reading: count everything when it started while SlotServe was watching
                delta = cpu_ns if started_at is not None and started_at >= self._since else 0
                cores = 0.0
            self.live[sid] = Live(cid, cpu_ns, now, round(cores, 3), memory, memory_limit)
            acc = self._pending.setdefault(sid, Pending(user_id))
            acc.core_seconds += delta / 1e9
            acc.peak = max(acc.peak, memory)
            acc.limit = memory_limit
            acc.cores_limit = cores_limit or (float(online) if online else None)

    def _online(self, node: int) -> bool:
        executor = self.m.executor(node)
        return executor is not None and executor.online

    async def write_usage(self, db, minute: datetime, seconds: int) -> int:
        """Called by the usage sampler once a minute: one "cpu" row per CPU-only environment that ran."""
        with contextlib.suppress(Exception):
            await self.poll()
        pending, self._pending = self._pending, {}
        written = 0
        for acc in pending.values():
            used = acc.core_seconds
            util = 0.0
            if acc.cores_limit:
                util = min(100.0, 100.0 * used / (acc.cores_limit * seconds)) * seconds if seconds else 0.0
            db.add(
                UsageSample(
                    start=minute,
                    hourly=False,
                    seconds=seconds,
                    resource="cpu",
                    lane_id=None,
                    gpu_index=-1,
                    kind=USAGE_KIND,
                    user_id=acc.user_id,
                    reservation_id=None,
                    metered_seconds=seconds,
                    util_seconds=util,
                    busy_seconds=0,
                    energy_wh=0.0,
                    mem_peak_mb=acc.peak // 2**20,
                    mem_total_mb=acc.limit // 2**20 if acc.limit else None,
                    cpu_core_seconds=round(used, 3),
                )
            )
            written += 1
        return written

    def live_out(self, session_id: int) -> dict | None:
        live = self.live.get(session_id)
        if live is None:
            return None
        return {
            "cpu": live.cpu,
            "memory": live.memory,
            "memory_limit": live.memory_limit,
            "at": live.time,
        }


def _gb(value: float) -> str:
    return f"{value:g}"
