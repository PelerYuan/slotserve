"""The relay: every byte between the control plane and a node travels over connections the node opens.

Nodes never accept connections: only the control plane has to be reachable. Model requests of the gateway, the
node's Docker API, SSH into dev environments, their direct ports and web interfaces are all *streams* through the
node's agent:

1. The control plane asks the agent (``stream.open`` over its control WebSocket) to open stream ``<id>`` to a
   target on the node (a model server port, the Docker socket, a container port).
2. The agent connects to the target, then opens a separate WebSocket to ``/api/cluster/stream/<id>``,
   authenticated with the node's token and the stream's one-time secret.
3. Bytes flow both ways over that WebSocket (binary frames). ``{"t": "eof"}`` is a half-close; closing the
   WebSocket ends the stream. Backpressure is TCP's: nobody reads faster than the other side writes.

The control connection only carries commands, so a large transfer never delays heartbeats or other commands.

``Relay`` keeps per-node limits (``relay_max_streams`` open at once), closes streams idle for
``relay_idle_seconds`` and counts bytes and streams per node (``RelayStats``). Ways to use a stream on the
control plane:

* ``Relay.transport(executor, resolve)``: an httpx transport (the gateway's model requests, the Docker API client);
* ``RelayListener``: a TCP listener whose connections are relayed to one target (SSH and direct ports on the
  control plane's port ranges; web interfaces on a loopback port the web proxy uses like any upstream);
* ``RelayStream`` itself: ``read`` / ``write`` / ``write_eof`` / ``close``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpcore
import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import protocol as p

if TYPE_CHECKING:
    from ..config import Settings
    from .remote import RemoteExecutor

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
HALF_CLOSE_SECONDS = 30.0  # after one direction ended, how long the other may go on in a TCP relay
Resolve = Callable[[str, int], dict]


class RelayError(Exception):
    """A stream could not be opened (node offline, too many streams, target unreachable, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class RelayStats:
    """What one node's relay carried (since the control plane started)."""

    open: int = 0  # streams open now
    opened: int = 0  # streams opened in total
    rejected: int = 0  # refused: the limit was reached
    failed: int = 0  # the node could not open them
    bytes_in: int = 0  # node -> control plane
    bytes_out: int = 0  # control plane -> node
    by_kind: dict[str, int] = field(default_factory=dict)  # open streams per target kind

    def out(self) -> dict:
        return {
            "open": self.open,
            "opened": self.opened,
            "rejected": self.rejected,
            "failed": self.failed,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "by_kind": dict(self.by_kind),
        }


class RelayStream:
    """One relayed connection on the control plane's side: a byte stream to a target on a node."""

    def __init__(self, relay: Relay, node_id: int, sid: str, secret: str, target: dict):
        self.relay = relay
        self.node_id = node_id
        self.id = sid
        self.secret = secret
        self.target = target
        self.kind = str(target.get("kind") or "?")
        self.websocket: WebSocket | None = None
        self.idle: float | None = None  # seconds without traffic before it is closed; None: relay_idle_seconds
        self.attached = asyncio.Event()
        self.closed = asyncio.Event()
        self.last_activity = time.monotonic()
        self._buffer = b""
        self._eof_in = False  # the node side sends nothing more
        self._eof_out = False  # this side sends nothing more
        self._send_lock = asyncio.Lock()
        self._counted = False

    @property
    def at_eof(self) -> bool:
        return (self._eof_in and not self._buffer) or self.closed.is_set()

    def attach(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.last_activity = time.monotonic()
        self.attached.set()

    async def read(self, max_bytes: int = CHUNK) -> bytes:
        """The next bytes from the node; ``b""`` once it sent EOF or the stream is closed."""
        if self._buffer:
            data, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
            return data
        while not self._eof_in and not self.closed.is_set() and self.websocket is not None:
            try:
                message = await self.websocket.receive()
            except (RuntimeError, WebSocketDisconnect, OSError):
                message = {"type": "websocket.disconnect"}
            if message["type"] == "websocket.disconnect":
                self._eof_in = True
                await self.close()
                break
            data = message.get("bytes")
            if data:
                self.last_activity = time.monotonic()
                self.relay.stats(self.node_id).bytes_in += len(data)
                if len(data) > max_bytes:
                    self._buffer = data[max_bytes:]
                    data = data[:max_bytes]
                return data
            text = message.get("text")
            if text is not None and _is_eof(text):
                self._eof_in = True
                if self._eof_out:
                    await self.close()
        return b""

    async def write(self, data: bytes) -> None:
        if not data:
            return
        if self.closed.is_set() or self._eof_out or self.websocket is None:
            raise ConnectionResetError("the relayed connection is closed")
        view = memoryview(data)
        try:
            async with self._send_lock:
                for i in range(0, len(view), CHUNK):
                    piece = bytes(view[i : i + CHUNK])
                    await self.websocket.send_bytes(piece)
                    self.relay.stats(self.node_id).bytes_out += len(piece)
        except (RuntimeError, WebSocketDisconnect, OSError) as exc:
            await self.close()
            raise ConnectionResetError("the relayed connection is closed") from exc
        self.last_activity = time.monotonic()

    async def write_eof(self) -> None:
        """Half-close: this side sends nothing more (the node shuts down its write side to the target)."""
        if self._eof_out or self.closed.is_set() or self.websocket is None:
            return
        self._eof_out = True
        try:
            async with self._send_lock:
                await self.websocket.send_text(p.EOF_FRAME)
        except (RuntimeError, WebSocketDisconnect, OSError):
            await self.close()
            return
        if self._eof_in:
            await self.close()

    async def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.relay._forget(self)
        if self.websocket is not None:
            with contextlib.suppress(Exception):
                await self.websocket.close()


def _is_eof(text: str) -> bool:
    try:
        return json.loads(text).get("t") == "eof"
    except (ValueError, AttributeError):
        return False


class Relay:
    """Opens streams to nodes and serves their data connections; limits, idle timeouts and counters per node."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pending: dict[str, RelayStream] = {}
        self._open: dict[str, RelayStream] = {}
        self._stats: dict[int, RelayStats] = {}

    def stats(self, node_id: int) -> RelayStats:
        found = self._stats.get(node_id)
        if found is None:
            found = self._stats[node_id] = RelayStats()
        return found

    def streams(self, node_id: int | None = None) -> list[RelayStream]:
        return [s for s in self._open.values() if node_id is None or s.node_id == node_id]

    async def open(
        self, executor: RemoteExecutor, target: dict, timeout: float | None = None, idle: float | None = None
    ) -> RelayStream:
        """A new stream to ``target`` on the executor's node (see ``protocol.M_STREAM_OPEN`` for targets).
        ``idle``: close it after this long without traffic (0: never; None: ``relay_idle_seconds``)."""
        if not executor.online:
            raise RelayError("offline", f"node {executor.name!r} is offline")
        stats = self.stats(executor.node_id)
        if stats.open >= max(1, self.settings.relay_max_streams):
            stats.rejected += 1
            raise RelayError("too_many_streams", f"node {executor.name!r} has {stats.open} relayed connections open")
        timeout = timeout or self.settings.relay_open_seconds
        stream = RelayStream(self, executor.node_id, secrets.token_hex(16), secrets.token_urlsafe(32), target)
        stream.idle = idle
        self._pending[stream.id] = stream
        stats.open += 1
        stats.by_kind[stream.kind] = stats.by_kind.get(stream.kind, 0) + 1
        stream._counted = True
        try:
            await executor.request(
                p.M_STREAM_OPEN, {"id": stream.id, "secret": stream.secret, "target": target}, timeout=timeout
            )
            await asyncio.wait_for(stream.attached.wait(), timeout)
        except p.RemoteError as exc:
            stats.failed += 1
            await stream.close()
            raise RelayError(exc.code, exc.message) from exc
        except TimeoutError as exc:
            stats.failed += 1
            await stream.close()
            raise RelayError("timeout", f"node {executor.name!r} did not open the connection in time") from exc
        except BaseException:
            await stream.close()
            raise
        finally:
            self._pending.pop(stream.id, None)
        stats.opened += 1
        self._open[stream.id] = stream
        return stream

    def _forget(self, stream: RelayStream) -> None:
        self._pending.pop(stream.id, None)
        self._open.pop(stream.id, None)
        if stream._counted:
            stream._counted = False
            stats = self.stats(stream.node_id)
            stats.open = max(0, stats.open - 1)
            left = stats.by_kind.get(stream.kind, 1) - 1
            if left > 0:
                stats.by_kind[stream.kind] = left
            else:
                stats.by_kind.pop(stream.kind, None)

    async def serve(self, websocket: WebSocket, stream_id: str, token_hash_of: Callable[[int], str | None]) -> None:
        """The data connection an agent opens for a stream (``/api/cluster/stream/<id>``)."""
        from .hub import hash_token

        stream = self._pending.get(stream_id)
        auth = websocket.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        secret = websocket.headers.get(p.STREAM_SECRET_HEADER, "")
        expected = token_hash_of(stream.node_id) if stream is not None else None
        if (
            stream is None
            or stream.attached.is_set()
            or not token
            or not expected
            or not hmac.compare_digest(hash_token(token), expected)
            or not hmac.compare_digest(secret, stream.secret)
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        stream.attach(websocket)
        await stream.closed.wait()

    async def close_node(self, node_id: int) -> None:
        """Ends every stream of a node (revoked, deleted)."""
        for stream in [*self._pending.values(), *self._open.values()]:
            if stream.node_id == node_id:
                await stream.close()

    async def close(self) -> None:
        for stream in [*self._pending.values(), *self._open.values()]:
            await stream.close()

    async def sweep(self) -> None:
        """Closes streams without traffic for ``relay_idle_seconds`` (or their own limit; 0 = never). Dead peers are
        noticed by the WebSockets' own keepalive pings, so long-lived quiet connections (SSH) need no idle limit."""
        default = self.settings.relay_idle_seconds
        now = time.monotonic()
        for stream in list(self._open.values()):
            limit = default if stream.idle is None else stream.idle
            if limit > 0 and now - stream.last_activity > limit:
                log.info("closing relayed connection %s to %s (idle)", stream.id[:8], stream.kind)
                await stream.close()

    # ---- httpx -----------------------------------------------------------------------------

    def transport(
        self, executor: RemoteExecutor, resolve: Resolve, keepalive: float = 5.0, connections: int = 20
    ) -> httpx.AsyncHTTPTransport:
        """An httpx transport whose connections are streams to the node; ``resolve(host, port)`` gives the target
        of a URL (e.g. ``{"kind": "docker"}``). Errors opening a stream surface as ``httpx.ConnectError``."""
        transport = httpx.AsyncHTTPTransport()
        transport._pool = httpcore.AsyncConnectionPool(
            max_connections=connections,
            max_keepalive_connections=min(connections, 8),
            keepalive_expiry=keepalive,
            network_backend=_Backend(self, executor, resolve),
        )
        return transport


class _NetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: RelayStream):
        self.stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(self.stream.read(max_bytes), timeout)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(str(exc) or "read timed out") from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(self.stream.write(buffer), timeout)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(str(exc) or "write timed out") from exc
        except ConnectionResetError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    async def aclose(self) -> None:
        await self.stream.close()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise httpcore.ConnectError("TLS is not supported through the relay")

    def get_extra_info(self, info: str) -> Any:
        if info == "is_readable":
            # an idle pooled connection whose stream ended must not be reused
            return self.stream.closed.is_set() or self.stream.at_eof
        return None


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, relay: Relay, executor: RemoteExecutor, resolve: Resolve):
        self.relay = relay
        self.executor = executor
        self.resolve = resolve

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            stream = await self.relay.open(self.executor, self.resolve(host, port), timeout)
        except RelayError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return _NetworkStream(stream)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not relayed")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# ---- TCP listeners -----------------------------------------------------------------------------


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stream: RelayStream) -> None:
    """Copies bytes between a local TCP connection and a stream until both sides are done (or one fails)."""

    async def up() -> None:
        while data := await reader.read(CHUNK):
            await stream.write(data)
        await stream.write_eof()

    async def down() -> None:
        while data := await stream.read(CHUNK):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            with contextlib.suppress(OSError, RuntimeError):
                writer.write_eof()

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        failed = any(t.exception() is not None for t in done if not t.cancelled())
        if pending and not failed and not stream.closed.is_set():
            # one direction is done (half-close); give the other a while to finish
            await asyncio.wait(pending, timeout=HALF_CLOSE_SECONDS)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await stream.close()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


Route = Callable[[], Awaitable[tuple["RemoteExecutor | None", dict]]]


def fixed_route(executor: Callable[[], RemoteExecutor | None], target: dict) -> Route:
    """A route to one target on one node (looked up when a connection comes in: the node may reconnect)."""

    async def route() -> tuple[RemoteExecutor | None, dict]:
        return executor(), target

    return route


class RelayListener:
    """A TCP port on the control plane whose connections are relayed to a node.

    The listener only accepts and pumps bytes; *where* a connection goes is decided by ``route`` (called for each
    connection), so a front that picks the target per connection (e.g. by name) needs no change here. ``key``
    identifies what the listener serves, for reconciling (by default: the target of a fixed route)."""

    def __init__(
        self,
        relay: Relay,
        route: Route,
        host: str,
        port: int,
        label: str = "",
        key: Any = None,
        idle: float | None = None,
    ):
        self.relay = relay
        self.route = route
        self.key = key
        self.idle = idle  # idle limit of its connections (see Relay.open)
        self.host = host
        self.port = port
        self.label = label or str(key)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def to(
        cls,
        relay: Relay,
        executor: Callable[[], RemoteExecutor | None],
        target: dict,
        host: str,
        port: int,
        label: str = "",
        idle: float | None = None,
    ) -> RelayListener:
        """A listener relaying every connection to one target."""
        return cls(relay, fixed_route(executor, target), host, port, label or str(target), key=target, idle=idle)

    async def start(self) -> None:
        """Starts listening (raises ``OSError`` when the port is taken); port 0 picks a free one."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock is not None:
            self.port = sock.getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            executor, target = await self.route()
            if executor is None:
                raise RelayError("offline", "the node is gone")
            stream = await self.relay.open(executor, target, idle=self.idle)
        except (RelayError, OSError) as exc:
            log.info("relay %s: %s", self.label, exc)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)
            return
        try:
            await pump(reader, writer, stream)
        finally:
            if task is not None:
                self._tasks.discard(task)

    @property
    def connections(self) -> int:
        return len(self._tasks)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
            self._server = None
