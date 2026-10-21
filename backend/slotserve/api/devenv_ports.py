"""Ports of dev environments: listing them (with what listens), adding and removing them at runtime, choosing how
each is opened (through the SlotServe login, or directly on a host port), and the overview of direct ports."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..errors import ApiError
from ..orm import DevPort, DevProject, DevSession, DevTemplate, User
from ..services.audit import record
from ..services.devenv import OPEN_STATES, session_web, web_base
from ..services.devenv_ports import MAX_PORTS, RESERVED, PortManager, clean_name
from .devenv import _session, ssh_host

router = APIRouter(prefix="/api/devenv", tags=["devenv"])

Mode = Literal["login", "direct"]


class PortIn(BaseModel):
    name: str = Field(default="", max_length=32)
    mode: Mode = "login"


class PortOut(BaseModel):
    id: int | None  # None: a declared port nobody changed
    name: str
    port: int
    kind: str
    mode: str
    declared: bool
    host_port: int | None
    url: str  # through the SlotServe login
    direct_url: str | None  # http://server:<host port>
    status: str  # login | active | starting | paused | off | error
    error: str | None
    listening: str | None  # all | local (loopback only) | none; None = unknown


class PortsOut(BaseModel):
    enabled: bool  # direct ports allowed at all
    can_direct: bool  # the owner may have one more direct port (or already has some)
    can_add: bool  # ports can be added to this environment
    limit: int
    used: int
    range: str
    ports: list[PortOut]
    detected: list[int]  # listening ports that are not listed yet


class PublicPortOut(BaseModel):
    id: int
    host_port: int
    container_port: int
    name: str
    owner: str | None
    project_id: int | None
    project_name: str | None
    session_id: int | None
    environment: str  # project, template or image name
    status: str  # active | paused | error | off
    error: str | None
    url: str
    created_at: datetime


def _ports(request: Request) -> PortManager:
    return request.app.state.devenv.ports


def _host(request: Request) -> str:
    return ssh_host(request)


async def _owner(db: AsyncSession, manager: PortManager, s: DevSession) -> tuple[int | None, User | None]:
    owner_id = await manager.owner_of(db, s)
    return owner_id, (await db.get(User, owner_id) if owner_id else None)


def _status(manager: PortManager, s: DevSession, mode: str, row: DevPort | None) -> str:
    if mode != "direct":
        return "login"
    if row is None or row.host_port is None:
        return "error"
    if not get_settings().devenv_public_ports_enabled:
        return "off"
    if s.state != "running":
        return "paused"
    if row.id in manager.active:
        return "active"
    return "error" if row.error else "starting"


async def _ports_out(db: AsyncSession, request: Request, s: DevSession, user: User) -> PortsOut:
    manager = _ports(request)
    settings = get_settings()
    rows = await manager.rows(db, s)
    entries = manager.merge(await manager.declared(db, s), rows)
    listening = await manager.listening(s)
    web = session_web(s) if s.state == "running" else None
    base = int((web or {}).get("base_session") or s.id)
    host = _host(request)
    owner_id, owner = await _owner(db, manager, s)
    used = await manager.direct_count(db, owner_id) if owner_id else 0
    has_direct = any(e["mode"] == "direct" for e in entries)
    problem = await manager.direct_problem(db, owner, owner_id, acting_admin=user.is_admin)
    out = []
    for e in entries:
        row = e["row"]
        hp = row.host_port if row is not None else None
        state = None if listening is None else listening.get(e["port"], "none")
        out.append(
            PortOut(
                id=row.id if row is not None else None,
                name=e["name"],
                port=e["port"],
                kind=e["kind"],
                mode=e["mode"],
                declared=e["declared"],
                host_port=hp,
                url=web_base(base, e["port"]),
                direct_url=f"http://{host}:{hp}/" if hp and e["mode"] == "direct" else None,
                status=_status(manager, s, e["mode"], row),
                error=row.error if row is not None else None,
                listening=state,
            )
        )
    listed = {e["port"] for e in entries} | RESERVED
    return PortsOut(
        enabled=settings.devenv_public_ports_enabled,
        can_direct=problem is None or has_direct,
        can_add=s.kind != "shared" and s.state in OPEN_STATES and len(entries) < MAX_PORTS,
        limit=settings.devenv_public_port_limit,
        used=used,
        range=settings.devenv_public_ports,
        ports=out,
        # reachable from outside the container (not loopback only), not listed yet
        detected=sorted(
            p for p, where in (listening or {}).items() if where == "all" and p not in listed and p < 32768
        ),
    )


@router.get("/sessions/{session_id}/ports", response_model=PortsOut)
async def list_ports(
    session_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    s = await _session(db, session_id, user)
    return await _ports_out(db, request, s, user)


@router.put("/sessions/{session_id}/ports/{port}", response_model=PortsOut)
async def put_port(
    session_id: int,
    port: int,
    body: PortIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Adds a port to an environment, or changes its name or how it is opened; takes effect while it runs."""
    s = await _session(db, session_id, user, manage=True)
    manager = _ports(request)
    if not 1 <= port <= 65535 or port in RESERVED:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_port_reserved")
    if s.state not in OPEN_STATES:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_not_running")
    declared = {d["port"]: d for d in await manager.declared(db, s)}
    if s.kind == "shared" and port not in declared:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_shared_ports")
    rows = {r.container_port: r for r in await manager.rows(db, s)}
    row = rows.get(port)
    if row is None and port not in declared and len(set(rows) | set(declared)) >= MAX_PORTS:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_port_limit", limit=MAX_PORTS)
    owner_id, owner = await _owner(db, manager, s)
    if row is None:
        row = DevPort(
            owner_id=owner_id,
            project_id=s.project_id,
            session_id=None if s.project_id else s.id,
            container_port=port,
            declared=port in declared,
        )
        db.add(row)
    default_name = declared[port]["name"] if port in declared else str(port)
    row.name = clean_name(body.name, port) if body.name.strip() else (row.name or default_name)
    if body.mode == "direct" and row.host_port is None:
        problem = await manager.direct_problem(db, owner, owner_id, acting_admin=user.is_admin)
        if problem is not None:
            await db.rollback()
            code = status.HTTP_403_FORBIDDEN if problem == "devenv_public_ports_admin_only" else 400
            raise ApiError(code, problem, limit=get_settings().devenv_public_port_limit)
        row.host_port = await manager.free_host_port(db)
        if row.host_port is None:
            await db.rollback()
            raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_public_ports_full")
    elif body.mode == "login" and row.host_port is not None:
        await manager.release_host_port(row)
    row.mode, row.error = body.mode, None
    if user.id != owner_id and body.mode == "direct":
        record(db, user, "devenv.port_direct", f"#{s.id}", port=port)
    try:
        await db.commit()
    except IntegrityError as exc:  # someone took the same host port at the same moment
        await db.rollback()
        raise ApiError(status.HTTP_409_CONFLICT, "devenv_public_port_taken") from exc
    request.app.state.devenv.poke()
    return await _ports_out(db, request, s, user)


@router.delete("/sessions/{session_id}/ports/{port}", response_model=PortsOut)
async def delete_port(
    session_id: int, port: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Removes an added port (a declared one goes back to the template's setting); its host port is released."""
    s = await _session(db, session_id, user, manage=True)
    manager = _ports(request)
    row = next((r for r in await manager.rows(db, s) if r.container_port == port), None)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await manager.remove_row(db, row)
    await db.commit()
    request.app.state.devenv.poke()
    return await _ports_out(db, request, s, user)


# ---- direct ports of everyone ----------------------------------------------------------------------


async def _environment(db: AsyncSession, row: DevPort) -> tuple[str, str, DevSession | None]:
    """(environment name, project name, the session it runs in now)."""
    if row.project_id:
        project = await db.get(DevProject, row.project_id)
        running = (
            await db.execute(
                select(DevSession).where(DevSession.project_id == row.project_id, DevSession.state == "running")
            )
        ).scalar()
        name = project.name if project else f"#{row.project_id}"
        return name, name, running
    s = await db.get(DevSession, row.session_id) if row.session_id else None
    template = await db.get(DevTemplate, s.template_id) if s is not None and s.template_id else None
    name = template.name if template else (s.image if s is not None else "?")
    return name, "", s


@router.get("/public-ports", response_model=list[PublicPortOut])
async def public_ports(
    request: Request, everyone: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Direct ports: your own, or everyone's for admins (with ``everyone``)."""
    manager = _ports(request)
    stmt = select(DevPort).where(DevPort.host_port.is_not(None)).order_by(DevPort.host_port)
    if not (everyone and user.is_admin):
        stmt = stmt.where(DevPort.owner_id == user.id)
    host = _host(request)
    out = []
    for row in (await db.execute(stmt)).scalars():
        name, project_name, s = await _environment(db, row)
        owner = await db.get(User, row.owner_id) if row.owner_id else None
        out.append(
            PublicPortOut(
                id=row.id,
                host_port=row.host_port,
                container_port=row.container_port,
                name=row.name or str(row.container_port),
                owner=owner.username if owner else None,
                project_id=row.project_id,
                project_name=project_name or None,
                session_id=s.id if s is not None else row.session_id,
                environment=name,
                status=_status(manager, s, row.mode, row) if s is not None else "paused",
                error=row.error,
                url=f"http://{host}:{row.host_port}/",
                created_at=row.created_at,
            )
        )
    return out


@router.delete("/public-ports/{port_id}", status_code=204)
async def release_public_port(
    port_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Releases a direct port (its owner) or reclaims it (admins): the host port goes back to the pool and the
    container port stays listed, opened through the SlotServe login. The environment keeps running."""
    row = await db.get(DevPort, port_id)
    if row is None or row.host_port is None or (row.owner_id != user.id and not user.is_admin):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if row.owner_id != user.id:
        record(db, user, "devenv.port_reclaim", f"{row.host_port}", container_port=row.container_port)
    await _ports(request).release_host_port(row)
    row.mode = "login"
    await db.commit()
    request.app.state.devenv.poke()
