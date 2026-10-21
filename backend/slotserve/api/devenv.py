"""Dev environments: templates, sessions (the containers behind bookings) and SSH keys."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..errors import ApiError
from ..orm import DevSession, DevTemplate, Lane, Reservation, SshKey, User
from ..services.audit import record
from ..services.devenv import DevEnvError, check_image, parse_public_key

router = APIRouter(prefix="/api/devenv", tags=["devenv"])
keys_router = APIRouter(prefix="/api/ssh-keys", tags=["devenv"])


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    command: str | None = Field(default=None, max_length=4000)
    env: dict[str, str] = Field(default_factory=dict)
    shared: bool = False  # admins: offer it to everyone


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    image: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=4000)
    env: dict[str, str] | None = None
    enabled: bool | None = None


class TemplateOut(BaseModel):
    id: int
    name: str
    image: str
    description: str
    command: str | None
    env: dict[str, str]
    kind: str  # builtin | custom | saved
    builtin_key: str | None
    owner: str | None
    mine: bool
    enabled: bool
    created_at: datetime


class SessionOut(BaseModel):
    id: int
    reservation_id: int
    username: str
    template_name: str | None
    image: str
    lane_name: str | None
    state: str
    detail: str | None
    progress: float | None
    error: str | None
    ssh_port: int | None
    ssh_command: str | None
    start_at: datetime | None
    end_at: datetime | None
    started_at: datetime | None
    stopped_at: datetime | None


class SaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class KeyIn(BaseModel):
    name: str = Field(default="", max_length=64)
    public_key: str = Field(min_length=20, max_length=16000)


class KeyOut(BaseModel):
    id: int
    name: str
    fingerprint: str
    key_type: str
    created_at: datetime


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
    fingerprint = None
    if s.devenv_enabled:
        try:
            fingerprint = manager.ssh_bundle().fingerprint
        except (DevEnvError, OSError):
            fingerprint = None
    return {
        "enabled": s.devenv_enabled,
        "docker": docker if user.is_admin else {"ok": docker["ok"]},
        "ssh_host": ssh_host(request),
        "host_key_fingerprint": fingerprint,
        "custom_images": s.devenv_custom_images,
        "saved_limit": s.devenv_saved_images,
        "pull_minutes": s.devenv_pull_minutes,
    }


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
    if body.shared and not user.is_admin:
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
        kind="builtin" if body.shared else "custom",
        owner_id=None if body.shared else user.id,
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
    if body.enabled is not None:
        t.enabled = body.enabled
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


async def _session_out(db: AsyncSession, request: Request, rows: list[DevSession]) -> list[SessionOut]:
    host = ssh_host(request)
    out = []
    for s in rows:
        r = await db.get(Reservation, s.reservation_id)
        owner = await db.get(User, s.user_id)
        template = await db.get(DevTemplate, s.template_id) if s.template_id else None
        lane = await db.get(Lane, s.lane_id) if s.lane_id else None
        out.append(
            SessionOut(
                id=s.id,
                reservation_id=s.reservation_id,
                username=owner.username if owner else "?",
                template_name=template.name if template else None,
                image=s.image,
                lane_name=lane.name if lane else None,
                state=s.state,
                detail=s.detail,
                progress=s.progress,
                error=s.error,
                ssh_port=s.ssh_port if s.state == "running" else None,
                ssh_command=f"ssh -p {s.ssh_port} root@{host}" if s.state == "running" and s.ssh_port else None,
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
    stmt = select(DevSession).order_by(DevSession.id.desc()).limit(limit)
    if not (everyone and user.is_admin):
        stmt = stmt.where(DevSession.user_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return await _session_out(db, request, list(rows))


async def _own_session(db: AsyncSession, session_id: int, user: User) -> DevSession:
    s = await db.get(DevSession, session_id)
    if s is None or (s.user_id != user.id and not user.is_admin):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return s


@router.post("/sessions/{session_id}/restart", status_code=202)
async def restart_session(
    session_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    s = await _own_session(db, session_id, user)
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
    s = await _own_session(db, session_id, user)
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
    s = await _own_session(db, session_id, user)
    return {"text": await request.app.state.devenv.logs(s, tail)}


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
async def add_key(body: KeyIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
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
    return _key_out(k)


@keys_router.delete("/{key_id}", status_code=204)
async def delete_key(key_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    k = await db.get(SshKey, key_id)
    if k is None or k.user_id != user.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await db.delete(k)
    await db.commit()
