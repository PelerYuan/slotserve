"""Request/response and events over one WebSocket, shared by the control plane and the node agent."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from . import protocol as p

log = logging.getLogger(__name__)

Send = Callable[[str], Awaitable[None]]
Handler = Callable[[dict], Awaitable[Any]]
EventHandler = Callable[[str, dict], Awaitable[None]]

DEFAULT_TIMEOUT = 30.0


class Peer:
    """One side of a connection: sends requests and events, answers the other side's requests."""

    def __init__(self, send: Send, handlers: dict[str, Handler] | None = None, on_event: EventHandler | None = None):
        self._send = send
        self.handlers: dict[str, Handler] = handlers or {}
        self.on_event = on_event
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def send(self, message: dict) -> None:
        if self.closed:
            raise p.RemoteError("disconnected", "connection closed")
        async with self._send_lock:
            await self._send(p.encode(message))

    async def emit(self, event: str, data: dict) -> None:
        await self.send(p.event(event, data))

    async def request(self, method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        if self.closed:
            raise p.RemoteError("disconnected", "the node is not connected")
        message = p.request(method, params)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        try:
            await self.send(message)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise p.RemoteError("timeout", f"{method} got no answer within {timeout:.0f}s") from exc
        finally:
            self._pending.pop(message["id"], None)

    async def handle(self, message: dict) -> None:
        """Processes one received frame (``hello`` / ``welcome`` / ``error`` are handled by the caller)."""
        kind = message.get("type")
        if kind == "response":
            future = self._pending.get(message.get("id", ""))
            if future is None or future.done():
                return
            if message.get("ok"):
                future.set_result(message.get("result"))
            else:
                err = message.get("error") or {}
                future.set_exception(p.RemoteError(str(err.get("code", "error")), str(err.get("message", ""))))
        elif kind == "request":
            # Requests run concurrently so a slow one (a download, a model stop) does not block the others.
            task = asyncio.create_task(self._answer(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "event":
            if self.on_event is not None:
                try:
                    await self.on_event(str(message.get("event")), message.get("data") or {})
                except Exception:
                    log.exception("handling event %s failed", message.get("event"))

    async def _answer(self, message: dict) -> None:
        rid = str(message.get("id", ""))
        method = str(message.get("method", ""))
        handler = self.handlers.get(method)
        if handler is None:
            reply = p.response_error(rid, "unknown_method", f"unknown method {method!r}")
        else:
            try:
                reply = p.response_ok(rid, await handler(message.get("params") or {}))
            except p.RemoteError as exc:
                reply = p.response_error(rid, exc.code, exc.message)
            except Exception as exc:  # reported to the caller
                log.exception("%s failed", method)
                reply = p.response_error(rid, "failed", f"{exc.__class__.__name__}: {exc}")
        with contextlib.suppress(Exception):
            await self.send(reply)

    async def close(self) -> None:
        """Fails every pending request and stops running handlers."""
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(p.RemoteError("disconnected", "connection closed"))
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
