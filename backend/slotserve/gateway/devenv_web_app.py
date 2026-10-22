"""The web listener: dev environment web interfaces on their own port (``devenv_web_port``), so browsers keep them
apart from SlotServe (see ``devenv_web_auth.py``). It is a second, small app served by a second uvicorn server in
the same process; it shares SlotServe's state (database, dev environment manager)."""

import asyncio
import contextlib
import logging
import os
import socket

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..security import web_origin
from . import devenv_web

log = logging.getLogger(__name__)


def create_web_app(main_state) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.web_listener = True
    app.state.settings = main_state.settings
    app.state.devenv = main_state.devenv
    app.add_middleware(_BaseHeaders)
    app.include_router(devenv_web.enter_router)
    app.include_router(devenv_web.router)

    @app.api_route("/{path:path}", methods=devenv_web.METHODS, include_in_schema=False)
    async def other(path: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    return app


class _BaseHeaders:
    """Every response of the web listener (also sign-in redirects and errors) sends a Referer only to its own
    origin and keeps no opener relation across origins; the proxy sets the same on the apps' responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                for name, value in (
                    (b"referrer-policy", web_origin.REFERRER_POLICY),
                    (b"cross-origin-opener-policy", web_origin.OPENER_POLICY),
                ):
                    if name not in names:
                        headers.append((name, value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _Embedded(uvicorn.Server):
    """A uvicorn server inside another one's event loop: the main server handles the signals."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        pass


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # on Windows this would let two servers share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


async def start(app: FastAPI, host: str, port: int) -> tuple[uvicorn.Server, asyncio.Task] | None:
    """Serves the web listener; logs and gives up when the port cannot be bound (SlotServe keeps running)."""
    try:
        sock = _bind(host, port)
    except OSError as exc:
        log.error("dev environment web interfaces: cannot listen on %s:%s (%s); they will not open", host, port, exc)
        return None
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="off",
        proxy_headers=True,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = _Embedded(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="devenv-web")
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.02)
    log.info("dev environment web interfaces on port %s", port)
    return server, task


async def stop(running: tuple[uvicorn.Server, asyncio.Task] | None) -> None:
    if running is None:
        return
    server, task = running
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(task, 10)
