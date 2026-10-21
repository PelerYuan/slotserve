"""The agent's side of the relay (see ``cluster/relay.py``): connects to targets on this machine when the control
plane asks, and carries the bytes over a WebSocket the agent opens to the control plane. Nothing listens here."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING

from ..cluster import protocol as p

if TYPE_CHECKING:
    from .node import NodeAgent

log = logging.getLogger("slotserve.agent.relay")

CHUNK = 64 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ANY = {"", "0.0.0.0", "::", "*"}
MAX_STREAMS = 512  # the control plane enforces its own (lower) limit per node


class AgentRelay:
    def __init__(self, agent: NodeAgent):
        self.agent = agent
        self.tasks: set[asyncio.Task] = set()
        self.bytes_in = 0  # control plane -> this machine
        self.bytes_out = 0

    @property
    def settings(self):
        return self.agent.settings

    @property
    def open_streams(self) -> int:
        return len(self.tasks)

    def url(self, stream_id: str) -> str:
        base = self.agent.server
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}{p.STREAM_PATH}/{stream_id}"

    async def open(self, params: dict) -> dict:
        """``stream.open``: connect to the target, then open the stream's data connection to the control plane."""
        from websockets.asyncio.client import connect

        if len(self.tasks) >= MAX_STREAMS:
            raise p.RemoteError("too_many_streams", "this node has too many relayed connections open")
        stream_id, secret = str(params.get("id") or ""), str(params.get("secret") or "")
        target = params.get("target") or {}
        if not stream_id or not secret:
            raise p.RemoteError("bad_request", "stream id and secret are required")
        reader, writer = await self.dial(target)
        headers = {"Authorization": f"Bearer {self.agent.token}", p.STREAM_SECRET_HEADER: secret}
        try:
            ws = await connect(
                self.url(stream_id), additional_headers=headers, max_size=None, open_timeout=15, compression=None
            )
        except Exception as exc:
            writer.close()
            raise p.RemoteError("relay_failed", f"could not connect back to the control plane: {exc}") from exc
        task = asyncio.create_task(self._pump(ws, reader, writer), name=f"relay {stream_id[:8]}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {}

    # ---- targets ----------------------------------------------------------------------------------

    async def dial(self, target: dict) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        kind = target.get("kind")
        try:
            if kind == "model":
                host = self.settings.llama_host
                return await asyncio.open_connection("127.0.0.1" if host in ANY else host, int(target["port"]))
            if kind == "tcp":
                host = str(target.get("host") or "127.0.0.1")
                if host not in LOOPBACK:
                    raise p.RemoteError("forbidden", "only loopback addresses can be relayed")
                return await asyncio.open_connection(host, int(target["port"]))
            if kind == "docker":
                return await open_docker(self.settings.docker_host)
            if kind == "container":
                host, port = await self.resolve_container(target)
                return await asyncio.open_connection(host, port)
        except p.RemoteError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise p.RemoteError("unreachable", f"{kind}: {exc or type(exc).__name__}") from exc
        raise p.RemoteError("bad_target", f"unknown relay target {kind!r}")

    async def resolve_container(self, target: dict) -> tuple[str, int]:
        """Where this machine reaches a container's port: a port Docker publishes on the host, else the container's
        address in its network (works where container networks are routable, i.e. Linux)."""
        docker = self.agent.docker()
        info = await docker.inspect(str(target["container"]))
        if info is None:
            raise p.RemoteError("not_found", f"container {target['container']} does not exist")
        port = int(target["port"])
        settings = info.get("NetworkSettings") or {}
        for binding in (settings.get("Ports") or {}).get(f"{port}/tcp") or []:
            if binding.get("HostPort"):
                host = binding.get("HostIp") or ""
                return ("127.0.0.1" if host in ANY else host), int(binding["HostPort"])
        networks = settings.get("Networks") or {}
        wanted = target.get("network")
        entry = networks.get(wanted) if wanted else None
        entry = entry or next(iter(networks.values()), None)
        ip = (entry or {}).get("IPAddress")
        if not ip:
            raise p.RemoteError("unreachable", f"container {target['container']} has no address")
        return ip, port

    # ---- moving bytes -------------------------------------------------------------------------------

    async def _pump(self, ws, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from websockets.exceptions import ConnectionClosed

        async def to_target() -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    self.bytes_in += len(message)
                    writer.write(message)
                    await writer.drain()
                elif _is_eof(message):
                    if writer.can_write_eof():
                        with contextlib.suppress(OSError, RuntimeError):
                            writer.write_eof()
                    return
            raise ConnectionResetError("closed")  # the control plane ended the stream

        async def from_target() -> None:
            while data := await reader.read(CHUNK):
                await ws.send(data)
                self.bytes_out += len(data)
            await ws.send(p.EOF_FRAME)

        tasks = [asyncio.create_task(to_target()), asyncio.create_task(from_target())]
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if any(not t.cancelled() and t.exception() is not None for t in done):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed, Exception):
                    await task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        for task in list(self.tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _is_eof(text: str) -> bool:
    try:
        return json.loads(text).get("t") == "eof"
    except (ValueError, AttributeError):
        return False


async def open_docker(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to the Docker API: ``unix://``, ``tcp://`` or (Windows) ``npipe://``."""
    if host.startswith("unix://"):
        return await asyncio.open_unix_connection(host[len("unix://") :])
    if host.startswith("tcp://"):
        rest = host[len("tcp://") :].split("/", 1)[0]
        name, _, port = rest.rpartition(":")
        return await asyncio.open_connection(name or rest, int(port or 2375))
    if host.startswith("npipe://") and os.name == "nt":
        path = host[len("npipe://") :].replace("/", "\\")
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=2**20)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_pipe_connection(lambda: protocol, path)  # type: ignore[attr-defined]
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
    raise OSError(f"unsupported Docker host {host!r}")
