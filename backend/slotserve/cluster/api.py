"""Admin -> Nodes, and the WebSocket node agents connect to."""

from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, WebSocket, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user
from ..errors import ApiError
from ..orm import DownloadTask, Lane, LlmModel, Node, NodeModel, User
from ..runtime import gpu as gpu_module
from ..schemas import GpuOut
from ..services.audit import record
from . import protocol as p
from . import system
from .hub import ClusterHub, hash_token, new_token
from .remote import RemoteExecutor

router = APIRouter(tags=["nodes"])
AGENT_IMAGE = "ghcr.io/peleryuan/slotserve:latest"
_cpu = system.CpuMeter()


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


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    address: str = Field(default="", max_length=255)


class NodeUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    address: str | None = Field(default=None, max_length=255)


class NodeTokenOut(BaseModel):
    node: NodeOut
    token: str  # shown once
    server_url: str
    docker_command: str
    agent_command: str


def get_cluster(request: Request) -> ClusterHub:
    return request.app.state.cluster


def _server_url(request: Request) -> str:
    return (get_settings().public_url or str(request.base_url)).rstrip("/")


def _commands(server_url: str, token: str, name: str) -> tuple[str, str]:
    docker = " \\\n  ".join(
        [
            "docker run -d --name slotserve-agent --restart unless-stopped",
            "--gpus all --network host",
            "-v /path/to/llama.cpp:/opt/llama.cpp:ro",
            "-v /path/to/models:/models",
            "-v slotserve-agent:/data",
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
    )


def _status(node: Node, executor: RemoteExecutor | None) -> str:
    if node.revoked_at is not None:
        return "revoked"
    if executor is not None and executor.online:
        return "online"
    if node.last_error and "protocol" in node.last_error:
        return "mismatch"
    return "offline" if node.last_seen_at is not None else "never"


def _remote_out(node: Node, lanes: list[Lane], model_count: int, executor: RemoteExecutor | None) -> NodeOut:
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
    )


async def _out(db: AsyncSession, nodes: list[Node], cluster: ClusterHub) -> list[NodeOut]:
    lanes = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())
    counts = dict(
        (await db.execute(select(NodeModel.node_id, func.count(NodeModel.id)).group_by(NodeModel.node_id))).all()
    )
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
            out.append(_remote_out(node, mine, counts.get(node.id, 0), cluster.executor(node.id)))
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


async def _issue(request: Request, db: AsyncSession, node: Node, cluster: ClusterHub) -> NodeTokenOut:
    token = new_token()
    node.token_hash = hash_token(token)
    node.token_prefix = token[: len(p.TOKEN_PREFIX) + 4]
    await db.commit()
    server = _server_url(request)
    docker, agent = _commands(server, token, node.name)
    return NodeTokenOut(
        node=(await _out(db, [node], cluster))[0],
        token=token,
        server_url=server,
        docker_command=docker,
        agent_command=agent,
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
    record(db, admin, "node.update", node.name, address=node.address)
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
    """A new token (the old one stops working at once); also re-admits a revoked node."""
    node = await _node(db, node_id)
    if node.is_local:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "node_is_local")
    node.revoked_at = None
    node.last_error = None
    record(db, admin, "node.token", node.name)
    await cluster.disconnect(node.id, p.CLOSE_INVALID_TOKEN, "token_rotated")
    if cluster.executor(node.id) is None:
        cluster.add_node(node)
    return await _issue(request, db, node, cluster)


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
