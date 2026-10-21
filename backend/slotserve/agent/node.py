"""The node agent (``slotserve agent``): runs a machine's lanes for the control plane.

It connects out to the control plane over a WebSocket with the node's token, reports the machine (GPUs, CPU,
memory, model files) and live metrics, and carries out commands: start / stop model servers (the same
``LlamaRuntime`` a single-machine SlotServe uses), read their logs, scan the models directory and download
models into it. Model servers keep running while the control plane is away; when the agent reconnects it reports
what runs and the control plane reconciles (lanes that no longer belong to the node are stopped).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import __version__
from ..cluster import protocol as p
from ..cluster import system
from ..cluster.rpc import Peer
from ..config import Settings
from ..runtime import gpu as gpu_module
from ..runtime.engines import ENGINES
from ..runtime.manager import LlamaRuntime
from ..schemas import GpuOut
from ..services import scanner
from .downloads import AgentDownloads

log = logging.getLogger("slotserve.agent")

StatsFn = Callable[[], Awaitable[list[GpuOut]]]
REPORT_SECONDS = 0.5  # how often runtime state changes are looked for
MAX_BACKOFF = 30.0
FATAL_BACKOFF = 60.0
MAX_PENDING_EVENTS = 200


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def runtime_report(rt: LlamaRuntime) -> dict:
    """Everything the control plane mirrors about one lane's model server (see cluster.remote.RemoteRuntime)."""
    progress = rt.log_progress()
    return {
        "lane_id": rt.lane_id,
        "devices": rt.devices,
        "state": rt.state.value,
        "model_id": rt.model_id,
        "model_name": rt.model_name,
        "preset_id": rt.preset_id,
        "preset_name": rt.preset_name,
        "load_started_at": _iso(rt.load_started_at),
        "ready_at": _iso(rt.ready_at),
        "est_load_seconds": rt.est_load_seconds,
        "error": rt.error,
        "error_hint": rt.error_hint,
        "engine": rt.engine,
        "command": rt.command,
        "pid": rt.pid,
        "exit_code": rt.exit_code,
        "failure_log": rt.failure_log,
        "failed_at": _iso(rt._failed_at),
        "fail_count": rt.fail_count,
        "next_retry_at": _iso(rt.next_retry_at),
        "port": rt.port,
        "log_name": rt.log_name,
        "stage": progress.stage,
        "percent": progress.percent,
    }


def agent_settings(settings: Settings) -> Settings:
    """The node's own settings, with model servers listening where the control plane can reach them."""
    return settings.model_copy(update={"llama_host": settings.agent_model_host or settings.llama_host})


class NodeAgent:
    def __init__(
        self,
        settings: Settings,
        server: str | None = None,
        token: str | None = None,
        address: str | None = None,
        stats: StatsFn | None = None,
    ):
        self.settings = agent_settings(settings)
        self.server = (server or settings.agent_server).rstrip("/")
        self.token = token or settings.agent_token
        self.address = address if address is not None else settings.agent_address
        self._stats = stats
        self.runtimes: dict[int, LlamaRuntime] = {}
        self.downloads = AgentDownloads(self.settings, self._emit_download)
        self.peer: Peer | None = None
        self.node_id: int | None = None
        self.node_name: str | None = None
        self.heartbeat = settings.node_heartbeat_seconds
        self.connected = asyncio.Event()
        self.status = "starting"  # connecting | connected | rejected | stopped
        self.last_error: str | None = None
        self.fatal_code: int | None = None
        self.protocol = p.PROTOCOL_VERSION  # overridable in tests (protocol mismatch)
        self.retry_seconds = 1.0  # first reconnect delay; doubles up to MAX_BACKOFF
        self._pending: deque[tuple[str, dict]] = deque(maxlen=MAX_PENDING_EVENTS)
        self._sent: dict[int, dict] = {}
        self._cpu = system.CpuMeter()
        self._stop = asyncio.Event()

    # ---- connection ---------------------------------------------------------

    @property
    def url(self) -> str:
        base = self.server
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base + p.AGENT_PATH

    async def run(self) -> None:
        """Connects, serves, and reconnects with backoff until ``stop`` is called."""
        if not self.server or not self.token:
            raise SystemExit("slotserve agent needs --server and --token (or SLOTSERVE_AGENT_SERVER / _TOKEN)")
        backoff = self.retry_seconds
        try:
            while not self._stop.is_set():
                self.status = "connecting"
                try:
                    await self._session()
                    backoff = self.retry_seconds
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - every failure means "try again later"
                    if self.fatal_code is None:
                        self.last_error = f"{exc.__class__.__name__}: {exc}"
                        log.warning("connection to %s failed: %s", self.server, self.last_error)
                finally:
                    self.connected.clear()
                if self._stop.is_set():
                    break
                if self.fatal_code is not None:
                    delay = FATAL_BACKOFF
                    if self.fatal_code == p.CLOSE_REVOKED:
                        await self.stop_runtimes()  # nobody will reconcile them any more
                else:
                    delay = max(backoff, self.retry_seconds)
                    backoff = min(MAX_BACKOFF, delay * 2)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay * random.uniform(0.8, 1.2))
        finally:
            self.status = "stopped"

    async def _session(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        headers = {"Authorization": f"Bearer {self.token}"}
        async with connect(self.url, additional_headers=headers, max_size=32 * 1024 * 1024, open_timeout=15) as ws:
            await ws.send(p.encode(await self._hello()))
            reply = p.decode(await asyncio.wait_for(ws.recv(), 30))
            if reply.get("type") == "error":
                self._rejected(reply.get("code", "error"), reply.get("message", ""))
                return
            if reply.get("type") != "welcome":
                raise p.ProtocolError("bad_frame", f"expected welcome, got {reply.get('type')!r}")
            if not p.compatible(reply.get("protocol")):
                self._rejected("protocol_mismatch", p.mismatch_message(reply.get("protocol"), "control plane"))
                return
            self.fatal_code = None
            self.last_error = None
            self.node_id = reply.get("node_id")
            self.node_name = reply.get("name")
            self.heartbeat = float(reply.get("heartbeat_seconds") or self.heartbeat)
            self.peer = Peer(ws.send, handlers=self._handlers())
            await self._sync_lanes(reply.get("lanes") or [])
            self.status = "connected"
            self.connected.set()
            log.info("connected to %s as node %s", self.server, self.node_name)
            while self._pending:
                name, data = self._pending.popleft()
                await self.peer.emit(name, data)
            loops = [
                asyncio.create_task(self._metrics_loop(), name="agent-metrics"),
                asyncio.create_task(self._runtime_loop(), name="agent-runtime"),
            ]
            try:
                async for text in ws:
                    try:
                        message = p.decode(text)
                    except p.ProtocolError as exc:
                        log.warning("bad frame from the control plane: %s", exc)
                        continue
                    if message.get("type") == "error":
                        self._rejected(message.get("code", "error"), message.get("message", ""))
                        break
                    await self.peer.handle(message)
            except ConnectionClosed:
                pass
            finally:
                for task in loops:
                    task.cancel()
                for task in loops:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                peer, self.peer = self.peer, None
                if peer is not None:
                    await peer.close()
            code = ws.close_code
            if code in p.FATAL_CLOSE_CODES and self.fatal_code is None:
                self._rejected(
                    {p.CLOSE_REVOKED: "revoked", p.CLOSE_PROTOCOL: "protocol_mismatch"}.get(code, "invalid_token"),
                    ws.close_reason or "",
                )
            log.info("disconnected from %s (code %s)", self.server, code)

    def _rejected(self, code: str, message: str) -> None:
        self.fatal_code = {
            "revoked": p.CLOSE_REVOKED,
            "protocol_mismatch": p.CLOSE_PROTOCOL,
            "invalid_token": p.CLOSE_INVALID_TOKEN,
            "token_rotated": p.CLOSE_INVALID_TOKEN,
        }.get(code, p.CLOSE_INVALID_TOKEN)
        self.status = "rejected"
        self.last_error = f"{code}: {message}" if message else code
        log.error("the control plane refused this node: %s", self.last_error)

    async def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        """Agent shutdown: stops every model server and download it started."""
        await self.stop()
        await self.downloads.close()
        await self.stop_runtimes()

    async def stop_runtimes(self) -> None:
        for lane_id in list(self.runtimes):
            rt = self.runtimes.pop(lane_id)
            with contextlib.suppress(Exception):
                await rt.shutdown()
        self._sent.clear()

    # ---- what the machine has -------------------------------------------------

    async def gpu_stats(self) -> list[GpuOut]:
        if self._stats is not None:
            return await self._stats()
        return await gpu_module.gpu_stats()

    async def inventory(self) -> dict:
        files = await asyncio.to_thread(scanner.find_files, Path(self.settings.models_dir))
        models_dir = Path(self.settings.models_dir)
        try:
            free = shutil.disk_usage(models_dir).free if models_dir.exists() else None
        except OSError:
            free = None
        return {
            "gpus": [g.model_dump() for g in await self.gpu_stats()],
            "models_dir": str(models_dir.resolve()),
            "models_free_bytes": free,
            "models": [{"path": f.path, "name": f.name, "size_bytes": f.size_bytes} for f in files],
            "engines": {e.id: " ".join(e.base_command(self.settings)) for e in ENGINES.values() if e.id != "custom"},
            "llama_port": self.settings.llama_port,
        }

    async def _hello(self) -> dict:
        return {
            "type": "hello",
            "protocol": self.protocol,
            "agent_version": __version__,
            "address": self.address,
            "info": system.machine_info(
                self.settings.agent_wol_mac, self.settings.agent_network, self.settings.agent_host_os
            ),
            "inventory": await self.inventory(),
            "runtimes": [runtime_report(rt) for rt in self.runtimes.values()],
        }

    async def _metrics(self) -> dict:
        used, total = system.memory_mb()
        load = None
        if hasattr(os, "getloadavg"):
            with contextlib.suppress(OSError):
                load = os.getloadavg()[0]
        return {
            "gpus": [g.model_dump() for g in await self.gpu_stats()],
            "cpu_percent": self._cpu.percent(),
            "memory_used_mb": used,
            "memory_total_mb": total,
            "load": load,
        }

    async def _metrics_loop(self) -> None:
        """Live metrics; they double as the heartbeat."""
        while True:
            try:
                await self._send_event(p.E_METRICS, await self._metrics())
            except p.RemoteError:
                return
            await asyncio.sleep(self.heartbeat)

    async def _runtime_loop(self) -> None:
        """Pushes every change of a lane's state (and loading progress) as it happens."""
        ticks = 0
        while True:
            ticks += 1
            for rt in list(self.runtimes.values()):
                report = runtime_report(rt)
                if report != self._sent.get(rt.lane_id) or ticks % 20 == 0:
                    self._sent[rt.lane_id] = report
                    try:
                        await self._send_event(p.E_RUNTIME, report)
                    except p.RemoteError:
                        return
            await asyncio.sleep(REPORT_SECONDS)

    async def _send_event(self, name: str, data: dict) -> None:
        if self.peer is None:
            raise p.RemoteError("disconnected", "not connected")
        await self.peer.emit(name, data)

    async def _record_load(self, fields: dict) -> None:
        data = {k: _iso(v) if hasattr(v, "isoformat") else v for k, v in fields.items()}
        if self.peer is not None and not self.peer.closed:
            with contextlib.suppress(p.RemoteError):
                await self.peer.emit(p.E_LOAD_EVENT, data)
                return
        self._pending.append((p.E_LOAD_EVENT, data))  # sent after the next reconnect

    async def _emit_download(self, data: dict) -> None:
        if self.peer is not None and not self.peer.closed:
            with contextlib.suppress(p.RemoteError):
                await self.peer.emit(p.E_DOWNLOAD, data)
                return
        if data.get("status") in ("done", "failed", "cancelled"):
            self._pending.append((p.E_DOWNLOAD, data))

    # ---- commands -------------------------------------------------------------

    def _handlers(self) -> dict[str, Callable[[dict], Awaitable[Any]]]:
        return {
            p.M_PING: self._ping,
            p.M_RUNTIME_ENSURE: self._ensure,
            p.M_RUNTIME_REMOVE: self._remove,
            p.M_RUNTIME_LIST: self._list,
            p.M_LANES_SYNC: self._lanes_sync,
            p.M_LOG_READ: self._log_read,
            p.M_LOG_TAIL: self._log_tail,
            p.M_MODELS_SCAN: self._scan,
            p.M_MODELS_STAT: self._stat,
            p.M_MODELS_DELETE: self._delete_model,
            p.M_DOWNLOAD_START: self.downloads.start,
            p.M_DOWNLOAD_CANCEL: self.downloads.cancel,
            p.M_WOL_SEND: self._wol_send,
        }

    async def _wol_send(self, params: dict) -> dict:
        """Wakes another machine of this network: magic packets to its broadcast addresses."""
        macs = [str(m) for m in params.get("macs") or []]
        broadcasts = [str(b) for b in params.get("broadcasts") or []] or ["255.255.255.255"]
        port = int(params.get("port") or 9)
        sent = await asyncio.to_thread(system.send_magic, macs, broadcasts, port)
        log.info("sent %s Wake-on-LAN packet(s) for %s", sent, ", ".join(macs))
        return {"sent": sent}

    async def _ping(self, _params: dict) -> dict:
        return {"version": __version__, "protocol": p.PROTOCOL_VERSION}

    def _runtime(self, lane_id: int, devices: list[int] | None = None, create: bool = True) -> LlamaRuntime | None:
        rt = self.runtimes.get(lane_id)
        if rt is None and create:
            rt = LlamaRuntime(self.settings, lane_id=lane_id, devices=devices, recorder=self._record_load)
            self.runtimes[lane_id] = rt
        return rt

    async def _ensure(self, params: dict) -> dict:
        lane_id = int(params["lane_id"])
        devices = params.get("devices")
        rt = self._runtime(lane_id, devices)
        force = bool(params.get("force"))
        if rt.devices != devices:
            rt.devices = devices
            force = True  # the GPUs changed: restart on the new ones
        model = params.get("model")
        preset = params.get("preset")
        if model is not None:
            model = SimpleNamespace(
                id=model["id"],
                name=model["name"],
                file_path=model["path"],
                engine=model.get("engine"),
                args=model.get("args") or "",
            )
        if preset is not None:
            preset = SimpleNamespace(id=preset["id"], name=preset["name"], args=preset.get("args") or "")
        await rt.ensure(model, float(params.get("est_load_seconds") or 0.0), force=force, preset=preset)
        report = runtime_report(rt)
        self._sent[lane_id] = report
        return report

    async def _remove(self, params: dict) -> dict:
        rt = self.runtimes.pop(int(params["lane_id"]), None)
        self._sent.pop(int(params["lane_id"]), None)
        if rt is not None:
            await rt.shutdown()
        return {"removed": rt is not None}

    async def _list(self, _params: dict) -> dict:
        return {"runtimes": [runtime_report(rt) for rt in self.runtimes.values()]}

    async def _sync_lanes(self, lanes: list[dict]) -> list[int]:
        """Stops model servers of lanes this node no longer has, and ones whose GPUs changed meanwhile."""
        wanted = {int(lane["lane_id"]): lane.get("devices") for lane in lanes}
        removed = []
        for lane_id in list(self.runtimes):
            if lane_id not in wanted:
                rt = self.runtimes.pop(lane_id)
                self._sent.pop(lane_id, None)
                log.info("lane %s no longer belongs to this node; stopping its model server", lane_id)
                await rt.shutdown()
                removed.append(lane_id)
            elif self.runtimes[lane_id].devices != wanted[lane_id]:
                rt = self.runtimes[lane_id]
                rt.devices = wanted[lane_id]
                await rt.ensure(None)
        return removed

    async def _lanes_sync(self, params: dict) -> dict:
        return {"removed": await self._sync_lanes(params.get("lanes") or [])}

    async def _log_read(self, params: dict) -> dict:
        rt = self._runtime(int(params["lane_id"]), create=False)
        if rt is None:
            return {"size": 0, "data": ""}
        start, end = int(params.get("start") or 0), int(params.get("end") or 0)
        data = await rt.log_read(start, end) if end > start else b""
        return {"size": await rt.log_size(), "data": p.b64(data)}

    async def _log_tail(self, params: dict) -> dict:
        rt = self._runtime(int(params["lane_id"]), create=False)
        return {"lines": await rt.log_tail(int(params.get("lines") or 200)) if rt is not None else []}

    async def _scan(self, _params: dict) -> dict:
        inv = await self.inventory()
        with contextlib.suppress(p.RemoteError):
            await self._send_event(p.E_INVENTORY, inv)
        return {"files": inv["models"]}

    async def _stat(self, params: dict) -> dict:
        path = Path(str(params.get("path") or ""))
        engine = params.get("engine") or "llama.cpp"
        if path.is_file():
            size = scanner.total_size(path) if path.name.lower().endswith(".gguf") else path.stat().st_size
            return {"exists": True, "path": str(path.resolve()), "size_bytes": size, "kind": "file"}
        if path.is_dir() and engine != "llama.cpp":
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return {"exists": True, "path": str(path.resolve()), "size_bytes": size, "kind": "dir"}
        return {"exists": False, "path": str(path), "size_bytes": 0, "kind": None}

    async def _delete_model(self, params: dict) -> dict:
        """Deletes a model file, only inside this node's models directory."""
        path = Path(str(params.get("path") or ""))
        root = Path(self.settings.models_dir).resolve()
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
            return {"deleted": True}
        return {"deleted": False}
