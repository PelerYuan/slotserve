import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user, get_lanes
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, ApiKey, AuditLog, Lane, LlmModel, ModelLoadEvent, Reservation, UsageLog, User
from ..runtime.lanes import LaneManager
from ..runtime.scheduler import ManualOverride
from ..schemas import (
    AdminApiKeyOut,
    AdminPasswordIn,
    AdminStatsOut,
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    AuditOut,
    AuditPageOut,
    BackupOut,
    LoadEventOut,
    ManualIn,
    RequestLogOut,
    RequestPageOut,
    ReservationOut,
    ReviewIn,
    SettingOut,
)
from ..security import hash_password
from ..services import app_settings, booking, kv, maintenance, queries
from ..services.audit import record
from ..services.review import review

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- overview -------------------------------------------------------------


@router.get("/stats", response_model=AdminStatsOut)
async def stats(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    now = utcnow()
    since = now - timedelta(hours=24)

    async def scalar(stmt) -> int:
        return int((await db.execute(stmt)).scalar_one() or 0)

    events = (
        await db.execute(
            select(ModelLoadEvent, LlmModel.name)
            .outerjoin(LlmModel, LlmModel.id == ModelLoadEvent.model_id)
            .order_by(ModelLoadEvent.started_at.desc())
            .limit(10)
        )
    ).all()
    return AdminStatsOut(
        users=await scalar(select(func.count(User.id))),
        disabled_users=await scalar(select(func.count(User.id)).where(User.disabled_at.is_not(None))),
        admins=await scalar(select(func.count(User.id)).where(User.is_admin.is_(True))),
        active_keys=await scalar(select(func.count(ApiKey.id)).where(ApiKey.revoked_at.is_(None))),
        upcoming_reservations=await scalar(
            select(func.count(Reservation.id)).where(Reservation.status == "booked", Reservation.end_at > now)
        ),
        pending_reservations=await scalar(
            select(func.count(Reservation.id)).where(Reservation.status == "pending", Reservation.end_at > now)
        ),
        requests_24h=await scalar(select(func.count(UsageLog.id)).where(UsageLog.created_at >= since)),
        tokens_24h=await scalar(
            select(func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens)).where(UsageLog.created_at >= since)
        ),
        failed_requests_24h=await scalar(
            select(func.count(UsageLog.id)).where(UsageLog.created_at >= since, UsageLog.status_code >= 400)
        ),
        models=await scalar(select(func.count(LlmModel.id)).where(LlmModel.deleted_at.is_(None))),
        load_events=[
            LoadEventOut(
                id=e.id,
                model_name=name,
                started_at=e.started_at,
                finished_at=e.finished_at,
                success=e.success,
                error=e.error,
            )
            for e, name in events
        ],
    )


# ---- users ----------------------------------------------------------------


async def _user_rows(db: AsyncSession, users: list[User]) -> list[AdminUserOut]:
    now = utcnow()
    ids = [u.id for u in users]
    keys = dict(
        (
            await db.execute(
                select(ApiKey.user_id, func.count(ApiKey.id))
                .where(ApiKey.user_id.in_(ids), ApiKey.revoked_at.is_(None))
                .group_by(ApiKey.user_id)
            )
        ).all()
    )
    usage = {
        uid: (n, tokens or 0)
        for uid, n, tokens in (
            await db.execute(
                select(
                    UsageLog.user_id,
                    func.count(UsageLog.id),
                    func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens),
                )
                .where(UsageLog.user_id.in_(ids), UsageLog.created_at >= now - timedelta(days=30))
                .group_by(UsageLog.user_id)
            )
        ).all()
    }
    last_request = dict(
        (
            await db.execute(
                select(UsageLog.user_id, func.max(UsageLog.created_at))
                .where(UsageLog.user_id.in_(ids))
                .group_by(UsageLog.user_id)
            )
        ).all()
    )
    upcoming = dict(
        (
            await db.execute(
                select(Reservation.user_id, func.count(Reservation.id))
                .where(Reservation.user_id.in_(ids), Reservation.status.in_(HOLDING_STATUSES), Reservation.end_at > now)
                .group_by(Reservation.user_id)
            )
        ).all()
    )
    out = []
    for u in users:
        last = last_request.get(u.id)
        out.append(
            AdminUserOut(
                id=u.id,
                username=u.username,
                is_admin=u.is_admin,
                created_at=u.created_at,
                disabled_at=u.disabled_at,
                # aggregate MAX() bypasses the UTCDateTime type, so normalise here
                last_active_at=queries.as_utc(last) if last else None,
                key_count=keys.get(u.id, 0),
                requests_30d=usage.get(u.id, (0, 0))[0],
                tokens_30d=int(usage.get(u.id, (0, 0))[1]),
                upcoming_reservations=upcoming.get(u.id, 0),
            )
        )
    return out


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return user


async def _ensure_other_admin(db: AsyncSession, user: User) -> None:
    """Refuses a change that would leave no active admin."""
    others = (
        await db.execute(
            select(func.count(User.id)).where(User.is_admin.is_(True), User.disabled_at.is_(None), User.id != user.id)
        )
    ).scalar_one()
    if others == 0:
        raise ApiError(status.HTTP_409_CONFLICT, "last_admin")


async def _end_reservations(db: AsyncSession, user: User) -> int:
    """Cancels upcoming and releases the active reservation of ``user``."""
    now = utcnow()
    rows = (
        (
            await db.execute(
                select(Reservation).where(
                    Reservation.user_id == user.id,
                    Reservation.status.in_(HOLDING_STATUSES),
                    Reservation.end_at > now,
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        if r.start_at > now or r.status == "pending":
            r.status = "cancelled"
        else:
            r.end_at = now
            r.released_at = now
    return len(rows)


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return await _user_rows(db, list(users))


@router.post("/users", response_model=AdminUserOut, status_code=201)
async def create_user(body: AdminUserCreateIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    taken = await db.execute(select(User.id).where(func.lower(User.username) == body.username.lower()))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "username_taken")
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=body.is_admin)
    db.add(user)
    await db.flush()
    record(db, admin, "user.create", user.username, is_admin=body.is_admin)
    await db.commit()
    return (await _user_rows(db, [user]))[0]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    body: AdminUserUpdateIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "cannot_modify_self")
    if body.is_admin is not None and body.is_admin != user.is_admin:
        if not body.is_admin:
            await _ensure_other_admin(db, user)
        user.is_admin = body.is_admin
        record(db, admin, "user.grant_admin" if body.is_admin else "user.revoke_admin", user.username)
    if body.disabled is not None and body.disabled != (user.disabled_at is not None):
        if body.disabled:
            if user.is_admin:
                await _ensure_other_admin(db, user)
            user.disabled_at = utcnow()
            ended = await _end_reservations(db, user)
            record(db, admin, "user.disable", user.username, reservations_ended=ended)
        else:
            user.disabled_at = None
            record(db, admin, "user.enable", user.username)
    await db.commit()
    lanes.poke()
    return (await _user_rows(db, [user]))[0]


@router.post("/users/{user_id}/password", status_code=204)
async def reset_password(
    user_id: int, body: AdminPasswordIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    user = await _get_user(db, user_id)
    user.password_hash = hash_password(body.password)
    record(db, admin, "user.reset_password", user.username)
    await db.commit()


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Deletes the account with its keys, reservations and usage history."""
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "cannot_modify_self")
    if user.is_admin:
        await _ensure_other_admin(db, user)
    record(db, admin, "user.delete", user.username)
    await db.delete(user)
    await db.commit()
    lanes.poke()


# ---- api keys -------------------------------------------------------------


@router.get("/keys", response_model=list[AdminApiKeyOut])
async def list_keys(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ApiKey, User.username)
            .join(User, User.id == ApiKey.user_id)
            .where(ApiKey.revoked_at.is_(None))
            .order_by(User.username, ApiKey.created_at.desc())
        )
    ).all()
    return [
        AdminApiKeyOut(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            user_id=k.user_id,
            username=username,
        )
        for k, username in rows
    ]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    key = await db.get(ApiKey, key_id)
    if key is None or key.revoked_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    owner = await db.get(User, key.user_id)
    key.revoked_at = utcnow()
    record(db, admin, "key.revoke", f"{owner.username if owner else '?'}/{key.name}", prefix=key.prefix)
    await db.commit()


# ---- reservations ---------------------------------------------------------


@router.get("/reservations", response_model=list[ReservationOut])
async def list_reservations(
    phase: str | None = Query(default=None, pattern="^(upcoming|active|finished|cancelled|pending|rejected|expired)$"),
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    now = utcnow()
    stmt = select(Reservation)
    if phase in ("cancelled", "pending", "rejected", "expired"):
        stmt = stmt.where(Reservation.status == phase)
    elif phase is not None:
        stmt = stmt.where(Reservation.status == "booked")
        if phase == "upcoming":
            stmt = stmt.where(Reservation.start_at > now)
        elif phase == "active":
            stmt = stmt.where(Reservation.start_at <= now, Reservation.end_at > now)
        else:
            stmt = stmt.where(Reservation.end_at <= now)
    if user_id is not None:
        stmt = stmt.where(Reservation.user_id == user_id)
    rows = (await db.execute(stmt.order_by(Reservation.start_at.desc()).limit(limit))).scalars().all()
    return await queries.reservation_out(db, list(rows), now)


# ---- settings -------------------------------------------------------------


@router.get("/settings", response_model=list[SettingOut])
async def get_settings_view(request: Request, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    defaults = request.app.state.settings_defaults
    overridden = await app_settings.overridden_keys(db)
    return [
        SettingOut(
            key=key,
            value=getattr(settings, key),
            default=defaults[key],
            overridden=key in overridden,
            secret=spec.secret,
            kind=spec.kind,
            unit=spec.unit,
            min=spec.min,
            max=spec.max,
            choices=list(spec.choices) if spec.choices else None,
        )
        for key, spec in app_settings.EDITABLE.items()
    ]


@router.put("/settings", response_model=list[SettingOut])
async def put_settings(
    changes: dict[str, bool | int | float | str | None],
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Body maps setting keys to new values; ``null`` restores the ``.env`` default."""
    try:
        applied = await app_settings.apply(db, get_settings(), request.app.state.settings_defaults, changes)
    except app_settings.InvalidSetting as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_setting", key=exc.key, reason=exc.reason) from exc
    if applied:
        record(db, admin, "settings.update", ", ".join(applied), **applied)
    if "api_key_reveal" in applied and not get_settings().api_key_reveal:
        # Off means off: no copy of any key stays readable.
        await db.execute(update(ApiKey).values(key_encrypted=None))
    await db.commit()
    if applied:
        request.app.state.channels.reload_for(applied.keys())
    return await get_settings_view(request, admin, db)


# ---- audit log ------------------------------------------------------------


@router.get("/audit", response_model=AuditPageOut)
async def audit_log(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(AuditLog.id)))).scalar_one()
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).offset(offset).limit(limit))).scalars().all()
    return AuditPageOut(
        total=total,
        items=[
            AuditOut(
                id=r.id,
                actor_id=r.actor_id,
                actor_name=r.actor_name,
                action=r.action,
                target=r.target,
                detail=json.loads(r.detail or "{}"),
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


# ---- runtime control ------------------------------------------------------


@router.post("/runtime/manual", status_code=204)
async def manual_runtime(
    body: ManualIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Load a model now, unload, or hand control back to the schedule (``auto``). Bookings always win."""
    ctx = lanes.get(body.lane_id)
    scheduler = ctx.scheduler
    if body.action == "auto":
        scheduler.set_manual(None)
        await kv.put(db, scheduler.manual_key, None)
        record(db, admin, "runtime.auto", ctx.name)
        await db.commit()
        return
    if await queries.active_reservation(db, utcnow(), ctx.id) is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "reservation_active")
    model = preset = None
    if body.action == "load":
        if body.model_id is None:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "validation_error", field="model_id")
        model, preset = await booking.resolve_model(db, body.model_id, body.preset_id)
        lane = await db.get(Lane, ctx.id)
        if lane is not None:
            await booking.check_model_on_lane(db, model, lane)
    until = utcnow() + timedelta(hours=body.hours) if body.hours else None
    override = ManualOverride(
        model_id=model.id if model else None,
        preset_id=preset.id if preset else None,
        until=until,
        by=admin.username,
        created_at=utcnow(),
    )
    scheduler.set_manual(override)
    await kv.put(db, scheduler.manual_key, override.to_json())
    record(db, admin, f"runtime.{body.action}", model.name if model else "", until=until)
    await db.commit()


# ---- request log ----------------------------------------------------------


@router.get("/requests", response_model=RequestPageOut)
async def request_log(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: int | None = None,
    errors_only: bool = False,
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(UsageLog)
    count = select(func.count(UsageLog.id))
    if user_id is not None:
        stmt, count = stmt.where(UsageLog.user_id == user_id), count.where(UsageLog.user_id == user_id)
    if errors_only:
        stmt, count = stmt.where(UsageLog.status_code >= 400), count.where(UsageLog.status_code >= 400)
    total = (await db.execute(count)).scalar_one()
    rows = (await db.execute(stmt.order_by(UsageLog.id.desc()).offset(offset).limit(limit))).scalars().all()
    names = dict((await db.execute(select(User.id, User.username))).all())
    keys = dict((await db.execute(select(ApiKey.id, ApiKey.name))).all())
    return RequestPageOut(
        total=total,
        items=[
            RequestLogOut(
                id=u.id,
                created_at=u.created_at,
                username=names.get(u.user_id, "?"),
                key_name=keys.get(u.api_key_id),
                model_name=u.model_name,
                endpoint=u.endpoint,
                stream=u.stream,
                status_code=u.status_code,
                duration_ms=u.duration_ms,
                prompt_tokens=u.prompt_tokens,
                completion_tokens=u.completion_tokens,
            )
            for u in rows
        ],
    )


# ---- backups --------------------------------------------------------------

_BACKUP_NAME = re.compile(r"^slotserve-(daily|manual|v\d+)-\d{8}-\d{6}\.db$")


def _backup_out(path) -> BackupOut:
    kind = path.name.split("-")[1]
    st = path.stat()
    return BackupOut(
        name=path.name,
        kind=kind if kind in ("daily", "manual") else "upgrade",
        size_bytes=st.st_size,
        created_at=datetime.fromtimestamp(st.st_mtime, UTC),
    )


@router.get("/backups", response_model=list[BackupOut])
async def list_backups(_: User = Depends(admin_user)):
    return [_backup_out(p) for p in maintenance.list_backups(get_settings()) if _BACKUP_NAME.match(p.name)]


@router.post("/backups", response_model=BackupOut, status_code=201)
async def create_backup(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    path = await asyncio.to_thread(maintenance.make_backup, get_settings(), "manual")
    record(db, admin, "backup.create", path.name)
    await db.commit()
    return _backup_out(path)


@router.get("/backups/{name}")
async def download_backup(name: str, _: User = Depends(admin_user)):
    if not _BACKUP_NAME.match(name):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    path = maintenance.backup_dir(get_settings()) / name
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)


# ---- approval -------------------------------------------------------------


async def _review(request: Request, reservation_id: int, body: ReviewIn, admin: User, db: AsyncSession, approve: bool):
    r = await db.get(Reservation, reservation_id)
    if r is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    done = await review(db, admin, r, approve, body.note, body.series)
    event = "booking_approved" if approve else "booking_rejected"
    request.app.state.notifier.emit(event, user_id=r.user_id, reservation_id=r.id, count=len(done))
    request.app.state.lanes.poke()
    request.app.state.housekeeper.poke()
    return await queries.reservation_out(db, done, utcnow())


@router.post("/reservations/{reservation_id}/approve", response_model=list[ReservationOut])
async def approve_reservation(
    reservation_id: int,
    body: ReviewIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await _review(request, reservation_id, body, admin, db, approve=True)


@router.post("/reservations/{reservation_id}/reject", response_model=list[ReservationOut])
async def reject_reservation(
    reservation_id: int,
    body: ReviewIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await _review(request, reservation_id, body, admin, db, approve=False)
