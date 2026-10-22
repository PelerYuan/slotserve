"""Dev environments: templates, sessions (the containers behind bookings), kept projects, SSH keys, passwords."""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import DevProject, DevSession, DevSharedMember, DevTemplate, Lane, Reservation, SshKey, User
from ..services.audit import record
from ..services.devenv import (
    OPEN_STATES,
    DevEnvError,
    can_access,
    check_image,
    check_persist_paths,
    dump_paths,
    load_paths,
    load_web_ports,
    parse_public_key,
    session_web,
    unix_names,
    visible,
    web_base,
)
from ..services.shacrypt import sha512_crypt

router = APIRouter(prefix="/api/devenv", tags=["devenv"])
keys_router = APIRouter(prefix="/api/ssh-keys", tags=["devenv"])


WebKind = Literal["desktop", "jupyter", "vscode", "http"]


class WebPort(BaseModel):
    """A web interface a template declares: opened at /devenv/<session>/port/<port>/."""

    name: str = Field(default="", max_length=32)
    port: int = Field(ge=1, le=65535)
    kind: WebKind = "http"
    # [ports hook] login: through SlotServe's proxy; direct: published on a host port (services/devenv_ports.py)
    mode: Literal["login", "direct"] = "login"


def _web_ports(ports: list[WebPort]) -> str | None:
    if len({p.port for p in ports}) != len(ports):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_web_port_duplicate")
    items = []
    for p in ports:
        item = {"name": p.name.strip() or str(p.port), "port": p.port, "kind": p.kind}
        if p.mode == "direct":
            item["mode"] = "direct"
        items.append(item)
    return json.dumps(items) if items else None


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    command: str | None = Field(default=None, max_length=4000)
    env: dict[str, str] = Field(default_factory=dict)
    web_ports: list[WebPort] = Field(default_factory=list, max_length=10)
    keep_entrypoint: bool = False
    persist_paths: list[str] = Field(default_factory=list, max_length=20)  # kept besides /workspace
    shared: bool = False  # admins: offer it to everyone
    compat: bool = False  # admins: compatibility mode, no no-new-privileges (sudo, s6-overlay; D14)


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    image: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=4000)
    env: dict[str, str] | None = None
    web_ports: list[WebPort] | None = Field(default=None, max_length=10)
    keep_entrypoint: bool | None = None
    persist_paths: list[str] | None = Field(default=None, max_length=20)
    enabled: bool | None = None
    compat: bool | None = None  # admins only


class TemplateOut(BaseModel):
    id: int
    name: str
    image: str
    description: str
    command: str | None
    env: dict[str, str]
    web_ports: list[WebPort]
    keep_entrypoint: bool
    persist_paths: list[str]
    compat: bool  # runs in compatibility mode (no no-new-privileges): the UI says so
    kind: str  # builtin | custom | saved
    builtin_key: str | None
    owner: str | None
    mine: bool
    enabled: bool
    created_at: datetime


class WebOut(BaseModel):
    name: str
    port: int
    kind: str
    url: str


class SessionOut(BaseModel):
    id: int
    kind: str  # booking | shared (the environment of an idle lane, open to everyone) | cpu (without a GPU)
    reservation_id: int | None
    username: str | None
    mine: bool
    project_id: int | None
    project_name: str | None
    lane_id: int | None
    template_name: str | None
    image: str
    lane_name: str | None
    state: str
    detail: str | None
    progress: float | None
    error: str | None
    ssh_port: int | None
    ssh_user: str | None
    ssh_command: str | None
    web: list[WebOut]  # the declared web interfaces, while it runs
    host_key_fingerprint: str | None = None  # this environment's own SSH host key (None until it first starts)
    joined: bool | None = None  # shared idle environments: whether you joined it (only members get an account)
    start_at: datetime | None
    end_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None


class SaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    template_id: int
    persist_paths: list[str] | None = Field(default=None, max_length=20)  # None: the template's


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    persist_paths: list[str] | None = Field(default=None, max_length=20)


class ProjectOut(BaseModel):
    id: int
    name: str
    owner: str | None
    mine: bool
    template_id: int | None
    template_name: str | None
    image: str
    persist_paths: list[str]
    state: str  # new | running | paused | missing
    size_bytes: int | None
    last_used_at: datetime | None
    created_at: datetime
    upcoming: int  # bookings that will continue it
    host_key_fingerprint: str | None = None  # its own SSH host key, kept across pauses and GPU changes


class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class KeyIn(BaseModel):
    name: str = Field(default="", max_length=64)
    public_key: str = Field(min_length=20, max_length=16000)


class KeyOut(BaseModel):
    id: int
    name: str
    fingerprint: str
    key_type: str
    created_at: datetime


def _paths(paths: list[str]) -> list[str]:
    try:
        return check_persist_paths(paths)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc


def _devenv_error(exc: DevEnvError) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, exc.code, **exc.params)


def ssh_host(request: Request) -> str:
    return get_settings().devenv_ssh_host or request.url.hostname or "localhost"


async def _template_out(db: AsyncSession, rows: list[DevTemplate], user: User) -> list[TemplateOut]:
    owners = {t.owner_id for t in rows if t.owner_id}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(owners)))).all()) if owners else {}
    out = []
    for t in rows:
        try:
            env = json.loads(t.env) if t.env else {}
        except ValueError:
            env = {}
        out.append(
            TemplateOut(
                id=t.id,
                name=t.name,
                image=t.image,
                description=t.description or "",
                command=t.command,
                env=env,
                web_ports=[WebPort(**p) for p in load_web_ports(t.web_ports)],
                keep_entrypoint=bool(t.keep_entrypoint),
                persist_paths=load_paths(t.persist_paths),
                compat=bool(t.compat),
                kind=t.kind,
                builtin_key=t.builtin_key,
                owner=names.get(t.owner_id),
                mine=t.owner_id == user.id,
                enabled=t.enabled,
                created_at=t.created_at,
            )
        )
    return out


@router.get("")
async def devenv_info(request: Request, user: User = Depends(current_user)):
    s = get_settings()
    manager = request.app.state.devenv
    docker = await manager.refresh_status() if s.devenv_enabled else {"ok": False, "error": None, "version": None}
    out = {
        "enabled": s.devenv_enabled,
        "docker": docker if user.is_admin else {"ok": docker["ok"]},
        "ssh_host": ssh_host(request),
        # every environment has its own host key now: see host_key_fingerprint of sessions and projects
        "host_key_fingerprint": None,
        "custom_images": s.devenv_custom_images,
        "saved_limit": s.devenv_saved_images,
        "pull_minutes": s.devenv_pull_minutes,
        "password_login": s.devenv_password_login,
        "password_set": bool(user.devenv_password_hash),
        "projects_limit": s.devenv_projects,
        "project_days": s.devenv_project_days,
    }
    if user.is_admin:
        out["hardening"] = await manager.hardening_summary() if s.devenv_enabled else None
    return out


# ---- templates ----------------------------------------------------------------


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(DevTemplate).where(DevTemplate.deleted_at.is_(None))
    if user.is_admin:
        stmt = stmt.where(or_(DevTemplate.kind == "builtin", DevTemplate.owner_id == user.id))
    else:
        stmt = stmt.where(
            or_((DevTemplate.kind == "builtin") & DevTemplate.enabled.is_(True), DevTemplate.owner_id == user.id)
        )
    rows = (await db.execute(stmt.order_by(DevTemplate.kind, DevTemplate.id))).scalars().all()
    return await _template_out(db, list(rows), user)


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(body: TemplateIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    if (body.shared or body.compat) and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    if not s.devenv_custom_images and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "devenv_custom_disabled")
    try:
        image = check_image(s, body.image)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    t = DevTemplate(
        name=body.name.strip(),
        image=image,
        description=body.description.strip(),
        command=(body.command or "").strip() or None,
        env=json.dumps(body.env) if body.env else None,
        web_ports=_web_ports(body.web_ports),
        keep_entrypoint=body.keep_entrypoint,
        persist_paths=dump_paths(_paths(body.persist_paths)),
        kind="builtin" if body.shared else "custom",
        owner_id=None if body.shared else user.id,
        compat=body.compat,
    )
    db.add(t)
    if body.shared:
        record(db, user, "devenv.template_create", t.name, image=image)
    await db.commit()
    return (await _template_out(db, [t], user))[0]


async def _own_template(db: AsyncSession, template_id: int, user: User) -> DevTemplate:
    t = await db.get(DevTemplate, template_id)
    if t is None or t.deleted_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if t.kind == "builtin" and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    if t.kind != "builtin" and t.owner_id != user.id and not user.is_admin:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return t


@router.patch("/templates/{template_id}", response_model=TemplateOut)
async def update_template(
    template_id: int, body: TemplatePatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    t = await _own_template(db, template_id, user)
    fields = body.model_fields_set
    if "image" in fields and body.image is not None:
        if t.kind == "saved":
            raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_saved_image_fixed")
        try:
            t.image = check_image(get_settings(), body.image)
        except DevEnvError as exc:
            raise _devenv_error(exc) from exc
    if body.name is not None:
        t.name = body.name.strip()
    if body.description is not None:
        t.description = body.description.strip()
    if "command" in fields:
        t.command = (body.command or "").strip() or None
    if body.env is not None:
        t.env = json.dumps(body.env) if body.env else None
    if body.web_ports is not None:
        t.web_ports = _web_ports(body.web_ports)
    if body.keep_entrypoint is not None:
        t.keep_entrypoint = body.keep_entrypoint
    if body.persist_paths is not None:
        t.persist_paths = dump_paths(_paths(body.persist_paths))
    if body.enabled is not None:
        t.enabled = body.enabled
    if body.compat is not None and bool(body.compat) != bool(t.compat):
        if not user.is_admin:
            raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
        t.compat = body.compat
        record(db, user, "devenv.template_compat", t.name, compat=body.compat)
    if t.kind == "builtin":
        record(db, user, "devenv.template_update", t.name, image=t.image, enabled=t.enabled)
    await db.commit()
    return (await _template_out(db, [t], user))[0]


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    t = await _own_template(db, template_id, user)
    try:
        await request.app.state.devenv.delete_template(db, t)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    if t.kind == "builtin":
        record(db, user, "devenv.template_delete", t.name)
    await db.commit()


# ---- sessions -----------------------------------------------------------------


async def _session_out(db: AsyncSession, request: Request, rows: list[DevSession], user: User) -> list[SessionOut]:
    host = ssh_host(request)
    manager = request.app.state.devenv
    names: dict[int, str] = {}
    joined: set[int] = set()
    if any(s.kind == "shared" for s in rows):
        users = list((await db.execute(select(User).where(User.disabled_at.is_(None)))).scalars())
        names = unix_names(users)
        member = select(DevSharedMember.lane_id).where(DevSharedMember.user_id == user.id)
        joined = set((await db.execute(member)).scalars())
    out = []
    for s in rows:
        r = await db.get(Reservation, s.reservation_id) if s.reservation_id else None
        owner = await db.get(User, s.user_id) if s.user_id else None
        template = await db.get(DevTemplate, s.template_id) if s.template_id else None
        project = await db.get(DevProject, s.project_id) if s.project_id else None
        lane = await db.get(Lane, s.lane_id) if s.lane_id else None
        web = session_web(s) if s.state == "running" else None
        base = int((web or {}).get("base_session") or s.id)  # a kept environment's container keeps its paths
        running = s.state == "running" and s.ssh_port is not None
        member = s.kind != "shared" or s.lane_id in joined
        login = (names.get(user.id) if member else None) if s.kind == "shared" else "root"
        out.append(
            SessionOut(
                id=s.id,
                kind=s.kind or "booking",
                reservation_id=s.reservation_id,
                username=owner.username if owner else None,
                mine=s.user_id == user.id,
                project_id=s.project_id,
                project_name=project.name if project else None,
                lane_id=s.lane_id,
                template_name=template.name if template else None,
                image=s.image,
                lane_name=lane.name if lane else None,
                state=s.state,
                detail=s.detail,
                progress=s.progress,
                error=s.error if s.kind != "shared" or user.is_admin else None,
                ssh_port=s.ssh_port if running else None,
                ssh_user=login,
                ssh_command=f"ssh -p {s.ssh_port} {login}@{host}" if running and login else None,
                web=[
                    WebOut(name=p["name"], port=p["port"], kind=p["kind"], url=web_base(base, p["port"]))
                    for p in (web or {}).get("ports", [])
                ],
                host_key_fingerprint=manager.host_key_fingerprint(manager.env_key(s)),
                joined=(s.lane_id in joined) if s.kind == "shared" else None,
                start_at=r.start_at if r else None,
                end_at=r.end_at if r else None,
                started_at=s.started_at,
                stopped_at=s.stopped_at,
            )
        )
    return out


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request,
    everyone: bool = False,
    limit: int = Query(default=20, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Your environments (everyone's for admins with ``everyone``) and the shared environments of idle lanes."""
    stmt = select(DevSession).where(DevSession.kind.is_not("shared")).order_by(DevSession.id.desc()).limit(limit)
    if not (everyone and user.is_admin):
        stmt = stmt.where(DevSession.user_id == user.id)
    rows = list((await db.execute(stmt)).scalars())
    shared = (
        await db.execute(
            select(DevSession)
            .where(DevSession.kind == "shared", DevSession.state.in_(OPEN_STATES))
            .order_by(DevSession.lane_id, DevSession.id)
        )
    ).scalars()
    return await _session_out(db, request, [*shared, *rows], user)


async def _session(db: AsyncSession, session_id: int, user: User, manage: bool = False) -> DevSession:
    """A session the person may see (``can_access``); ``manage``: restart, save, logs (admins for shared ones)."""
    s = await db.get(DevSession, session_id)
    if s is None or not can_access(s, user):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if manage and s.kind == "shared" and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    return s


@router.post("/sessions/{session_id}/restart", status_code=202)
async def restart_session(
    session_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    s = await _session(db, session_id, user, manage=True)
    if s.kind == "cpu" and s.state == "failed" and s.user_id is not None:
        try:  # devenv_cpu: a failed CPU-only environment holds no place; retrying it takes one
            await request.app.state.devenv.cpu.check_room(db, s.user_id)
        except DevEnvError as exc:
            raise _devenv_error(exc) from exc
    if s.user_id != user.id:
        record(db, user, "devenv.restart", f"#{s.id}")
        await db.commit()
    try:
        await request.app.state.devenv.restart(s.id)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    return {"ok": True}


@router.post("/sessions/{session_id}/save", status_code=202)
async def save_session(
    session_id: int,
    body: SaveIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _session(db, session_id, user, manage=True)
    if s.kind == "shared":
        # Its /etc/shadow holds everyone's password hashes; it must not end up in an image.
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_shared_save")
    try:
        await request.app.state.devenv.save(s.id, body.name, user)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    return {"ok": True}


@router.get("/sessions/{session_id}/logs")
async def session_logs(
    session_id: int,
    request: Request,
    tail: int = Query(default=300, ge=1, le=5000),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _session(db, session_id, user, manage=True)
    return {"text": await request.app.state.devenv.logs(s, tail)}


# ---- preview ----------------------------------------------------------------------


class PreviewIn(BaseModel):
    template_id: int | None = None
    project_id: int | None = None  # a kept environment (its template and kept directories)
    gpu: bool = True  # False: an environment without a GPU (CPU-only)
    lane_id: int | None = None  # the lane of a GPU environment; default: the first lane


@router.post("/preview")
async def preview(
    body: PreviewIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """What an environment would get - GPUs, CPU, memory, mounts, web interfaces, SSH, security - without starting
    anything. Redacted: no tokens, password hashes, keys or storage passwords (containers/redact.py)."""
    project = None
    if body.project_id is not None:
        project = await db.get(DevProject, body.project_id)
        if project is None or (project.owner_id != user.id and not user.is_admin):
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        template = await db.get(DevTemplate, project.template_id) if project.template_id else None
    else:
        template = await db.get(DevTemplate, body.template_id) if body.template_id is not None else None
        if not visible(template, user):
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    lane_id = None
    if body.gpu:
        lane = await db.get(Lane, body.lane_id) if body.lane_id is not None else None
        if lane is None:
            lane = (await db.execute(select(Lane).order_by(Lane.position, Lane.id).limit(1))).scalar_one_or_none()
        if lane is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        lane_id = lane.id
    s = DevSession(
        id=0,
        kind="booking" if body.gpu else "cpu",
        user_id=user.id,
        template_id=template.id if template else None,
        project_id=project.id if project else None,
        lane_id=lane_id,
        image=project.image if project else (template.image if template else ""),
        state="waiting",
    )
    out = await request.app.state.devenv.preview(db, s)
    await db.rollback()  # nothing of the transient session is kept
    return out


# ---- shared idle environments ---------------------------------------------------


@router.post("/shared/{lane_id}/join", status_code=204)
async def join_shared(
    lane_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Joins a lane's shared idle environment: you get an account there (SSH keys only, R1-10)."""
    if await db.get(Lane, lane_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if (await db.execute(select(SshKey.id).where(SshKey.user_id == user.id).limit(1))).first() is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "ssh_key_required")
    if await db.get(DevSharedMember, (lane_id, user.id)) is None:
        db.add(DevSharedMember(lane_id=lane_id, user_id=user.id))
        await db.commit()
    request.app.state.devenv.accounts_changed()


@router.delete("/shared/{lane_id}/join", status_code=204)
async def leave_shared(
    lane_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Leaves a lane's shared idle environment: your account there loses its keys at the next refresh."""
    row = await db.get(DevSharedMember, (lane_id, user.id))
    if row is not None:
        await db.delete(row)
        await db.commit()
    request.app.state.devenv.accounts_changed()


# ---- SSH keys -------------------------------------------------------------------


def _key_out(k: SshKey) -> KeyOut:
    return KeyOut(
        id=k.id, name=k.name, fingerprint=k.fingerprint, key_type=k.public_key.split()[0], created_at=k.created_at
    )


@keys_router.get("", response_model=list[KeyOut])
async def list_keys(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SshKey).where(SshKey.user_id == user.id).order_by(SshKey.id))).scalars().all()
    return [_key_out(k) for k in rows]


@keys_router.post("", response_model=KeyOut, status_code=201)
async def add_key(
    body: KeyIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    try:
        key, fingerprint = parse_public_key(body.public_key)
    except ValueError as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "ssh_key_invalid") from exc
    existing = (
        await db.execute(select(SshKey.id).where(SshKey.user_id == user.id, SshKey.fingerprint == fingerprint))
    ).first()
    if existing is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "ssh_key_exists")
    parts = key.split()
    name = body.name.strip() or (parts[2] if len(parts) > 2 else parts[0])
    k = SshKey(user_id=user.id, name=name[:64], public_key=key, fingerprint=fingerprint)
    db.add(k)
    await db.commit()
    request.app.state.devenv.accounts_changed()
    return _key_out(k)


@keys_router.delete("/{key_id}", status_code=204)
async def delete_key(
    key_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    k = await db.get(SshKey, key_id)
    if k is None or k.user_id != user.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await db.delete(k)
    await db.commit()
    request.app.state.devenv.accounts_changed()


# ---- environment password ----------------------------------------------------------


@router.get("/password")
async def password_status(user: User = Depends(current_user)):
    return {"set": bool(user.devenv_password_hash), "login_enabled": get_settings().devenv_password_login}


@router.put("/password", status_code=204)
async def set_password(
    body: PasswordIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Sets the SSH password for dev environments; only its SHA-512-crypt hash is stored."""
    if not body.password.strip() or any(ch in body.password for ch in "\n\r\0"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_password_invalid")
    user.devenv_password_hash = sha512_crypt(body.password)
    await db.commit()
    request.app.state.devenv.accounts_changed()


@router.delete("/password", status_code=204)
async def clear_password(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.devenv_password_hash = None
    await db.commit()
    request.app.state.devenv.accounts_changed()


# ---- kept environments (projects) -----------------------------------------------------


async def _project_out(db: AsyncSession, rows: list[DevProject], user: User, manager=None) -> list[ProjectOut]:
    out = []
    now = utcnow()
    for p in rows:
        owner = await db.get(User, p.owner_id)
        template = await db.get(DevTemplate, p.template_id) if p.template_id else None
        upcoming = (
            await db.execute(
                select(func.count(Reservation.id)).where(
                    Reservation.devenv_project_id == p.id,
                    Reservation.status.in_(("booked", "pending")),
                    Reservation.end_at > now,
                )
            )
        ).scalar_one()
        out.append(
            ProjectOut(
                id=p.id,
                name=p.name,
                owner=owner.username if owner else None,
                mine=p.owner_id == user.id,
                template_id=p.template_id,
                template_name=template.name if template else None,
                image=p.image,
                persist_paths=load_paths(p.persist_paths),
                state=p.state,
                size_bytes=p.size_bytes,
                last_used_at=p.last_used_at,
                created_at=p.created_at,
                upcoming=upcoming,
                host_key_fingerprint=manager.host_key_fingerprint(f"p{p.id}") if manager is not None else None,
            )
        )
    return out


async def _project(db: AsyncSession, project_id: int, user: User) -> DevProject:
    p = await db.get(DevProject, project_id)
    if p is None or (p.owner_id != user.id and not user.is_admin):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return p


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    request: Request, everyone: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    stmt = select(DevProject).order_by(DevProject.id)
    if not (everyone and user.is_admin):
        stmt = stmt.where(DevProject.owner_id == user.id)
    return await _project_out(db, list((await db.execute(stmt)).scalars()), user, request.app.state.devenv)


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = get_settings()
    if not s.devenv_enabled:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_disabled")
    template = await db.get(DevTemplate, body.template_id)
    if not visible(template, user):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    count = (await db.execute(select(func.count(DevProject.id)).where(DevProject.owner_id == user.id))).scalar_one()
    if count >= s.devenv_projects:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_project_limit", limit=s.devenv_projects)
    paths = _paths(body.persist_paths) if body.persist_paths is not None else load_paths(template.persist_paths)
    p = DevProject(
        owner_id=user.id,
        name=body.name.strip(),
        template_id=template.id,
        image=template.image,
        persist_paths=dump_paths(paths),
        state="new",
    )
    db.add(p)
    await db.commit()
    return (await _project_out(db, [p], user))[0]


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int, body: ProjectPatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Renames a project or changes its kept paths (they apply the next time its container starts)."""
    p = await _project(db, project_id, user)
    if body.name is not None:
        p.name = body.name.strip()
    if body.persist_paths is not None:
        p.persist_paths = dump_paths(_paths(body.persist_paths))
    await db.commit()
    return (await _project_out(db, [p], user))[0]


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: int,
    request: Request,
    delete_data: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Removes a project's container (and with ``delete_data`` its kept directories); /workspace stays."""
    p = await _project(db, project_id, user)
    if p.owner_id != user.id:
        record(db, user, "devenv.project_delete", f"#{p.id} {p.name}", data=delete_data)
    try:
        await request.app.state.devenv.delete_project(db, p, data=delete_data)
    except DevEnvError as exc:
        raise _devenv_error(exc) from exc
    await db.commit()
