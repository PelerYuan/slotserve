"""Admin -> Nodes, and the WebSocket node agents connect to."""

from __future__ import annotations

import asyncio
import json
import shlex
import ssl
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import SessionLocal, get_db, utcnow
from ..deps import SESSION_USER_KEY, admin_user
from ..errors import ApiError
from ..orm import DownloadTask, Lane, LlmModel, Node, NodeModel, User
from ..runtime import gpu as gpu_module
from ..runtime.manager import RuntimeState
from ..schemas import GpuOut
from ..security import constant_time_equals
from ..services.audit import record
from . import protocol as p
from . import system
from .hub import ClusterHub, hash_token, new_token
from .install import AGENT_IMAGE, one_liners
from .remote import RemoteExecutor

router = APIRouter(tags=["nodes"])
_cpu = system.CpuMeter()
FINGERPRINT_CACHE_SECONDS = 600
_fingerprint_cache: dict[str, tuple[float, str | None, str | None]] = {}


class NodeLaneOut(BaseModel):
    id: int
    name: str
    gpus: list[int]


class NodeOut(BaseModel):
    id: int
    name: str
    is_local: bool
    # online | offline | never (no agent connected yet) | revoked | mismatch (protocol version)
    status: str
    address: str  # set by an admin ("" = automatic)
    effective_address: str  # what the control plane uses to reach model servers and containers
    token_prefix: str | None
    created_at: datetime
    connected_at: datetime | None
    last_seen_at: datetime | None
    revoked_at: datetime | None
    agent_version: str | None
    protocol_version: int | None
    last_error: str | None
    hostname: str | None = None
    os: str | None = None
    cpu_cores: int | None = None
    memory_total_mb: int | None = None
    memory_used_mb: int | None = None
    cpu_percent: float | None = None
    models_dir: str | None = None
    model_count: int = 0
    gpus: list[GpuOut] = []
    lanes: list[NodeLaneOut] = []
    # What kind of machine (services/sharing.py, cluster/system.py): e.g. "Windows · WSL2 (Docker Desktop)".
    platform: str | None = None
    host_os: str | None = None  # linux | windows | macos
    # Personal computers: owner, sharing hours and pause (see services/sharing.summary).
    owner_id: int | None = None
    owner_name: str | None = None
    sharing_schedule: list[dict] | None = None
    shared_now: bool = True
    sharing_paused: bool = False
    sharing_paused_until: datetime | None = None
    sharing_stops_at: datetime | None = None
    sharing_next_change: datetime | None = None
    # Wake-on-LAN
    network: str | None = None
    mac_addresses: str = ""  # entered by hand
    wol_macs: list[str] = []  # every MAC a magic packet goes to
    woken_at: datetime | None = None
    # The node's clock minus the server's, in seconds (None until it reported); samples and events always use
    # the server's clock, this only tells admins to fix the node's time sync.
    clock_skew_seconds: float | None = None
    clock_skew_warning: bool = False
    # current | outdated (older than the server: update it) | newer | unknown
    version_status: str = "unknown"
    update_commands: dict[str, str] = {}
    libraries: list[dict] = []  # model libraries on the node: storage_id, path, error
    downloads_queued: int = 0
    download_running: int | None = None  # task id
    on_exit: str | None = None  # what the agent does with model servers when it stops (stop | keep)


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    address: str = Field(default="", max_length=255)


class NodeUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    address: str | None = Field(default=None, max_length=255)
    owner_id: int | None = None  # a personal computer's owner (null: nobody)


class NodeTokenOut(BaseModel):
    node: NodeOut
    token: str  # shown once
    server_url: str
    docker_command: str
    agent_command: str
    install_linux: str = ""  # one-line installers (cluster/install.py)
    install_windows: str = ""
    # Rotation: the connected agent received the new token over its connection and keeps running with it.
    delivered: bool = False


class ServerLinkOut(BaseModel):
    """How agents reach this server, for Admin -> Nodes (pin the certificate on the nodes)."""

    server_url: str
    tls: bool
    fingerprint: str | None  # SHA-256 of the server certificate
    fingerprint_source: str | None  # certificate_file | public_url
    fingerprint_error: str | None = None
    version: str
    protocol: int


def get_cluster(request: Request) -> ClusterHub:
    return request.app.state.cluster


def _server_url(request: Request) -> str:
    return (get_settings().public_url or str(request.base_url)).rstrip("/")


def _cert_fingerprint(path: str) -> str:
    with open(path, encoding="ascii", errors="replace") as fh:
        pem = fh.read()
    start = pem.index("-----BEGIN CERTIFICATE-----")  # the first one is the server's own
    end = pem.index("-----END CERTIFICATE-----", start) + len("-----END CERTIFICATE-----")
    return p.fingerprint(ssl.PEM_cert_to_DER_cert(pem[start:end]))


def _url_fingerprint(url: str) -> str:
    parsed = urlparse(url)
    host, port = parsed.hostname or "", parsed.port or 443
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    import socket

    with socket.create_connection((host, port), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("no certificate")
    return p.fingerprint(der)


async def server_fingerprint(server_url: str) -> tuple[str | None, str | None, str | None]:
    """(fingerprint, source, error) of the certificate agents see: the configured certificate file, or the one
    presented at an https:// public URL (a reverse proxy), cached for a few minutes."""
    settings = get_settings()
    if settings.ssl_certfile:
        try:
            return _cert_fingerprint(settings.ssl_certfile), "certificate_file", None
        except (OSError, ValueError) as exc:
            return None, "certificate_file", str(exc)[:200]
    if not server_url.startswith("https://"):
        return None, None, None
    cached = _fingerprint_cache.get(server_url)
    if cached is not None and time.monotonic() - cached[0] < FINGERPRINT_CACHE_SECONDS:
        return cached[1], "public_url", cached[2]
    try:
        found, error = await asyncio.wait_for(asyncio.to_thread(_url_fingerprint, server_url), 8), None
    except (OSError, ValueError, TimeoutError) as exc:
        found, error = None, f"{exc.__class__.__name__}: {exc}"[:200]
    _fingerprint_cache[server_url] = (time.monotonic(), found, error)
    return found, "public_url", error


def _commands(server_url: str, token: str, name: str) -> tuple[str, str]:
    docker = " \\\n  ".join(
        [
            "docker run -d --name slotserve-agent --restart unless-stopped",
            "--gpus all --network host",
            "-v /path/to/llama.cpp:/opt/llama.cpp:ro",
            "-v /path/to/models:/models",
            "-v slotserve-agent:/data",
            "-v /var/run/docker.sock:/var/run/docker.sock",
            f"-e SLOTSERVE_AGENT_SERVER={shlex.quote(server_url)}",
            f"-e SLOTSERVE_AGENT_TOKEN={shlex.quote(token)}",
            f"{AGENT_IMAGE} slotserve agent",
        ]
    )
    agent = f"slotserve agent --server {shlex.quote(server_url)} --token {shlex.quote(token)}"
    return docker, agent


async def _local_out(node: Node, lanes: list[Lane], model_count: int, cluster: ClusterHub) -> NodeOut:
    used, total = system.memory_mb()
    settings = get_settings()
    return NodeOut(
        id=node.id,
        name=node.name,
        is_local=True,
        status="online",
        address="",
        effective_address=settings.llama_host,
        token_prefix=None,
        created_at=node.created_at,
        connected_at=None,
        last_seen_at=utcnow(),
        revoked_at=None,
        agent_version=__version__,
        protocol_version=p.PROTOCOL_VERSION,
        last_error=None,
        hostname=system.machine_info()["hostname"],
        os=system.machine_info()["os"],
        cpu_cores=system.machine_info()["cpu_cores"],
        memory_total_mb=total,
        memory_used_mb=used,
        cpu_percent=_cpu.percent(),
        models_dir=str(settings.models_dir.resolve()),
        model_count=model_count,
        gpus=await gpu_module.gpu_stats(),
        lanes=[NodeLaneOut(id=lane.id, name=lane.name, gpus=lane.devices or []) for lane in lanes],
        version_status="current",
        clock_skew_seconds=0.0,
    )


def _status(node: Node, executor: RemoteExecutor | None) -> str:
    if node.revoked_at is not None:
        return "revoked"
    if executor is not None and executor.online:
        return "online"
    if node.last_error and "protocol" in node.last_error:
        return "mismatch"
    return "offline" if node.last_seen_at is not None else "never"


def _remote_out(
    node: Node,
    lanes: list[Lane],
    model_count: int,
    executor: RemoteExecutor | None,
    downloads: dict[int, int] | None = None,
    running: int | None = None,
) -> NodeOut:
    data: dict = {}
    if node.inventory:
        try:
            data = json.loads(node.inventory)
        except ValueError:
            data = {}
    info = (executor.info if executor and executor.info else None) or data.get("info") or {}
    inv = (executor.inventory if executor and executor.inventory else None) or data.get("inventory") or {}
    metrics = executor.metrics if executor is not None and executor.online else {}
    last_seen = node.last_seen_at
    if executor is not None and executor.online and executor.last_seen:
        last_seen = utcnow() - timedelta(seconds=time.monotonic() - executor.last_seen)
    gpus = metrics.get("gpus") or inv.get("gpus") or []
    skew = executor.clock_skew if executor is not None and executor.online else None
    version = p.version_status(node.agent_version, __version__) if node.agent_version else "unknown"
    return NodeOut(
        id=node.id,
        name=node.name,
        is_local=False,
        status=_status(node, executor),
        address=node.address or "",
        effective_address=executor.address if executor is not None else (node.address or node.seen_address or ""),
        token_prefix=node.token_prefix,
        created_at=node.created_at,
        connected_at=node.connected_at,
        last_seen_at=last_seen,
        revoked_at=node.revoked_at,
        agent_version=node.agent_version,
        protocol_version=node.protocol_version,
        last_error=node.last_error,
        hostname=info.get("hostname"),
        os=info.get("os"),
        cpu_cores=info.get("cpu_cores"),
        memory_total_mb=metrics.get("memory_total_mb") or info.get("memory_total_mb"),
        memory_used_mb=metrics.get("memory_used_mb"),
        cpu_percent=metrics.get("cpu_percent"),
        models_dir=inv.get("models_dir"),
        model_count=model_count,
        gpus=[GpuOut(**g) for g in gpus],
        lanes=[NodeLaneOut(id=lane.id, name=lane.name, gpus=lane.devices or []) for lane in lanes],
        clock_skew_seconds=round(skew, 1) if skew is not None else None,
        clock_skew_warning=skew is not None and abs(skew) > get_settings().node_clock_skew_warn_seconds,
        version_status=version,
        update_commands=p.update_commands(__version__)
        if version == "outdated" or _status(node, executor) == "mismatch"
        else {},
        libraries=list(inv.get("libraries") or []),
        downloads_queued=(downloads or {}).get(node.id, 0),
        download_running=running,
        on_exit=inv.get("on_exit"),
    )


def _machine_fields(node: Node, executor: RemoteExecutor | None, owner_name: str | None) -> dict:
    """Platform, owner, sharing and Wake-on-LAN fields of a node."""
    from ..services import sharing

    info = sharing.local_info(get_settings()) if node.is_local else sharing._info(node, executor)
    plat = info.get("platform") or {}
    return {
        **sharing.summary(node, get_settings(), utcnow(), owner_name),
        "platform": plat.get("label"),
        "host_os": plat.get("host_os"),
        "wol_macs": sharing.macs_of(node, info),
    }


async def _out(db: AsyncSession, nodes: list[Node], cluster: ClusterHub) -> list[NodeOut]:
    out = await _base_out(db, nodes, cluster)
    names = dict((await db.execute(select(User.id, User.username))).all())
    return [
        o.model_copy(update=_machine_fields(n, cluster.executor(n.id), names.get(n.owner_id)))
        for o, n in zip(out, nodes, strict=True)
    ]


async def _base_out(db: AsyncSession, nodes: list[Node], cluster: ClusterHub) -> list[NodeOut]:
    lanes = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())
    counts = dict(
        (await db.execute(select(NodeModel.node_id, func.count(NodeModel.id)).group_by(NodeModel.node_id))).all()
    )
    queued = dict(
        (
            await db.execute(
                select(DownloadTask.node_id, func.count(DownloadTask.id))
                .where(DownloadTask.status == "queued", DownloadTask.node_id.is_not(None))
                .group_by(DownloadTask.node_id)
            )
        ).all()
    )
    active = getattr(getattr(cluster.state, "downloader", None), "active", {}) or {}
    out = []
    for node in nodes:
        mine = [lane for lane in lanes if (lane.node_id or cluster.local_id) == node.id]
        if node.is_local:
            local_models = (
                await db.execute(
                    select(func.count(LlmModel.id)).where(
                        LlmModel.deleted_at.is_(None),
                        (LlmModel.id.not_in(select(NodeModel.model_id)))
                        | LlmModel.id.in_(select(NodeModel.model_id).where(NodeModel.node_id == node.id)),
                    )
                )
            ).scalar_one()
            out.append(await _local_out(node, mine, local_models, cluster))
        else:
            out.append(
                _remote_out(node, mine, counts.get(node.id, 0), cluster.executor(node.id), queued, active.get(node.id))
            )
    return out


async def _node(db: AsyncSession, node_id: int) -> Node:
    node = await db.get(Node, node_id)
    if node is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return node


async def _name_free(db: AsyncSession, name: str, exclude: int | None = None) -> None:
    stmt = select(Node.id).where(func.lower(Node.name) == name.lower())
    if any(i != exclude for i in (await db.execute(stmt)).scalars()):
        raise ApiError(status.HTTP_409_CONFLICT, "node_name_taken")


@router.get("/api/admin/nodes", response_model=list[NodeOut])
async def list_nodes(
    _: User = Depends(admin_user), db: AsyncSession = Depends(get_db), cluster: ClusterHub = Depends(get_cluster)
):
    nodes = list((await db.execute(select(Node).order_by(Node.is_local.desc(), Node.name))).scalars())
    return await _out(db, nodes, cluster)


@router.get("/api/admin/nodes/server", response_model=ServerLinkOut)
async def server_link(request: Request, _: User = Depends(admin_user)):
    """The URL agents use and the server certificate's fingerprint (to pin with ``--fingerprint``)."""
    server = _server_url(request)
    fingerprint, source, error = await server_fingerprint(server)
    return ServerLinkOut(
        server_url=server,
        tls=server.startswith("https://") or bool(get_settings().ssl_certfile),
        fingerprint=fingerprint,
        fingerprint_source=source,
        fingerprint_error=error,
        version=__version__,
        protocol=p.PROTOCOL_VERSION,
    )


async def _issue(
    request: Request, db: AsyncSession, node: Node, cluster: ClusterHub, token: str | None = None, delivered=False
) -> NodeTokenOut:
    token = token or new_token()
    node.token_hash = hash_token(token)  # only the SHA-256 is stored; the token is shown once
    node.token_prefix = token[: len(p.TOKEN_PREFIX) + 4]
    await db.commit()
    server = _server_url(request)
    docker, agent = _commands(server, token, node.name)
    linux, windows = one_liners(server, token)
    return NodeTokenOut(
        node=(await _out(db, [node], cluster))[0],
        token=token,
        server_url=server,
        docker_command=docker,
        agent_command=agent,
        install_linux=linux,
        install_windows=windows,
        delivered=delivered,
    )


@router.post("/api/admin/nodes", response_model=NodeTokenOut, status_code=201)
async def create_node(
    body: NodeIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    cluster: ClusterHub = Depends(get_cluster),
):
    """Adds a machine: returns its agent token (shown once) and the command to start the agent there."""
    await _name_free(db, body.name)
    node = Node(name=body.name, address=body.address.strip())
    db.add(node)
    await db.flush()
    record(db, admin, "node.create", node.name)
    out = await _issue(request, db, node, cluster)
    cluster.add_node(node)
    return out


@router.patch("/api/admin/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: int,
    body: NodeUpdateIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    cluster: ClusterHub = Depends(get_cluster),
):
    node = await _node(db, node_id)
    if body.name is not None and body.name != node.name:
        await _name_free(db, body.name, node.id)
        node.name = body.name
    if body.address is not None and not node.is_local:
        node.address = body.address.strip()
    if "owner_id" in body.model_fields_set:
        if body.owner_id is not None and await db.get(User, body.owner_id) is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        node.owner_id = body.owner_id
    record(db, admin, "node.update", node.name, address=node.address, owner_id=node.owner_id)
    await db.commit()
    executor = cluster.executor(node.id)
    if executor is not None:
        executor.name = node.name
        executor.configured_address = node.address or ""
    elif node.is_local:
        cluster.registry.local.name = node.name
    return (await _out(db, [node], cluster))[0]


@router.post("/api/admin/nodes/{node_id}/token", response_model=NodeTokenOut)
async def rotate_token(
    node_id: int,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    cluster: ClusterHub = Depends(get_cluster),
):
    """A new token; the old one stops working at once. A connected agent receives the new one over its
    connection and keeps running (nothing to change on the machine); otherwise put the new token into the agent's
    configuration. Also re-admits a revoked node."""
    node = await _node(db, node_id)
    if node.is_local:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "node_is_local")
    token = new_token()
    delivered = node.revoked_at is None and await cluster.deliver_token(node.id, token)
    node.revoked_at = None
    node.last_error = None
    record(db, admin, "node.token", node.name, delivered=delivered)
    if not delivered:
        await cluster.disconnect(node.id, p.CLOSE_INVALID_TOKEN, "token_rotated")
    if cluster.executor(node.id) is None:
        cluster.add_node(node)
    return await _issue(request, db, node, cluster, token, delivered)


@router.post("/api/admin/nodes/{node_id}/revoke", response_model=NodeOut)
async def revoke_node(
    node_id: int,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    cluster: ClusterHub = Depends(get_cluster),
):
    """Disconnects the agent and refuses its token from now on (its lanes become unavailable)."""
    node = await _node(db, node_id)
    if node.is_local:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "node_is_local")
    node.revoked_at = utcnow()
    record(db, admin, "node.revoke", node.name)
    await db.commit()
    await cluster.disconnect(node.id, p.CLOSE_REVOKED, "revoked")
    return (await _out(db, [node], cluster))[0]


@router.delete("/api/admin/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: int,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    cluster: ClusterHub = Depends(get_cluster),
):
    node = await _node(db, node_id)
    if node.is_local:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "node_is_local")
    if (await db.execute(select(Lane.id).where(Lane.node_id == node.id).limit(1))).first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "node_has_lanes")
    active = await db.execute(
        select(DownloadTask.id).where(DownloadTask.node_id == node.id, DownloadTask.status.in_(("queued", "running")))
    )
    if active.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "node_has_downloads")
    record(db, admin, "node.delete", node.name)
    await db.delete(node)
    await db.commit()
    await cluster.remove_node(node_id)


@router.websocket(p.AGENT_PATH)
async def agent_socket(websocket: WebSocket):
    await websocket.app.state.cluster.serve(websocket)


@router.websocket(p.STREAM_PATH + "/{stream_id}")
async def stream_socket(websocket: WebSocket, stream_id: str):
    """The data connection of one relayed stream (cluster/relay.py), opened by the node's agent."""
    await websocket.app.state.cluster.serve_stream(websocket, stream_id)


# ---- health summary for monitoring ----------------------------------------------------------


class HealthNodeOut(BaseModel):
    name: str
    status: str
    version: str | None
    version_status: str
    clock_skew_seconds: float | None
    last_seen_at: datetime | None
    lanes: int


class HealthOut(BaseModel):
    status: str  # ok | degraded (a node offline, outdated or out of sync, a lane failed) | down (no lane serves)
    version: str
    problems: list[str]
    nodes: dict[str, int]  # by status
    lanes: dict[str, int]  # by runtime state
    downloads: dict[str, int]  # queued / running / failed (last 24 hours)
    node_list: list[HealthNodeOut]


async def _health_allowed(request: Request) -> bool:
    """The metrics token (Authorization: Bearer) or an admin session."""
    token = get_settings().metrics_token
    auth = request.headers.get("authorization", "")
    if token and auth.lower().startswith("bearer ") and constant_time_equals(auth[7:].strip(), token):
        return True
    user_id = request.session.get(SESSION_USER_KEY) if "session" in request.scope else None
    if user_id is None:
        return False
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
    return bool(user is not None and user.is_admin and user.disabled_at is None)


@router.get("/api/health", response_model=HealthOut)
async def health(request: Request, strict: bool = Query(default=False)):
    """One summary for monitoring: nodes, lanes, downloads, versions and clocks. ``strict=true`` answers 503
    unless everything is fine. Needs the metrics token or an admin session."""
    if not await _health_allowed(request):
        return JSONResponse(
            {"detail": {"code": "unauthorized"}}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
    cluster: ClusterHub = request.app.state.cluster
    problems: list[str] = []
    async with SessionLocal() as db:
        nodes = list((await db.execute(select(Node).order_by(Node.is_local.desc(), Node.name))).scalars())
        out = await _out(db, nodes, cluster)
        since = utcnow() - timedelta(hours=24)
        rows = (
            await db.execute(
                select(DownloadTask.status, func.count(DownloadTask.id))
                .where((DownloadTask.status.in_(("queued", "running"))) | (DownloadTask.finished_at >= since))
                .group_by(DownloadTask.status)
            )
        ).all()
    downloads = {"queued": 0, "running": 0, "failed": 0}
    for state, n in rows:
        if state in downloads:
            downloads[state] = n
    by_status: dict[str, int] = {}
    for n in out:
        by_status[n.status] = by_status.get(n.status, 0) + 1
        if n.status in ("offline", "mismatch") and n.lanes:
            problems.append(f"node {n.name} is {n.status}")
        if n.version_status == "outdated":
            problems.append(f"node {n.name} runs an older agent ({n.agent_version}); update it")
        if n.clock_skew_warning:
            problems.append(f"node {n.name}'s clock is {n.clock_skew_seconds:+.0f}s off")
    lanes: dict[str, int] = {}
    contexts = request.app.state.lanes.ordered()
    for ctx in contexts:
        state = ctx.runtime.snapshot().state
        lanes[state.value] = lanes.get(state.value, 0) + 1
        if state in (RuntimeState.FAILED, RuntimeState.OFFLINE):
            problems.append(f"lane {ctx.name} is {state.value}")
    if downloads["failed"]:
        problems.append(f"{downloads['failed']} download(s) failed in the last 24 hours")
    serving = sum(lanes.get(s.value, 0) for s in RuntimeState if s not in (RuntimeState.FAILED, RuntimeState.OFFLINE))
    level = "ok" if not problems else ("down" if contexts and not serving else "degraded")
    body = HealthOut(
        status=level,
        version=__version__,
        problems=problems,
        nodes=by_status,
        lanes=lanes,
        downloads=downloads,
        node_list=[
            HealthNodeOut(
                name=n.name,
                status=n.status,
                version=n.agent_version,
                version_status=n.version_status,
                clock_skew_seconds=n.clock_skew_seconds,
                last_seen_at=n.last_seen_at,
                lanes=len(n.lanes),
            )
            for n in out
        ],
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=503 if strict and level != "ok" else 200)
