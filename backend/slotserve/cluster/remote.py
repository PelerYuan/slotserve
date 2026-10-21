"""Executors for node agents: commands go over the agent's WebSocket, data through the relay (``relay.py``)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..config import Settings
from ..db import SessionLocal
from ..orm import LlmModel, ModelPreset, NodeModel
from ..runtime.executor import LaneRuntime, ModelFile, NodeExecutor
from ..runtime.manager import FAIL_BACKOFF_SECONDS, RequestTracker, RuntimeSnapshot, RuntimeState
from ..runtime.progress import LogProgress
from ..schemas import GpuOut
from . import protocol as p
from .rpc import Peer

if TYPE_CHECKING:
    import httpx
    from starlette.websockets import WebSocket

    from ..services.docker_api import DockerClient
    from .relay import Relay

log = logging.getLogger(__name__)
LOG_CHUNK_BYTES = 2 * 1024 * 1024


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class RemoteExecutor(NodeExecutor):
    """A node agent; ``peer`` is set while it is connected."""

    is_local = False

    def __init__(self, settings: Settings, node_id: int, name: str, address: str = ""):
        self.settings = settings
        self.node_id = node_id
        self.name = name
        self.configured_address = address  # set by an admin (shown only: nothing connects to nodes)
        self.reported_address = ""  # sent by the agent (SLOTSERVE_AGENT_ADDRESS)
        self.seen_address = ""  # where the agent connected from
        self.relay: Relay | None = None  # set by the hub
        self.token_hash: str | None = None  # of the connected agent's token (authenticates its data connections)
        self._docker: DockerClient | None = None
        self._http: httpx.AsyncClient | None = None
        self.peer: Peer | None = None
        self.websocket: WebSocket | None = None
        self.protocol: int | None = None
        self.agent_version: str | None = None
        self.info: dict = {}
        self.inventory: dict = {}
        self.metrics: dict = {}
        self.metrics_at: float = 0.0
        self.last_seen: float = 0.0  # monotonic
        self.connected_since: datetime | None = None
        self.received_at: datetime | None = None  # control plane time of the last frame from the agent
        # The agent's clock minus the control plane's (seconds), from timestamps in hello and metrics; times the
        # agent reports are shifted by it, and samples and events are recorded at the control plane's time.
        self.clock_skew: float | None = None
        self.runtimes: dict[int, RemoteRuntime] = {}

    # ---- NodeExecutor -------------------------------------------------------

    @property
    def online(self) -> bool:
        return self.peer is not None and not self.peer.closed

    @property
    def address(self) -> str:
        return self.configured_address or self.reported_address or self.seen_address or "127.0.0.1"

    def create_runtime(self, lane_id: int, devices: list[int] | None) -> LaneRuntime:
        runtime = RemoteRuntime(self, lane_id, devices)
        self.runtimes[lane_id] = runtime
        return runtime

    async def gpu_stats(self) -> list[GpuOut]:
        if not self.online:
            return []
        return [GpuOut(**g) for g in self.metrics.get("gpus") or self.inventory.get("gpus") or []]

    def known_gpus(self) -> list[GpuOut]:
        """GPUs from the last inventory or metrics, also while the node is offline (for configuring lanes)."""
        return [GpuOut(**g) for g in self.metrics.get("gpus") or self.inventory.get("gpus") or []]

    def docker(self) -> DockerClient:
        """The node's Docker API, relayed by its agent (same client and code paths as the local Docker)."""
        if self._docker is None:
            from ..services.docker_api import DockerClient

            self._docker = DockerClient(
                f"relay://{self.name}", transport=self._relay().transport(self, _docker_target), connect_timeout=None
            )
        return self._docker

    def http_client(self) -> httpx.AsyncClient:
        """HTTP to the node's model servers (``http://<anything>:<port>``), relayed by its agent."""
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(
                transport=self._relay().transport(self, _model_target, keepalive=15.0, connections=200),
                timeout=httpx.Timeout(None, connect=self.settings.relay_open_seconds + 5),
                trust_env=False,
            )
        return self._http

    def _relay(self) -> Relay:
        if self.relay is None:
            raise RuntimeError("the relay is not set up")
        return self.relay

    def relay_stats(self) -> dict:
        return self.relay.stats(self.node_id).out() if self.relay is not None else {}

    async def close_clients(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def scan_models(self) -> list[ModelFile]:
        result = await self.request(p.M_MODELS_SCAN, timeout=120)
        return [
            ModelFile(path=f["path"], name=f["name"], size_bytes=int(f.get("size_bytes") or 0)) for f in result["files"]
        ]

    # ---- commands -----------------------------------------------------------

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        peer = self.peer
        if peer is None or peer.closed:
            raise p.RemoteError("offline", f"node {self.name!r} is offline")
        return await peer.request(method, params, timeout or self.settings.node_rpc_timeout_seconds)

    def attach(self, peer: Peer, websocket: WebSocket | None = None) -> None:
        self.peer = peer
        self.websocket = websocket
        self.last_seen = time.monotonic()

    def detach(self) -> None:
        self.peer = None
        self.websocket = None
        self.metrics = {}

    def note_clock(self, agent_time: datetime | None, received: datetime) -> None:
        if agent_time is None:
            return
        skew = (agent_time - received).total_seconds()
        # Smoothed, so one frame delayed in transit does not make the clock look wrong.
        self.clock_skew = skew if self.clock_skew is None else round(self.clock_skew * 0.7 + skew * 0.3, 3)

    def server_time(self, value: Any) -> datetime | None:
        """A time the agent reported, on the control plane's clock."""
        moment = _dt(value)
        if moment is None or not self.clock_skew:
            return moment
        return moment - timedelta(seconds=self.clock_skew)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "address": self.address, "protocol": self.protocol, "relay": self.relay_stats()}


def _docker_target(_host: str, _port: int) -> dict:
    return {"kind": "docker"}


def _model_target(_host: str, port: int) -> dict:
    return {"kind": "model", "port": port}


class RemoteRuntime(RequestTracker):
    """A lane's model server on a node agent.

    The agent runs the real ``LlamaRuntime`` and reports its state (``apply``); this mirror is what the scheduler,
    the gateway and the admin views read. Gateway requests in flight are counted here, on the control plane, so
    a model switch still waits for them before the agent is told to stop the server.
    """

    def __init__(self, executor: RemoteExecutor, lane_id: int, devices: list[int] | None):
        self.executor = executor
        self.settings = executor.settings
        self.lane_id = lane_id
        self.devices = devices
        self._lock = asyncio.Lock()
        self._init_requests()
        self._state = RuntimeState.STOPPED
        self.model_id: int | None = None
        self.model_name: str | None = None
        self.preset_id: int | None = None
        self.preset_name: str | None = None
        self.load_started_at: datetime | None = None
        self.ready_at: datetime | None = None
        self.est_load_seconds: float | None = None
        self.error: str | None = None
        self.error_hint: str | None = None
        self.engine: str | None = None
        self.command: str | None = None
        self.pid: int | None = None
        self.exit_code: int | None = None
        self.failure_log: list[str] = []
        self._failed_at: datetime | None = None
        self._fail_count = 0
        self._next_retry_at: datetime | None = None
        self._port: int | None = None
        self._log_name = f"llama-server-lane{lane_id}.log"
        self._progress = LogProgress(stage=None, percent=None)

    # ---- state mirrored from the agent ----------------------------------------

    @property
    def state(self) -> RuntimeState:
        return self._state if self.executor.online else RuntimeState.OFFLINE

    @state.setter
    def state(self, value: RuntimeState) -> None:
        self._state = value

    def apply(self, data: dict) -> None:
        """Takes over the state the agent reported for this lane."""
        try:
            self._state = RuntimeState(data.get("state") or "stopped")
        except ValueError:
            self._state = RuntimeState.FAILED
        self.model_id = data.get("model_id")
        self.model_name = data.get("model_name")
        self.preset_id = data.get("preset_id")
        self.preset_name = data.get("preset_name")
        self.load_started_at = self.executor.server_time(data.get("load_started_at"))
        self.ready_at = self.executor.server_time(data.get("ready_at"))
        self.est_load_seconds = data.get("est_load_seconds")
        self.error = data.get("error")
        self.error_hint = data.get("error_hint")
        self.engine = data.get("engine")
        self.command = data.get("command")
        self.pid = data.get("pid")
        self.exit_code = data.get("exit_code")
        self.failure_log = list(data.get("failure_log") or [])
        self._failed_at = self.executor.server_time(data.get("failed_at"))
        self._fail_count = int(data.get("fail_count") or 0)
        self._next_retry_at = self.executor.server_time(data.get("next_retry_at"))
        self._port = data.get("port") or self._port
        self._log_name = data.get("log_name") or self._log_name
        self._progress = LogProgress(stage=data.get("stage"), percent=data.get("percent"))

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            model_id=self.model_id,
            model_name=self.model_name,
            preset_id=self.preset_id,
            preset_name=self.preset_name,
            load_started_at=self.load_started_at,
            ready_at=self.ready_at,
            est_load_seconds=self.est_load_seconds,
            error=self.error if self.executor.online else f"node {self.executor.name!r} is offline",
            error_hint=self.error_hint,
            draining=self.draining,
            inflight=self.inflight,
        )

    @property
    def port(self) -> int:
        return self._port or self.settings.llama_port + self.lane_id - 1

    @property
    def base_url(self) -> str:
        return f"http://{self.executor.address}:{self.port}"

    @property
    def spec(self) -> tuple[int, int | None] | None:
        return (self.model_id, self.preset_id) if self.model_id is not None else None

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def next_retry_at(self) -> datetime | None:
        return self._next_retry_at if self._state == RuntimeState.FAILED else None

    @property
    def log_name(self) -> str:
        return self._log_name

    def log_progress(self) -> LogProgress:
        if self.state != RuntimeState.LOADING:
            return LogProgress(stage=None, percent=None)
        return self._progress

    # ---- commands -------------------------------------------------------------

    def _retry_due(self) -> bool:
        if self._failed_at is None:
            return True
        idx = min(max(self._fail_count - 1, 0), len(FAIL_BACKOFF_SECONDS) - 1)
        from ..db import utcnow

        return (utcnow() - self._failed_at).total_seconds() >= FAIL_BACKOFF_SECONDS[idx]

    async def ensure(
        self,
        model: LlmModel | None,
        est_load_seconds: float = 0.0,
        force: bool = False,
        preset: ModelPreset | None = None,
    ) -> None:
        """Like ``LlamaRuntime.ensure``; the agent makes the same decisions, this side only drains requests."""
        if not self.executor.online:
            return  # nothing can change while the node is away; reconciled when it reconnects
        async with self._lock:
            if model is None:
                if self._state == RuntimeState.STOPPED and self.model_id is None:
                    return
                await self._drain()
            else:
                same = self.spec == (model.id, preset.id if preset else None)
                if not force and same and self._state in (RuntimeState.LOADING, RuntimeState.READY):
                    return
                if not force and same and self._state == RuntimeState.FAILED and not self._retry_due():
                    return
                if not same:
                    await self._drain()
            params = {
                "lane_id": self.lane_id,
                "devices": self.devices,
                "model": await self._model_payload(model) if model is not None else None,
                "preset": {"id": preset.id, "name": preset.name, "args": preset.args} if preset else None,
                "est_load_seconds": est_load_seconds,
                "force": force,
            }
            try:
                result = await self.executor.request(
                    p.M_RUNTIME_ENSURE, params, timeout=self.settings.stop_timeout_seconds + 60
                )
            except p.RemoteError as exc:
                log.warning("lane %s on node %s: ensure failed: %s", self.lane_id, self.executor.name, exc)
                return
            if isinstance(result, dict):
                self.apply(result)

    async def _model_payload(self, model: LlmModel) -> dict:
        """The model as the node sees it: its own path when the files live elsewhere there."""
        path = model.file_path
        async with SessionLocal() as db:
            found = (
                await db.execute(
                    select(NodeModel.path).where(
                        NodeModel.node_id == self.executor.node_id, NodeModel.model_id == model.id
                    )
                )
            ).scalar_one_or_none()
        return {
            "id": model.id,
            "name": model.name,
            "path": found or path,
            "engine": model.engine,
            "args": model.args,
        }

    async def shutdown(self) -> None:
        """The lane was removed or moved: stop its server on the node (when it is reachable)."""
        self.executor.runtimes.pop(self.lane_id, None)
        if self.executor.online:
            try:
                await self.executor.request(p.M_RUNTIME_REMOVE, {"lane_id": self.lane_id}, timeout=60)
            except p.RemoteError as exc:
                log.warning("could not stop lane %s on node %s: %s", self.lane_id, self.executor.name, exc)

    async def log_tail(self, lines: int = 200) -> list[str]:
        try:
            return list((await self.executor.request(p.M_LOG_TAIL, {"lane_id": self.lane_id, "lines": lines}))["lines"])
        except p.RemoteError:
            return []

    async def log_size(self) -> int:
        try:
            result = await self.executor.request(p.M_LOG_READ, {"lane_id": self.lane_id, "start": 0, "end": 0})
            return int(result["size"])
        except p.RemoteError:
            return 0

    async def log_read(self, start: int, end: int) -> bytes:
        out = bytearray()
        try:
            while start < end:
                stop = min(end, start + LOG_CHUNK_BYTES)  # keeps every frame well below WebSocket size limits
                result = await self.executor.request(
                    p.M_LOG_READ, {"lane_id": self.lane_id, "start": start, "end": stop}, timeout=60
                )
                data = p.unb64(result["data"])
                out += data
                if len(data) < stop - start:
                    break
                start = stop
        except p.RemoteError:
            pass
        return bytes(out)
