"""The control plane's side of clustering: node registry, agent connections, heartbeats and reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import __version__
from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import HOLDING_STATUSES, DownloadTask, Lane, ModelLoadEvent, Node, Reservation
from ..runtime.executor import NodeRegistry
from ..services.ratelimit import AttemptLimiter
from . import inventory
from . import protocol as p
from .relay import Relay
from .remote import RemoteExecutor
from .rpc import Peer

log = logging.getLogger(__name__)

WATCH_SECONDS = 2.0
LAST_SEEN_WRITE_SECONDS = 15.0
HELLO_TIMEOUT = 15.0
# Bookings that count as affected when their node goes offline: running now or starting within this window.
AFFECTED_WINDOW = timedelta(hours=24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return p.TOKEN_PREFIX + secrets.token_urlsafe(32)


def _agent_time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        from datetime import UTC

        moment = moment.replace(tzinfo=UTC)
    return moment


def _bearer(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


async def ensure_local_node(settings: Settings) -> Node:
    """The built-in node (this machine); created on first start."""
    async with SessionLocal() as db:
        node = (await db.execute(select(Node).where(Node.is_local.is_(True)).limit(1))).scalar_one_or_none()
        if node is None:
            name = settings.local_node_name or "local"
            taken = (await db.execute(select(Node.id).where(Node.name == name))).first()
            node = Node(name=name if taken is None else f"{name}-{secrets.token_hex(2)}", is_local=True)
            db.add(node)
            await db.commit()
        return node


class ClusterHub:
    """Keeps a ``RemoteExecutor`` per node, accepts agent connections and watches their heartbeats."""

    def __init__(self, app_state, registry: NodeRegistry):
        self.state = app_state
        self.settings: Settings = app_state.settings
        self.registry = registry
        self.relay = Relay(self.settings)  # every byte to and from nodes (relay.py)
        self.local_id: int | None = None
        self._offline_since: dict[int, float] = {}
        self._alerted: set[int] = set()
        self._last_written: dict[int, float] = {}
        self._closing = False
        # Downloads running on nodes: task id -> (node id, future resolved by the agent's "download" events).
        self.downloads: dict[int, tuple[int, asyncio.Future]] = {}
        # Failed agent logins (unknown or rotated tokens) per address.
        self.logins = AttemptLimiter()

    # ---- setup --------------------------------------------------------------

    async def start(self) -> None:
        local = await ensure_local_node(self.settings)
        self.local_id = local.id
        self.registry.local.node_id = local.id
        self.registry.local.name = local.name
        async with SessionLocal() as db:
            nodes = list((await db.execute(select(Node).where(Node.is_local.is_(False)))).scalars())
        for node in nodes:
            self.add_node(node)
            if node.last_seen_at is not None and node.revoked_at is None:
                self._offline_since[node.id] = time.monotonic()  # until it reconnects

    def add_node(self, node: Node) -> RemoteExecutor:
        executor = RemoteExecutor(self.settings, node.id, node.name, node.address or "")
        executor.relay = self.relay
        executor.seen_address = node.seen_address or ""
        if node.inventory:
            with contextlib.suppress(ValueError):
                data = json.loads(node.inventory)
                executor.inventory = data.get("inventory") or {}
                executor.info = data.get("info") or {}
                executor.reported_address = data.get("address") or ""
        self.registry.add(executor)
        return executor

    def executor(self, node_id: int | None) -> RemoteExecutor | None:
        found = self.registry.find(node_id)
        return found if isinstance(found, RemoteExecutor) else None

    async def disconnect(self, node_id: int, code: int, reason: str = "") -> None:
        executor = self.executor(node_id)
        if executor is None or executor.websocket is None:
            return
        if code in p.FATAL_CLOSE_CODES:  # tell the agent why, so it does not just reconnect
            executor.token_hash = None  # its relayed connections are refused from now on
            await self.relay.close_node(node_id)
            with contextlib.suppress(Exception):
                await executor.websocket.send_text(p.encode(p.error(reason or "disconnected", reason)))
        with contextlib.suppress(Exception):
            await executor.websocket.close(code)

    async def remove_node(self, node_id: int) -> None:
        await self.disconnect(node_id, p.CLOSE_REVOKED, "revoked")
        await self.relay.close_node(node_id)
        removed = self.registry.remove(node_id)
        if isinstance(removed, RemoteExecutor):
            await removed.close_clients()
        self._offline_since.pop(node_id, None)
        self._alerted.discard(node_id)

    async def close(self) -> None:
        """App shutdown: drop agent connections; model servers on nodes keep running and are reconciled later."""
        self._closing = True
        await self.relay.close()
        for executor in self.registry.all():
            if isinstance(executor, RemoteExecutor):
                if executor.peer is not None:
                    peer = executor.peer
                    executor.detach()
                    await peer.close()
                await executor.close_clients()

    # ---- agent connections --------------------------------------------------

    def _login_limit(self) -> int:
        return max(1, int(self.settings.node_login_attempts))

    def _throttled(self, ip: str) -> int | None:
        """Minutes until an address may try again after too many failed agent logins, or None."""
        q = self.logins._recent(f"agent:{ip}", time.monotonic())
        if len(q) < self._login_limit():
            return None
        wait = q[0] + self.logins.window - time.monotonic()
        return max(1, int(wait // 60) + 1)

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        ip = websocket.client.host if websocket.client else "?"
        minutes = self._throttled(ip)
        if minutes is not None:
            await self._reject(
                websocket,
                p.CLOSE_RATE_LIMITED,
                "rate_limited",
                f"too many failed node logins from {ip}; try again in {minutes} minute(s)",
            )
            return
        token = _bearer(websocket)
        async with SessionLocal() as db:
            node = (
                (await db.execute(select(Node).where(Node.token_hash == hash_token(token)))).scalar_one_or_none()
                if token
                else None
            )
        if node is None or node.is_local:
            self.logins._recent(f"agent:{ip}", time.monotonic()).append(time.monotonic())
            log.warning("refused a node agent from %s: unknown or rotated token", ip)
            await self._reject(websocket, p.CLOSE_INVALID_TOKEN, "invalid_token", "unknown or rotated node token")
            return
        if node.revoked_at is not None:
            await self._reject(websocket, p.CLOSE_REVOKED, "revoked", f"node {node.name!r} was revoked by an admin")
            return
        try:
            hello = p.decode(await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT))
        except (TimeoutError, WebSocketDisconnect, p.ProtocolError, RuntimeError):
            await self._reject(websocket, p.CLOSE_BAD_HELLO, "bad_hello", "expected a hello frame")
            return
        if hello.get("type") != "hello":
            await self._reject(websocket, p.CLOSE_BAD_HELLO, "bad_hello", "expected a hello frame")
            return
        version = hello.get("protocol")
        received = utcnow()
        if not p.compatible(version):
            message = p.mismatch_message(version, "agent", __version__)
            log.warning("node %s: %s", node.name, message)
            async with SessionLocal() as db:
                row = await db.get(Node, node.id)
                if row is not None:
                    row.last_error = message
                    row.protocol_version = version if isinstance(version, int) else None
                    row.agent_version = str(hello.get("agent_version") or "")[:32] or None
                    row.last_seen_at = utcnow()
                    await db.commit()
            await self._reject(websocket, p.CLOSE_PROTOCOL, "protocol_mismatch", message)
            return

        executor = self.executor(node.id) or self.add_node(node)
        if executor.online:  # the same agent reconnecting before the old socket noticed, or a second agent
            old_peer, old_ws = executor.peer, executor.websocket
            executor.detach()
            if old_peer is not None:
                await old_peer.close()
            if old_ws is not None:
                with contextlib.suppress(Exception):
                    await old_ws.close(p.CLOSE_REPLACED)

        peer = Peer(websocket.send_text, on_event=lambda name, data: self._on_event(executor, name, data))
        executor.attach(peer, websocket)
        executor.token_hash = node.token_hash
        executor.protocol = version
        executor.agent_version = str(hello.get("agent_version") or "")[:32]
        executor.info = hello.get("info") or {}
        executor.inventory = hello.get("inventory") or {}
        executor.reported_address = str(hello.get("address") or "")
        executor.seen_address = websocket.client.host if websocket.client else ""
        executor.connected_since = utcnow()
        executor.note_clock(_agent_time(hello.get("time")), received)
        # Reconciliation, part 1: what the node is actually running replaces what this side assumed.
        for report in hello.get("runtimes") or []:
            runtime = executor.runtimes.get(report.get("lane_id"))
            if runtime is not None:
                runtime.apply(report)

        async with SessionLocal() as db:
            row = await db.get(Node, node.id)
            lanes = list((await db.execute(select(Lane).where(Lane.node_id == node.id))).scalars())
            if row is not None:
                row.connected_at = row.last_seen_at = utcnow()
                row.agent_version = executor.agent_version or None
                row.protocol_version = version
                row.seen_address = executor.seen_address or None
                row.inventory = json.dumps(
                    {"info": executor.info, "inventory": executor.inventory, "address": executor.reported_address}
                )
                row.last_error = None
            await inventory.sync_node_files(db, node.id, executor.inventory.get("models") or [], self.local_id)
            wanted = list(
                (
                    await db.execute(
                        select(DownloadTask.id).where(
                            DownloadTask.node_id == node.id, DownloadTask.status.in_(("queued", "running"))
                        )
                    )
                ).scalars()
            )
            libraries = await self.library_specs(db, node.id)
            await db.commit()
        # Reconciliation, part 2: the agent stops whatever runs for lanes it no longer owns, and cancels
        # downloads that were cancelled or removed while it was away.
        welcome = {
            "type": "welcome",
            "protocol": p.PROTOCOL_VERSION,
            "node_id": node.id,
            "name": node.name,
            "heartbeat_seconds": self.settings.node_heartbeat_seconds,
            "lanes": [{"lane_id": lane.id, "devices": lane.devices} for lane in lanes],
            "server_version": __version__,
            "server_time": utcnow().isoformat(),
            "downloads": wanted,
            "libraries": libraries,
        }
        try:
            await websocket.send_text(p.encode(welcome))
        except Exception:  # noqa: BLE001 - the agent went away during the handshake
            executor.detach()
            await peer.close()
            return
        log.info("node %s connected from %s (agent %s)", node.name, executor.seen_address, executor.agent_version)
        if p.version_status(executor.agent_version, __version__) == "outdated":
            log.warning(
                "node %s runs agent %s, older than this server (%s): %s",
                node.name,
                executor.agent_version,
                __version__,
                p.update_commands(__version__)["compose"],
            )
        self._back_online(executor)
        self._poke_lanes(node.id)
        self._poke_downloads()  # downloads that waited for this node resume (HTTP Range) where they stopped

        try:
            while True:
                text = await websocket.receive_text()
                executor.last_seen = time.monotonic()
                executor.received_at = utcnow()
                try:
                    message = p.decode(text)
                except p.ProtocolError as exc:
                    log.warning("node %s sent a bad frame: %s", node.name, exc)
                    continue
                await peer.handle(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await peer.close()
            if executor.peer is peer:
                executor.detach()
                self._went_offline(executor)
                await self._write_last_seen(node.id)
                log.info("node %s disconnected", node.name)

    async def _reject(self, websocket: WebSocket, close_code: int, code: str, message: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.send_text(p.encode(p.error(code, message)))
        with contextlib.suppress(Exception):
            # the reason too: an agent may see the close before it reads the error frame
            await websocket.close(close_code, reason=message.encode()[:120].decode(errors="ignore"))

    # ---- events from agents ---------------------------------------------------

    async def _on_event(self, executor: RemoteExecutor, name: str, data: dict) -> None:
        if name == p.E_METRICS:
            executor.metrics = data
            executor.metrics_at = time.monotonic()
            executor.note_clock(_agent_time(data.get("time")), executor.received_at or utcnow())
        elif name == p.E_RUNTIME:
            runtime = executor.runtimes.get(data.get("lane_id"))
            if runtime is not None:
                runtime.apply(data)
        elif name == p.E_LOAD_EVENT:
            await self._record_load(executor, data)
        elif name == p.E_INVENTORY:
            executor.inventory = data
            async with SessionLocal() as db:
                row = await db.get(Node, executor.node_id)
                if row is not None:
                    row.inventory = json.dumps(
                        {"info": executor.info, "inventory": data, "address": executor.reported_address}
                    )
                await inventory.sync_node_files(db, executor.node_id, data.get("models") or [], self.local_id)
                await db.commit()
        elif name == p.E_DOWNLOAD:
            await self._on_download(executor, data)

    async def _record_load(self, executor: RemoteExecutor, data: dict) -> None:
        lane_id = data.get("lane_id")
        if lane_id not in executor.runtimes:
            return  # only lanes of this node

        # Recorded at the time it arrived (the node's clock may be off): the load took as long as the node says.
        finished = executor.received_at or utcnow()
        started_agent, finished_agent = _agent_time(data.get("started_at")), _agent_time(data.get("finished_at"))
        took = (finished_agent - started_agent) if started_agent and finished_agent else timedelta(0)
        started = finished - max(took, timedelta(0))

        async with SessionLocal() as db:
            db.add(
                ModelLoadEvent(
                    model_id=data.get("model_id"),
                    started_at=started,
                    finished_at=finished,
                    success=bool(data.get("success")),
                    error=data.get("error"),
                    lane_id=lane_id,
                    command=data.get("command"),
                    exit_code=data.get("exit_code"),
                    log_excerpt=data.get("log_excerpt"),
                )
            )
            await db.commit()

    # ---- liveness -------------------------------------------------------------

    def _poke_lanes(self, node_id: int) -> None:
        lanes = getattr(self.state, "lanes", None)
        if lanes is None:
            return
        for ctx in lanes.contexts.values():
            if ctx.node_id == node_id:
                ctx.scheduler.poke()

    def _went_offline(self, executor: RemoteExecutor) -> None:
        for node_id, future in list(self.downloads.values()):
            if node_id == executor.node_id and not future.done():
                future.set_exception(p.RemoteError("offline", f"node {executor.name!r} went offline"))
        if not self._closing:
            self._offline_since[executor.node_id] = time.monotonic()
            self._poke_lanes(executor.node_id)

    def _poke_downloads(self) -> None:
        downloader = getattr(self.state, "downloader", None)
        if downloader is not None:
            downloader.poke()

    def _back_online(self, executor: RemoteExecutor) -> None:
        self._offline_since.pop(executor.node_id, None)
        if executor.node_id in self._alerted:
            self._alerted.discard(executor.node_id)
            self._emit("node_online", node=executor.name)

    def _emit(self, event: str, **data: Any) -> None:
        notifier = getattr(self.state, "notifier", None)
        if notifier is not None:
            notifier.emit(event, **data)

    async def _write_last_seen(self, node_id: int) -> None:
        executor = self.executor(node_id)
        if executor is None:
            return
        self._last_written[node_id] = time.monotonic()
        async with SessionLocal() as db:
            row = await db.get(Node, node_id)
            if row is not None and executor.last_seen:
                row.last_seen_at = utcnow() - timedelta(seconds=time.monotonic() - executor.last_seen)
                await db.commit()

    async def check(self) -> None:
        """One watchdog round: silent agents are dropped, long-offline nodes are reported once."""
        now = time.monotonic()
        limit = self.settings.node_offline_seconds
        for executor in list(self.registry.all()):
            if not isinstance(executor, RemoteExecutor):
                continue
            if executor.online:
                if now - executor.last_seen > limit:
                    log.warning("node %s sent nothing for %.0fs; treating it as offline", executor.name, limit)
                    peer, websocket = executor.peer, executor.websocket
                    executor.detach()
                    self._went_offline(executor)
                    if peer is not None:
                        await peer.close()
                    if websocket is not None:
                        with contextlib.suppress(Exception):
                            await websocket.close(1001)
                elif now - self._last_written.get(executor.node_id, 0.0) > LAST_SEEN_WRITE_SECONDS:
                    await self._write_last_seen(executor.node_id)
                continue
            since = self._offline_since.get(executor.node_id)
            if since is not None and now - since >= limit and executor.node_id not in self._alerted:
                self._alerted.add(executor.node_id)
                await self._alert_offline(executor)

    async def _alert_offline(self, executor: RemoteExecutor) -> None:
        """Tells admins, and everyone whose booking runs on the node now or soon."""
        self._emit("node_offline", node=executor.name)
        now = utcnow()
        async with SessionLocal() as db:
            lane_ids = list((await db.execute(select(Lane.id).where(Lane.node_id == executor.node_id))).scalars())
            if not lane_ids:
                return
            rows = (
                await db.execute(
                    select(Reservation.id, Reservation.user_id).where(
                        Reservation.lane_id.in_(lane_ids),
                        Reservation.status.in_(HOLDING_STATUSES),
                        Reservation.end_at > now,
                        Reservation.start_at < now + AFFECTED_WINDOW,
                    )
                )
            ).all()
        for rid, uid in rows:
            self._emit("booking_node_offline", reservation_id=rid, user_id=uid, node=executor.name)

    # ---- downloads executed by nodes ----------------------------------------------

    async def run_download(
        self,
        node_id: int,
        task_id: int,
        url: str,
        filename: str,
        headers: dict,
        base: str | None = None,
        expected_bytes: int | None = None,
        existing_ok: bool = False,
    ) -> str:
        """Has the node download into its models directory (or ``base``); returns the file's path there.

        Raises ``RemoteError`` when the node is or goes offline (the task is then retried later),
        ``RuntimeError`` when the download failed and ``DownloadCancelled`` when it was cancelled.
        """
        from ..services.downloader import DownloadCancelled

        executor = self.executor(node_id)
        if executor is None:
            raise RuntimeError("the node no longer exists")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.downloads[task_id] = (node_id, future)
        params: dict[str, Any] = {"task_id": task_id, "url": url, "filename": filename, "headers": headers}
        if base:
            params["base"] = base
        if expected_bytes:
            params["expected_bytes"] = expected_bytes
        if existing_ok:
            params["existing_ok"] = True
        try:
            started = await executor.request(p.M_DOWNLOAD_START, params)
            # Finished while the connection was down: the agent remembers how it ended.
            result = started["result"] if isinstance(started, dict) and started.get("result") else await future
        finally:
            self.downloads.pop(task_id, None)
        if result.get("status") == "cancelled":
            raise DownloadCancelled
        if result.get("status") == "failed":
            raise RuntimeError(result.get("error") or "download failed on the node")
        return str(result.get("path") or "")

    async def cancel_download(self, task_id: int) -> None:
        entry = self.downloads.get(task_id)
        executor = self.executor(entry[0]) if entry else None
        if executor is not None and executor.online:
            with contextlib.suppress(p.RemoteError):
                await executor.request(p.M_DOWNLOAD_CANCEL, {"task_id": task_id})

    async def _on_download(self, executor: RemoteExecutor, data: dict) -> None:
        task_id = data.get("task_id")
        entry = self.downloads.get(task_id)
        if entry is None or entry[0] != executor.node_id:
            return  # not a download this node runs for us (e.g. reported after a restart)
        status = data.get("status")
        if status == "running":
            values = {k: data[k] for k in ("downloaded_bytes", "total_bytes", "speed_bps") if k in data}
            if values:
                async with SessionLocal() as db:
                    await db.execute(sa_update(DownloadTask).where(DownloadTask.id == task_id).values(**values))
                    await db.commit()
        elif status in ("done", "failed", "cancelled") and not entry[1].done():
            entry[1].set_result(data)

    async def deliver_token(self, node_id: int, token: str) -> bool:
        """Hands a new token to a connected agent, which stores it and uses it from then on."""
        executor = self.executor(node_id)
        if executor is None or not executor.online:
            return False
        try:
            result = await executor.request(p.M_TOKEN_ROTATE, {"token": token}, timeout=15)
        except p.RemoteError as exc:
            log.info("node %s could not take the new token in-band: %s", executor.name, exc)
            return False
        stored = bool(isinstance(result, dict) and result.get("stored"))
        if stored:
            executor.token_hash = hash_token(token)  # its relayed data connections now carry the new token
        return stored

    async def library_specs(self, db, node_id: int) -> list[dict]:
        """The model libraries this node uses (Admin -> Storage, "available on nodes")."""
        from ..services import storage as storage_service

        out = []
        for st in await storage_service.enabled_storages(db):
            if st.purpose == "models" and node_id in storage_service.library_nodes(st):
                out.append(storage_service.node_library_spec(st, self.settings.secret_key))
        return out

    async def library_dir(self, node_id: int, storage_id: int) -> str:
        """Where a model library is on a node (the agent mounts NFS / SMB libraries on first use)."""
        from ..orm import Storage
        from ..services import storage as storage_service

        executor = self.executor(node_id)
        if executor is None:
            raise RuntimeError("the node no longer exists")
        known = next(
            (
                lib.get("path")
                for lib in executor.inventory.get("libraries") or []
                if lib.get("storage_id") == storage_id and lib.get("path")
            ),
            None,
        )
        if known:
            return str(known)
        async with SessionLocal() as db:
            st = await db.get(Storage, storage_id)
            if st is None or st.purpose != "models" or node_id not in storage_service.library_nodes(st):
                raise RuntimeError("the model library is not available on this node")
            spec = storage_service.node_library_spec(st, self.settings.secret_key)
        try:
            result = await executor.request(p.M_LIBRARY_PREPARE, spec, timeout=180)
        except p.RemoteError as exc:
            if exc.code in ("offline", "disconnected", "timeout"):
                raise
            raise RuntimeError(f"the model library is not available on node {executor.name!r}: {exc.message}") from exc
        return str(result["path"])

    def offline_lanes(self) -> set[int]:
        return {
            lane_id
            for executor in self.registry.all()
            if isinstance(executor, RemoteExecutor) and not executor.online
            for lane_id in executor.runtimes
        }

    def token_hash_of(self, node_id: int) -> str | None:
        """The token hash of a node's connected agent (its relayed data connections must carry the same token)."""
        executor = self.executor(node_id)
        return executor.token_hash if executor is not None and executor.online else None

    async def serve_stream(self, websocket: WebSocket, stream_id: str) -> None:
        await self.relay.serve(websocket, stream_id, self.token_hash_of)

    async def run(self) -> None:
        while True:
            try:
                await self.check()
                await self.relay.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("node watchdog failed")
            await asyncio.sleep(WATCH_SECONDS)
