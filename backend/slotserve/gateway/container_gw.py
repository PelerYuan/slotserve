"""The container-side gateway (A13, F11): SlotServe's OpenAI-compatible ``/v1`` API - and nothing else - for the dev
environments, as ``http://slotserve-gw:<devenv_gateway_port>/v1``.

It is a third small app served by its own uvicorn server in SlotServe's process (like ``devenv_web_app.py``): the
same ``/v1`` routes, authentication (API keys and environment keys, ``security/credentials.py``), admission and
metering as on SlotServe's own port, but no web UI, no ``/api``, no sign-in. Every other path is 404.

Where it listens (``choose``):

* SlotServe runs on the host: on the address of Docker's default bridge (``docker0``, e.g. 172.17.0.1), which the
  containers reach and the network outside the machine does not; environments get ``slotserve-gw:<that address>``
  in ``/etc/hosts`` (``ExtraHosts``).
* SlotServe runs in a container: on all of its own addresses (only the Docker networks it is attached to reach
  them, nothing publishes the port); it joins each environment's network anyway (to reach the web interfaces) and
  does so with the alias ``slotserve-gw``.
* ``devenv_gateway_host`` set: there (``ExtraHosts`` points at it, or at Docker's ``host-gateway`` for a wildcard
  address).

Without a usable address (Docker is remote, the bridge is unknown and SlotServe only listens on loopback, or the
port cannot be bound) environments simply get no LLM endpoint and no key; nothing else changes.
"""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..containers.contributors.platform_env import GATEWAY_ALIAS
from . import devenv_web_app, proxy

log = logging.getLogger(__name__)
RETRY_SECONDS = 300.0  # after a failed bind


def create_gateway_app(main_state) -> FastAPI:
    """Only ``/v1``: the gateway's routes over SlotServe's state (lanes, settings, HTTP client)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state = main_state  # the routes read request.app.state.lanes / settings / http
    app.include_router(proxy.router)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def other(path: str):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    return app


@dataclass(frozen=True)
class Placement:
    bind: str  # address the listener binds
    extra_host: str | None  # /etc/hosts line of the environments ("slotserve-gw:<address>"); None: DNS alias


def _wildcard(host: str) -> bool:
    return host in ("", "0.0.0.0", "::")


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def choose(
    configured: str, server_host: str, in_container: bool, local_docker: bool, bridge_gateway: str | None
) -> Placement | None:
    """Where the gateway listens and how environments find it; None when they could not reach it."""
    if in_container:
        return Placement(configured or "0.0.0.0", None)
    if not local_docker:
        return None  # containers run on another machine: slotserve-gw on this one is out of their reach
    if configured:
        target = "host-gateway" if _wildcard(configured) else configured
        return Placement(configured, f"{GATEWAY_ALIAS}:{target}")
    if bridge_gateway:
        return Placement(bridge_gateway, f"{GATEWAY_ALIAS}:{bridge_gateway}")
    if _loopback(server_host):
        return None
    target = "host-gateway" if _wildcard(server_host) else server_host
    return Placement(server_host, f"{GATEWAY_ALIAS}:{target}")


class ContainerGateway:
    """The listener's life: started by the dev environment manager once Docker answers (``ensure``), stopped with
    SlotServe. ``serve=False`` (tests) only computes where it would be."""

    def __init__(self, main_state, serve: bool = True):
        self.state = main_state
        self.serve = serve
        self.placement: Placement | None = None
        self.app: FastAPI | None = None
        self._running: tuple[uvicorn.Server, asyncio.Task] | None = None
        self._tried_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def port(self) -> int:
        return int(self.state.settings.devenv_gateway_port or 0)

    @property
    def url(self) -> str:
        """The LLM endpoint inside environments, or "" while there is none."""
        if self.placement is None or not self.port:
            return ""
        return f"http://{GATEWAY_ALIAS}:{self.port}/v1"

    @property
    def extra_host(self) -> str | None:
        return self.placement.extra_host if self.placement else None

    async def ensure(self, docker, in_container: bool) -> None:
        """Starts the listener when it is not running yet (retried every few minutes after a failure)."""
        if self.placement is not None or not self.port:
            return
        loop = asyncio.get_running_loop()
        if self._tried_at is not None and loop.time() - self._tried_at < RETRY_SECONDS:
            return
        async with self._lock:
            if self.placement is not None:
                return
            self._tried_at = loop.time()
            settings = self.state.settings
            bridge = None
            local = settings.docker_host.startswith("unix://") or settings.docker_host.startswith("npipe://")
            if not in_container and local and not settings.devenv_gateway_host:
                bridge = await _bridge_gateway(docker)
            placement = choose(settings.devenv_gateway_host, settings.host, in_container, local, bridge)
            if placement is None:
                log.warning(
                    "container-side gateway: no address the environments could reach (Docker at %s); environments "
                    "get no LLM endpoint (set DEVENV_GATEWAY_HOST)",
                    settings.docker_host,
                )
                return
            if self.serve:
                if self.app is None:
                    self.app = create_gateway_app(self.state)
                self._running = await start(self.app, placement.bind, self.port)
                if self._running is None:
                    return
            self.placement = placement

    async def close(self) -> None:
        await devenv_web_app.stop(self._running)
        self._running = None
        self.placement = None


async def _bridge_gateway(docker) -> str | None:
    """The address of Docker's default bridge on this machine (``docker0``), or None."""
    try:
        info = await docker.network_inspect("bridge")
    except Exception as exc:  # noqa: BLE001 - whatever failed: no bridge address
        log.debug("docker network inspect bridge failed: %s", exc)
        return None
    for config in ((info or {}).get("IPAM") or {}).get("Config") or []:
        gateway = config.get("Gateway")
        if gateway:
            return str(gateway).split("/", 1)[0]
    return None


async def start(app: FastAPI, host: str, port: int) -> tuple[uvicorn.Server, asyncio.Task] | None:
    """Serves the gateway; logs and gives up when the address cannot be bound (SlotServe keeps running)."""
    try:
        sock = devenv_web_app._bind(host, port)
    except OSError as exc:
        log.error("container-side gateway: cannot listen on %s:%s (%s); no LLM endpoint", host, port, exc)
        return None
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="off",
        proxy_headers=False,  # nobody stands in front of it: the client address is the container's
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = devenv_web_app._Embedded(config)
    task = asyncio.create_task(server.serve(sockets=[sock]), name="container-gateway")
    for _ in range(100):
        if server.started or task.done():
            break
        await asyncio.sleep(0.02)
    log.info("container-side gateway (/v1 only) on %s:%s", host, port)
    return server, task
