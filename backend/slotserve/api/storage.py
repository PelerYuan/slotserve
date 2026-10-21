"""Storage managed in the panel (Admin -> Storage) and what each person can see of it."""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import admin_user, current_user
from ..errors import ApiError
from ..orm import Node, Storage, User
from ..services import storage as svc
from ..services.audit import record

router = APIRouter(prefix="/api/admin/storage", tags=["storage"])
user_router = APIRouter(prefix="/api/storage", tags=["storage"])

Kind = Literal["host", "nfs", "smb"]
Purpose = Literal["shared", "personal", "projects", "models"]
Access = Literal["everyone", "users", "admins"]


class StorageIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Kind
    purpose: Purpose
    host_path: str | None = Field(default=None, max_length=500)
    server: str | None = Field(default=None, max_length=255)
    remote_path: str | None = Field(default=None, max_length=500)
    options: str | None = Field(default=None, max_length=500)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)  # None keeps the stored one, "" removes it
    mount_path: str | None = Field(default=None, max_length=255)
    read_only: bool = False
    access: Access = "everyone"
    user_ids: list[int] = Field(default_factory=list, max_length=1000)
    local_path: str | None = Field(default=None, max_length=500)
    quota_gb: float | None = Field(default=None, gt=0, le=1_000_000)
    enabled: bool = True
    node_ids: list[int] = Field(default_factory=list, max_length=200)  # host: other machines that have it too


class CheckOut(BaseModel):
    ok: bool
    error: str | None = None
    total: int | None = None
    used: int | None = None
    free: int | None = None
    writable: bool | None = None


class StorageOut(BaseModel):
    id: int
    name: str
    kind: str
    purpose: str
    source: str  # host path, server:/export or //server/share
    host_path: str | None
    server: str | None
    remote_path: str | None
    options: str | None
    username: str | None
    has_password: bool  # the password itself is never returned
    mount_path: str | None  # as entered (shared, models)
    container_path: str | None  # where environments see it
    read_only: bool
    access: str
    user_ids: list[int]
    usernames: list[str]
    local_path: str | None
    quota_gb: float | None
    enabled: bool
    node_ids: list[int]  # host: other machines where the same directory exists (their environments can use it)
    status: CheckOut | None
    checked_at: datetime | None
    usage_total: int | None  # personal: sum of everyone's folders
    usage_people: int  # personal: folders measured
    over_quota: int  # personal: people over the soft limit
    created_at: datetime


class UsageOut(BaseModel):
    user_id: int
    username: str
    bytes: int
    over: bool


class MyStorageOut(BaseModel):
    id: int
    name: str
    purpose: str
    path: str | None  # in environments
    read_only: bool
    ok: bool | None
    used: int | None  # personal: my folder
    quota_gb: float | None
    over: bool


def _error(exc: svc.StorageError) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, exc.code, **exc.params)


async def _out(db: AsyncSession, rows: list[Storage]) -> list[StorageOut]:
    ids = {i for st in rows for i in svc.user_ids(st)}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(ids)))).all()) if ids else {}
    out = []
    for st in rows:
        check = svc.load_json(st.status, {})
        usage = svc.load_json(st.usage, {})
        limit = (st.quota_gb or 0) * 1e9
        out.append(
            StorageOut(
                id=st.id,
                name=st.name,
                kind=st.kind,
                purpose=st.purpose,
                source=svc.source(st),
                host_path=st.host_path,
                server=st.server,
                remote_path=st.remote_path,
                options=st.options,
                username=st.username,
                has_password=bool(st.password_enc),
                mount_path=st.mount_path,
                container_path=svc.container_path(st),
                read_only=bool(st.read_only),
                access=st.access,
                user_ids=svc.user_ids(st),
                usernames=[names[i] for i in svc.user_ids(st) if i in names],
                local_path=st.local_path,
                quota_gb=st.quota_gb,
                enabled=st.enabled,
                node_ids=svc.node_ids(st),
                status=CheckOut(**{k: check.get(k) for k in CheckOut.model_fields}) if "ok" in check else None,
                checked_at=st.checked_at,
                usage_total=sum(usage.values()) if st.purpose == "personal" and st.usage else None,
                usage_people=len(usage),
                over_quota=sum(1 for v in usage.values() if limit and v > limit),
                created_at=st.created_at,
            )
        )
    return out


async def _apply(db: AsyncSession, st: Storage, body: StorageIn) -> None:
    """Validates a storage's settings and writes them onto ``st``."""
    secret_key = get_settings().secret_key
    try:
        st.name = body.name.strip()
        if not st.name:
            raise svc.StorageError("storage_name_invalid")
        st.kind, st.purpose, st.access = body.kind, body.purpose, body.access
        st.host_path = st.server = st.remote_path = st.options = st.username = None
        if body.kind == "host":
            st.host_path = svc.check_host_path(body.host_path or "")
            st.password_enc = None
        else:
            remote = (body.remote_path or "").strip()
            server = (body.server or "").strip()
            if body.kind == "smb" and remote.startswith("//"):
                # //server/share/dir pasted as a whole
                head, _, rest = remote[2:].partition("/")
                server, remote = server or head, rest
            st.server = svc.check_server(server)
            st.remote_path = svc.check_remote_path(body.kind, remote)
            st.options = svc.check_options(body.options)
            if body.kind == "smb":
                st.username = svc.check_username(body.username)
                if body.password is not None:
                    st.password_enc = (
                        svc.encrypt_password(svc.check_password(body.password), secret_key) if body.password else None
                    )
                if st.username is None:
                    st.password_enc = None
            else:
                st.password_enc = None
        st.mount_path = None
        if body.purpose == "shared" or (body.purpose == "models" and (body.mount_path or "").strip()):
            st.mount_path = svc.check_mount_path(body.mount_path or "")
        st.read_only = body.read_only if body.purpose in ("shared", "models") else False
        st.local_path = svc.check_local_path(body.local_path) if body.purpose == "models" else None
        if body.purpose == "models" and st.kind != "host" and not st.local_path:
            raise svc.StorageError("storage_local_required")
        st.quota_gb = body.quota_gb if body.purpose == "personal" else None
        ids = sorted(set(body.user_ids)) if body.access == "users" else []
        if body.access == "users":
            found = set((await db.execute(select(User.id).where(User.id.in_(ids)))).scalars()) if ids else set()
            if not ids or found != set(ids):
                raise svc.StorageError("storage_users_invalid")
        st.user_ids = json.dumps(ids) if ids else None
        st.enabled = body.enabled
        nodes = sorted(set(body.node_ids)) if body.kind == "host" else []
        if nodes:
            found = set(
                (await db.execute(select(Node.id).where(Node.id.in_(nodes), Node.is_local.is_(False)))).scalars()
            )
            if found != set(nodes):
                raise svc.StorageError("storage_nodes_invalid")
        st.node_ids = json.dumps(nodes) if nodes else None
    except svc.StorageError as exc:
        raise _error(exc) from exc
    taken = await db.execute(select(Storage.id).where(Storage.name == st.name, Storage.id != (st.id or 0)))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "storage_name_taken")
    if st.mount_path and st.enabled:
        others = (
            await db.execute(
                select(Storage).where(
                    Storage.id != (st.id or 0), Storage.enabled.is_(True), Storage.mount_path.is_not(None)
                )
            )
        ).scalars()
        for o in others:
            a, b = st.mount_path, o.mount_path
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise ApiError(status.HTTP_409_CONFLICT, "storage_mount_taken", name=o.name)


def _summary(st: Storage) -> dict:
    return {"kind": st.kind, "purpose": st.purpose, "source": svc.source(st), "access": st.access}


@router.get("", response_model=list[StorageOut])
async def list_storages(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = list((await db.execute(select(Storage).order_by(Storage.id))).scalars())
    return await _out(db, rows)


@router.post("", response_model=StorageOut, status_code=201)
async def create_storage(body: StorageIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    st = Storage()
    await _apply(db, st, body)
    db.add(st)
    await db.flush()
    record(db, admin, "storage.create", st.name, **_summary(st))
    await db.commit()
    return (await _out(db, [st]))[0]


async def _get(db: AsyncSession, storage_id: int) -> Storage:
    st = await db.get(Storage, storage_id)
    if st is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return st


@router.put("/{storage_id}", response_model=StorageOut)
async def update_storage(
    storage_id: int,
    body: StorageIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    st = await _get(db, storage_id)
    before = svc.config_hash(st, get_settings().secret_key)
    await _apply(db, st, body)
    changed = svc.config_hash(st, get_settings().secret_key) != before
    if changed:
        st.status = st.checked_at = None  # check it again
    record(db, admin, "storage.update", st.name, **_summary(st))
    await db.commit()
    if changed:
        await request.app.state.storage.remove_unused_volumes()
    return (await _out(db, [st]))[0]


@router.delete("/{storage_id}", status_code=204)
async def delete_storage(
    storage_id: int, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    """Removes the storage from SlotServe; the data on it stays where it is."""
    st = await _get(db, storage_id)
    record(db, admin, "storage.delete", st.name, **_summary(st))
    await db.delete(st)
    await db.commit()
    await request.app.state.storage.remove_unused_volumes()


@router.post("/{storage_id}/check", response_model=StorageOut)
async def check_storage(
    storage_id: int, request: Request, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    await _get(db, storage_id)
    st = await request.app.state.storage.check(storage_id)
    if st is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return (await _out(db, [st]))[0]


@router.get("/{storage_id}/usage", response_model=list[UsageOut])
async def storage_usage(storage_id: int, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Per-person usage of a personal storage, from the last check (largest first)."""
    st = await _get(db, storage_id)
    usage = {int(k): int(v) for k, v in svc.load_json(st.usage, {}).items() if str(k).isdigit()}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(usage)))).all()) if usage else {}
    limit = (st.quota_gb or 0) * 1e9
    rows = [
        UsageOut(user_id=uid, username=names.get(uid, f"#{uid}"), bytes=used, over=bool(limit and used > limit))
        for uid, used in usage.items()
    ]
    return sorted(rows, key=lambda r: -r.bytes)


@user_router.get("", response_model=list[MyStorageOut])
async def my_storages(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The storages a person's environments get, with their own usage of a personal storage."""
    out = []
    seen: set[str] = set()
    for st in await svc.enabled_storages(db):
        if not svc.allows(st, user) or (st.purpose == "models" and not st.mount_path):
            continue
        if st.purpose in ("personal", "projects"):
            if st.purpose in seen:
                continue  # only the first one a person may use counts
            seen.add(st.purpose)
        used = svc.load_json(st.usage, {}).get(str(user.id)) if st.purpose == "personal" else None
        limit = (st.quota_gb or 0) * 1e9
        out.append(
            MyStorageOut(
                id=st.id,
                name=st.name,
                purpose=st.purpose,
                path=svc.container_path(st),
                read_only=bool(st.read_only),
                ok=svc.load_json(st.status, {}).get("ok"),
                used=used,
                quota_gb=st.quota_gb,
                over=bool(limit and used and used > limit),
            )
        )
    return out
