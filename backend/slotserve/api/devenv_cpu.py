"""Dev environments without a GPU: start, pause, limits and live use (see services/devenv_cpu.py)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..errors import ApiError
from ..orm import DevProject, DevSession, DevTemplate, Lane, User
from ..services.audit import record
from ..services.devenv import DevEnvError, visible
from ..services.devenv_cpu import ACTIVE_STATES, KIND, cpu_ranges
from .devenv import SessionOut, _devenv_error, _project, _session_out, node_name

router = APIRouter(prefix="/api/devenv/cpu", tags=["devenv"])


class StartIn(BaseModel):
    template_id: int | None = None
    project_id: int | None = None
    node_id: int | None = None  # the machine to run on; None: a kept environment's own machine, else the server's


class AfterBookingIn(BaseModel):
    after_booking: bool


class LiveOut(BaseModel):
    cpu: float  # cores in use
    memory: int  # bytes
    memory_limit: int | None
    at: datetime


class CpuSessionOut(BaseModel):
    id: int
    username: str | None
    mine: bool
    name: str
    project_id: int | None
    state: str
    started_at: datetime | None
    live: LiveOut | None
    node_id: int | None = None  # None: the server's own machine
    node_name: str | None = None


class NodeChoice(BaseModel):
    """A machine CPU-only environments can run on."""

    id: int | None  # None: the server's own machine
    name: str
    online: bool
    cpus: int | None
    memory: int | None  # bytes


class LaneCores(BaseModel):
    lane_id: int
    name: str
    cpus: int
    cpuset: str


class CpuInfo(BaseModel):
    enabled: bool
    per_user: int
    running: int  # mine, waiting to run or running
    cores: float  # per environment; 0 = the whole pool
    memory_gb: float  # per environment; 0 = no limit
    memory_pool_gb: float  # all CPU-only environments together; 0 = no limit
    memory_claimed_gb: float
    cores_per_gpu: int
    host_cpus: int | None
    host_memory: int | None
    pool_cpus: int | None
    pool: str | None
    lanes: list[LaneCores]
    after_booking: list[int]  # my projects that continue without a GPU when a booking ends
    sessions: list[CpuSessionOut]  # mine; every one for admins
    nodes: list[NodeChoice] = []  # machines to choose from (more than one only with other machines connected)


async def _start_target(db: AsyncSession, body: StartIn, user: User) -> tuple[DevTemplate | None, DevProject | None]:
    if (body.template_id is None) == (body.project_id is None):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "validation_error", field="template_id")
    if body.project_id is not None:
        project = await db.get(DevProject, body.project_id)
        if project is None or project.owner_id != user.id:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        return None, project
    template = await db.get(DevTemplate, body.template_id)
    if not visible(template, user):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return template, None


@router.get("", response_model=CpuInfo)
async def cpu_info(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    manager = request.app.state.devenv
    cpu = manager.cpu
    layout = await cpu.layout(db) if s.devenv_enabled else None
    lanes = {lane.id: lane.name for lane in (await db.execute(select(Lane))).scalars()}
    stmt = select(DevSession).where(DevSession.kind == KIND, DevSession.state.in_((*ACTIVE_STATES, "failed")))
    if not user.is_admin:
        stmt = stmt.where(DevSession.user_id == user.id)
    rows = list((await db.execute(stmt.order_by(DevSession.id))).scalars())
    names: dict[int, str] = {}
    if owners := {r.user_id for r in rows if r.user_id}:
        names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(owners)))).all())
    sessions = []
    for r in rows:
        project = await db.get(DevProject, r.project_id) if r.project_id else None
        template = await db.get(DevTemplate, r.template_id) if r.template_id else None
        live = cpu.live_out(r.id)
        sessions.append(
            CpuSessionOut(
                id=r.id,
                username=names.get(r.user_id),
                mine=r.user_id == user.id,
                name=project.name if project else template.name if template else r.image,
                project_id=r.project_id,
                state=r.state,
                started_at=r.started_at,
                live=LiveOut(**live) if live else None,
                node_id=manager.node_key(r.node_id),
                node_name=node_name(request, r.node_id),
            )
        )
    active_all = sum(1 for r in rows if r.state in ACTIVE_STATES) if user.is_admin else await cpu.active_count(db)
    after = (
        await db.execute(
            select(DevProject.id).where(DevProject.owner_id == user.id, DevProject.cpu_after_booking.is_(True))
        )
    ).scalars()
    return CpuInfo(
        enabled=cpu.enabled(),
        per_user=s.devenv_cpu_per_user,
        running=sum(1 for r in rows if r.user_id == user.id and r.state in ACTIVE_STATES),
        cores=s.devenv_cpu_cores,
        memory_gb=s.devenv_cpu_memory_gb,
        memory_pool_gb=s.devenv_cpu_memory_pool_gb,
        memory_claimed_gb=cpu.memory_claimed_gb(active_all),
        cores_per_gpu=s.devenv_cpu_cores_per_gpu,
        host_cpus=cpu.host["cpus"] if layout and cpu.host else None,
        host_memory=cpu.host["memory"] if layout and cpu.host else None,
        pool_cpus=len(layout.pool) if layout else None,
        pool=cpu_ranges(layout.pool) if layout and layout.pool else None,
        lanes=[
            LaneCores(lane_id=lane_id, name=lanes.get(lane_id, "?"), cpus=len(cores), cpuset=cpu_ranges(cores))
            for lane_id, cores in (layout.lanes.items() if layout else [])
            if cores
        ],
        after_booking=list(after),
        sessions=sessions,
        nodes=_node_choices(request, cpu) if s.devenv_enabled else [],
    )


def _node_choices(request: Request, cpu) -> list[NodeChoice]:
    """The server's own machine, and every other machine (its cores and memory as last known)."""
    nodes = request.app.state.nodes
    local = nodes.local
    out = [
        NodeChoice(
            id=None,
            name=local.name,
            online=True,
            cpus=cpu.host["cpus"] if cpu.host else None,
            memory=cpu.host["memory"] if cpu.host else None,
        )
    ]
    for executor in nodes.all():
        if executor.is_local:
            continue
        host = cpu.host_of(executor.node_id) or {}
        out.append(
            NodeChoice(
                id=executor.node_id,
                name=executor.name,
                online=executor.online,
                cpus=host.get("cpus") or executor.info.get("cpu_cores"),
                memory=host.get("memory")
                or (executor.info["memory_total_mb"] * 2**20 if executor.info.get("memory_total_mb") else None),
            )
        )
    return out


@router.post("/start", response_model=SessionOut, status_code=201)
async def start_cpu(
    body: StartIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Starts a dev environment without a GPU (no booking): from a template, or one of my kept environments."""
    template, project = await _start_target(db, body, user)
    if body.node_id is not None and request.app.state.nodes.find(body.node_id) is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_node_missing")
    try:
        s = await request.app.state.devenv.cpu.start(db, user, template=template, project=project, node_id=body.node_id)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    return (await _session_out(db, request, [s], user))[0]


@router.post("/sessions/{session_id}/stop", status_code=202)
async def stop_cpu(
    session_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Pauses a CPU-only environment (its owner or an admin): a kept one keeps its container, others are removed."""
    s = await db.get(DevSession, session_id)
    if s is None or s.kind != KIND or (s.user_id != user.id and not user.is_admin):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if s.user_id != user.id:
        record(db, user, "devenv.cpu_stop", f"#{s.id}")
        await db.commit()
    try:
        await request.app.state.devenv.cpu.stop(s.id)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    return {"ok": True}


@router.put("/projects/{project_id}", status_code=204)
async def project_after_booking(
    project_id: int, body: AfterBookingIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Whether a kept environment continues without a GPU when a booking of it ends (otherwise it is paused)."""
    p = await _project(db, project_id, user)
    p.cpu_after_booking = body.after_booking
    await db.commit()
