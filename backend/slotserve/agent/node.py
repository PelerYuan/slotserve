"""The node agent (``slotserve agent``): runs a machine's lanes for the control plane.

It connects out to the control plane over a WebSocket with the node's token, reports the machine (GPUs, CPU,
memory, model files) and live metrics, and carries out commands: start / stop model servers (the same
``LlamaRuntime`` a single-machine SlotServe uses), read their logs, scan the models directory and download
models into it. Model servers keep running while the control plane is away; when the agent reconnects it reports
what runs and the control plane reconciles (lanes that no longer belong to the node are stopped).

Security of the link: ``https://`` server URLs connect over ``wss://`` and verify the server certificate against
the system CAs, a CA bundle (``agent_ca_bundle``) and / or a pinned SHA-256 fingerprint
(``agent_server_fingerprint``, checked before the token is sent). The control plane can rotate the token in-band;
the new one is stored in the data directory and used from then on.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import shutil
import ssl
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import __version__
from ..cluster import protocol as p
from ..cluster import system
from ..cluster.rpc import Peer
from ..config import Settings
from ..db import utcnow
from ..runtime import gpu as gpu_module
from ..runtime.engines import ENGINES
from ..runtime.manager import LlamaRuntime
from ..schemas import GpuOut
from ..services import scanner
from .downloads import AgentDownloads
from .libraries import AgentLibraries, LibraryError

log = logging.getLogger("slotserve.agent")

StatsFn = Callable[[], Awaitable[list[GpuOut]]]
REPORT_SECONDS = 0.5  # how often runtime state changes are looked for
FATAL_BACKOFF = 60.0
MAX_PENDING_EVENTS = 200
TOKEN_FILE = "agent-token.json"  # a token the control plane rotated in-band (see NodeAgent._stored_token)
STATE_FILE = "agent-runtimes.json"  # model servers kept running at exit (agent_on_exit=keep)


class CertificateMismatch(Exception):
    """The server's TLS certificate is not the pinned one: the token is never sent."""


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pinned_connection(expected: str):
    """A websockets client connection that checks the server certificate before the HTTP upgrade (and the token)."""
    from websockets.asyncio.client import ClientConnection

    class PinnedConnection(ClientConnection):
        async def handshake(self, *args, **kwargs):
            ssl_object = self.transport.get_extra_info("ssl_object") if self.transport else None
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
            seen = p.fingerprint(der) if der else "none"
            if seen != expected:
                raise CertificateMismatch(f"server certificate fingerprint {seen} is not the pinned {expected}")
            return await super().handshake(*args, **kwargs)

    return PinnedConnection


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
        self.configured_token = token or settings.agent_token
        self.token = self._stored_token(self.configured_token)
        self.address = address if address is not None else settings.agent_address
        self.fingerprint = p.normalize_fingerprint(settings.agent_server_fingerprint or "")  # ValueError when bad
        self.ca_bundle = settings.agent_ca_bundle or ""
        if self.fingerprint and not self.server.startswith(("https://", "wss://")):
            raise ValueError("a pinned certificate fingerprint needs an https:// server URL")
        self.on_exit = (settings.agent_on_exit or "stop").lower()
        self._stats = stats
        self.runtimes: dict[int, LlamaRuntime] = {}
        self._specs: dict[int, dict] = {}  # lane id -> the last runtime.ensure params (describes kept servers)
        self.downloads = AgentDownloads(self.settings, self._emit_download)
        self.libraries = AgentLibraries(self.settings)
        self.peer: Peer | None = None
        self.node_id: int | None = None
        self.node_name: str | None = None
        self.server_version: str | None = None
        self.heartbeat = settings.node_heartbeat_seconds
        self.connected = asyncio.Event()
        self.status = "starting"  # connecting | connected | rejected | throttled | stopped
        self.last_error: str | None = None
        self.fatal_code: int | None = None
        self.protocol = p.PROTOCOL_VERSION  # overridable in tests (protocol mismatch)
        # Reconnect delays: the first one, doubled after every failed attempt up to the maximum, +-25 % jitter.
        self.retry_seconds = settings.agent_retry_seconds
        self.max_retry_seconds = max(settings.agent_max_retry_seconds, settings.agent_retry_seconds)
        self.attempts = 0
        self._pending: deque[tuple[str, dict]] = deque(maxlen=MAX_PENDING_EVENTS)
        self._sent: dict[int, dict] = {}
        self._cpu = system.CpuMeter()
        self._stop = asyncio.Event()
        self._ws = None
        self._adopted = False
        self._background: set[asyncio.Task] = set()

    # ---- token ----------------------------------------------------------------

    @property
    def _token_file(self) -> Path:
        return Path(self.settings.data_dir) / TOKEN_FILE

    def _stored_token(self, configured: str) -> str:
        """A token rotated in-band replaces the configured one, as long as the configured one was not changed."""
        try:
            data = json.loads(self._token_file.read_text())
        except (OSError, ValueError):
            return configured
        if configured and data.get("origin") == _digest(configured) and data.get("token"):
            return str(data["token"])
        return configured

    async def _rotate_token(self, params: dict) -> dict:
        token = str(params.get("token") or "")
        if not token.startswith(p.TOKEN_PREFIX):
            raise ValueError("not a node token")
        path = self._token_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"origin": _digest(self.configured_token), "token": token}))
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(path)
        self.token = token
        log.info("the control plane rotated this node's token; the new one is stored in %s", path)
        return {"stored": True}

    # ---- connection ---------------------------------------------------------

    @property
    def url(self) -> str:
        base = self.server
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base + p.AGENT_PATH

    def ssl_context(self) -> ssl.SSLContext | None:
        """TLS settings for a wss:// server: system CAs, a CA bundle, and / or a pinned certificate."""
        if not self.url.startswith("wss://"):
            return None
        context = ssl.create_default_context(cafile=self.ca_bundle or None)
        if self.fingerprint and not self.ca_bundle:
            # Pinning alone (self-signed certificates): the fingerprint is the trust anchor.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def next_delay(self) -> float:
        """Exponential back-off with jitter, so many nodes do not reconnect in lock-step after a server restart."""
        base = min(self.max_retry_seconds, self.retry_seconds * (2 ** max(self.attempts - 1, 0)))
        return base * random.uniform(0.75, 1.25)

    async def run(self) -> None:
        """Connects, serves, and reconnects with back-off until ``stop`` is called."""
        if not self.server or not self.token:
            raise SystemExit("slotserve agent needs --server and --token (or SLOTSERVE_AGENT_SERVER / _TOKEN)")
        if self.url.startswith("ws://"):
            log.warning("the connection to %s is not encrypted; use https:// outside trusted networks", self.server)
        await self.adopt()
        try:
            while not self._stop.is_set():
                self.status = "connecting"
                accepted = False
                try:
                    accepted = await self._session()
                except asyncio.CancelledError:
                    raise
                except CertificateMismatch as exc:
                    self.fatal_code = p.CLOSE_INVALID_TOKEN
                    self.status = "rejected"
                    self.last_error = f"certificate_mismatch: {exc}"
                    log.error("refusing to connect: %s", exc)
                except Exception as exc:  # noqa: BLE001 - every failure means "try again later"
                    if self.fatal_code is None:
                        self.last_error = f"{exc.__class__.__name__}: {exc}"
                        log.warning("connection to %s failed: %s", self.server, self.last_error)
                finally:
                    self.connected.clear()
                if self._stop.is_set():
                    break
                self.attempts = 1 if accepted else self.attempts + 1
                if self.fatal_code is not None:
                    delay = max(FATAL_BACKOFF, self.retry_seconds) * random.uniform(0.75, 1.25)
                    if self.fatal_code == p.CLOSE_REVOKED:
                        await self.stop_runtimes()  # nobody will reconcile them any more
                else:
                    delay = self.next_delay()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay)
        finally:
            self.status = "stopped"

    async def _session(self) -> bool:
        """One connection; returns whether the control plane accepted it."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        options: dict[str, Any] = {
            "additional_headers": {"Authorization": f"Bearer {self.token}"},
            "max_size": 32 * 1024 * 1024,
            "open_timeout": 15,
        }
        context = self.ssl_context()
        if context is not None:
            options["ssl"] = context
        if self.fingerprint:
            options["create_connection"] = _pinned_connection(self.fingerprint)
        async with connect(self.url, **options) as ws:
            self._ws = ws
            await ws.send(p.encode(await self._hello()))
            reply = p.decode(await asyncio.wait_for(ws.recv(), 30))
            if reply.get("type") == "error":
                self._rejected(reply.get("code", "error"), reply.get("message", ""))
                return False
            if reply.get("type") != "welcome":
                raise p.ProtocolError("bad_frame", f"expected welcome, got {reply.get('type')!r}")
            if not p.compatible(reply.get("protocol")):
                self._rejected("protocol_mismatch", p.mismatch_message(reply.get("protocol"), "control plane"))
                return False
            self.fatal_code = None
            self.last_error = None
            self.node_id = reply.get("node_id")
            self.node_name = reply.get("name")
            self.server_version = reply.get("server_version")
            self.heartbeat = float(reply.get("heartbeat_seconds") or self.heartbeat)
            if self.server_version and p.version_status(__version__, self.server_version) == "outdated":
                log.warning(
                    "this agent (%s) is older than the control plane (%s); update it: %s",
                    __version__,
                    self.server_version,
                    p.update_commands(self.server_version)["compose"],
                )
            self.peer = Peer(ws.send, handlers=self._handlers())
            await self._sync_lanes(reply.get("lanes") or [])
            self.downloads.reconcile(reply.get("downloads"))
            if reply.get("libraries") is not None:
                self._spawn(self._sync_libraries(reply.get("libraries") or []))
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
                self._ws = None
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
                    {
                        p.CLOSE_REVOKED: "revoked",
                        p.CLOSE_PROTOCOL: "protocol_mismatch",
                        p.CLOSE_RATE_LIMITED: "rate_limited",
                    }.get(code, "invalid_token"),
                    ws.close_reason or "",
                )
            log.info("disconnected from %s (code %s)", self.server, code)
            return True

    def _rejected(self, code: str, message: str) -> None:
        self.fatal_code = {
            "revoked": p.CLOSE_REVOKED,
            "protocol_mismatch": p.CLOSE_PROTOCOL,
            "invalid_token": p.CLOSE_INVALID_TOKEN,
            "token_rotated": p.CLOSE_INVALID_TOKEN,
            "rate_limited": p.CLOSE_RATE_LIMITED,
        }.get(code, p.CLOSE_INVALID_TOKEN)
        self.status = "throttled" if code == "rate_limited" else "rejected"
        self.last_error = f"{code}: {message}" if message else code
        log.error("the control plane refused this node: %s", self.last_error)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def request_stop(self) -> None:
        """Leaves the connection loop (also from a signal handler); ``close`` then stops or keeps model servers."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            self._spawn(ws.close())

    async def stop(self) -> None:
        self.request_stop()

    async def close(self) -> None:
        """Agent shutdown: stops downloads, and stops the model servers or keeps them (``agent_on_exit``)."""
        self.request_stop()
        for task in list(self._background):
            task.cancel()
        await self.downloads.close()
        if self.on_exit == "keep":
            await self.keep_runtimes()
        else:
            await self.stop_runtimes()

    async def stop_runtimes(self) -> None:
        for lane_id in list(self.runtimes):
            rt = self.runtimes.pop(lane_id)
            with contextlib.suppress(Exception):
                await rt.shutdown()
        self._sent.clear()
        self._specs.clear()

    # ---- keeping model servers across agent restarts ----------------------------

    @property
    def _state_file(self) -> Path:
        return Path(self.settings.data_dir) / STATE_FILE

    async def keep_runtimes(self) -> list[int]:
        """Leaves ready model servers running and writes down what they are, for the next agent to take over."""
        kept = []
        for lane_id, rt in list(self.runtimes.items()):
            spec = self._specs.get(lane_id) or {}
            if rt.state.value != "ready" or not rt.pid or not spec.get("model"):
                continue
            kept.append(
                {
                    "lane_id": lane_id,
                    "devices": rt.devices,
                    "pid": rt.pid,
                    "port": rt.port,
                    "model": spec["model"],
                    "preset": spec.get("preset"),
                    "command": rt.command,
                    "load_started_at": _iso(rt.load_started_at),
                    "ready_at": _iso(rt.ready_at),
                }
            )
            rt.detach()
            self.runtimes.pop(lane_id)
        await self.stop_runtimes()  # whatever was not ready
        if kept:
            path = self._state_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"runtimes": kept}))
            log.info("left %s model server(s) running for the next agent", len(kept))
        return [k["lane_id"] for k in kept]

    async def adopt(self) -> list[int]:
        """Takes over model servers a previous agent process kept running (see ``keep_runtimes``)."""
        if self._adopted:
            return []
        self._adopted = True
        path = self._state_file
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        with contextlib.suppress(OSError):
            path.unlink()
        import httpx

        from ..runtime.adopted import pid_alive

        adopted = []
        async with httpx.AsyncClient(timeout=3.0) as client:
            for item in data.get("runtimes") or []:
                try:
                    lane_id, pid, model = int(item["lane_id"]), int(item["pid"]), dict(item["model"])
                except (KeyError, TypeError, ValueError):
                    continue
                engine = ENGINES.get(model.get("engine") or "llama.cpp") or ENGINES["llama.cpp"]
                url = f"http://127.0.0.1:{item.get('port')}{engine.health_path(self.settings)}"
                healthy = False
                if pid_alive(pid):
                    with contextlib.suppress(httpx.HTTPError):
                        healthy = (await client.get(url)).status_code == 200
                if not healthy:
                    log.info("the model server kept for lane %s (pid %s) is gone; not taking it over", lane_id, pid)
                    continue
                preset = item.get("preset")
                rt = self._runtime(lane_id, item.get("devices"))
                rt.adopt(
                    pid,
                    self._model_ns(model),
                    SimpleNamespace(id=preset["id"], name=preset["name"], args=preset.get("args") or "")
                    if preset
                    else None,
                    item.get("command"),
                    _parse_dt(item.get("load_started_at")),
                    _parse_dt(item.get("ready_at")),
                )
                self._specs[lane_id] = {"lane_id": lane_id, "model": model, "preset": preset}
                adopted.append(lane_id)
        return adopted

    # ---- what the machine has -------------------------------------------------

    async def gpu_stats(self) -> list[GpuOut]:
        if self._stats is not None:
            return await self._stats()
        return await gpu_module.gpu_stats()

    async def inventory(self) -> dict:
        files = await asyncio.to_thread(scanner.find_files, Path(self.settings.models_dir))
        for folder in list(self.libraries.dirs.values()):
            with contextlib.suppress(OSError):
                files += await asyncio.to_thread(scanner.find_files, Path(folder))
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
            "libraries": self.libraries.describe(),
            "on_exit": self.on_exit,
        }

    async def _hello(self) -> dict:
        return {
            "type": "hello",
            "protocol": self.protocol,
            "agent_version": __version__,
            "time": utcnow().isoformat(),
            "address": self.address,
            "info": system.machine_info(),
            "inventory": await self.inventory(),
            "runtimes": [runtime_report(rt) for rt in self.runtimes.values()],
            "downloads": self.downloads.snapshot(),
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
            "time": utcnow().isoformat(),  # for the control plane's clock skew check
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

    async def _sync_libraries(self, specs: list[dict]) -> None:
        await self.libraries.sync(specs)
        await self._send_inventory()

    async def _send_inventory(self) -> None:
        with contextlib.suppress(p.RemoteError):
            await self._send_event(p.E_INVENTORY, await self.inventory())

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
            p.M_DOWNLOAD_LIST: self.downloads.list,
            p.M_LIBRARY_PREPARE: self._prepare_library,
            p.M_TOKEN_ROTATE: self._rotate_token,
        }

    async def _ping(self, _params: dict) -> dict:
        return {"version": __version__, "protocol": p.PROTOCOL_VERSION, "time": utcnow().isoformat()}

    async def _prepare_library(self, params: dict) -> dict:
        try:
            result = await self.libraries.prepare(params)
        except LibraryError as exc:
            raise p.RemoteError("library_unavailable", str(exc)) from exc
        self._spawn(self._send_inventory())
        return result

    @staticmethod
    def _model_ns(model: dict) -> SimpleNamespace:
        return SimpleNamespace(
            id=model["id"],
            name=model["name"],
            file_path=model["path"],
            engine=model.get("engine"),
            args=model.get("args") or "",
        )

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
        self._specs[lane_id] = {"lane_id": lane_id, "model": model, "preset": preset}
        if preset is not None:
            preset = SimpleNamespace(id=preset["id"], name=preset["name"], args=preset.get("args") or "")
        await rt.ensure(
            self._model_ns(model) if model is not None else None,
            float(params.get("est_load_seconds") or 0.0),
            force=force,
            preset=preset,
        )
        report = runtime_report(rt)
        self._sent[lane_id] = report
        return report

    async def _remove(self, params: dict) -> dict:
        lane_id = int(params["lane_id"])
        rt = self.runtimes.pop(lane_id, None)
        self._sent.pop(lane_id, None)
        self._specs.pop(lane_id, None)
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
                self._specs.pop(lane_id, None)
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
            complete = (path / "config.json").is_file() and not any(path.glob("*.part"))
            return {
                "exists": True,
                "path": str(path.resolve()),
                "size_bytes": size,
                "kind": "dir",
                "complete": complete,
            }
        return {"exists": False, "path": str(path), "size_bytes": 0, "kind": None}

    async def _delete_model(self, params: dict) -> dict:
        """Deletes a model file, only inside this node's models directory."""
        path = Path(str(params.get("path") or ""))
        root = Path(self.settings.models_dir).resolve()
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
            return {"deleted": True}
        return {"deleted": False}
