import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user, get_lanes, sudo_admin
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, ApiKey, AuditLog, LlmModel, ModelLoadEvent, Reservation, UsageLog, User
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
    QuotaGrantIn,
    QuotaGrantOut,
    RequestLogOut,
    RequestPageOut,
    ReservationOut,
    ReviewIn,
    SettingChangeOut,
    SettingOut,
    SettingsHistoryOut,
    SettingsPreviewOut,
    UserPolicyOut,
)
from ..security import hash_password, sessions, sudo
from ..services import app_settings, booking, kv, maintenance, policies, queries
from ..services.audit import AUDIT_ACTIONS, record, split_detail
from ..services.review import review
from .runtime_admin import load_event_diagnosis

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
                diagnosis=load_event_diagnosis(e),
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
            await sessions.revoke_user(db, user.id, "disabled")
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
    await sessions.revoke_user(db, user.id, "password")
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
    await policies.delete_all(db, user.id)
    await db.delete(user)
    await db.commit()
    lanes.poke()


# ---- per-person limits (quota grants) -------------------------------------


def _grant_out(g: policies.Grant) -> QuotaGrantOut:
    return QuotaGrantOut(**{k: v for k, v in g.__dict__.items()}, active=g.active(utcnow()))


@router.get("/users/{user_id}/policy", response_model=UserPolicyOut)
async def user_policy(user_id: int, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """The limits a person books under now: the global settings plus temporary extra hours."""
    user = await _get_user(db, user_id)
    settings = get_settings()
    now = utcnow()
    limits = await policies.policy_for(db, settings, user, now)
    week_start, week_end = booking.week_bounds(settings, now)
    rows = (
        await db.execute(
            select(Reservation.start_at, Reservation.end_at).where(
                Reservation.user_id == user.id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.start_at < week_end,
                Reservation.end_at > week_start,
            )
        )
    ).all()
    booked = sum(
        (min(booking.aware(e), week_end) - max(booking.aware(s), week_start)).total_seconds() / 3600 for s, e in rows
    )
    open_bookings = (
        await db.execute(
            select(func.count(Reservation.id)).where(
                Reservation.user_id == user.id, Reservation.status.in_(HOLDING_STATUSES), Reservation.end_at > now
            )
        )
    ).scalar_one()
    return UserPolicyOut(
        user_id=user.id,
        username=user.username,
        exempt=limits.exempt,
        quota_weekly_hours=limits.quota_weekly_hours,
        base_weekly_hours=limits.base_weekly_hours,
        extra_hours=limits.extra_hours,
        quota_upcoming=limits.quota_upcoming,
        max_reservation_hours=limits.max_reservation_hours,
        max_advance_days=limits.max_advance_days,
        week_start=week_start,
        booked_hours_this_week=round(booked, 2),
        open_bookings=open_bookings,
        grants=[_grant_out(g) for g in limits.grants],
    )


@router.post("/users/{user_id}/quota-grants", response_model=QuotaGrantOut, status_code=201)
async def add_quota_grant(
    user_id: int,
    body: QuotaGrantIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Extra weekly hours for one person until ``until`` (e.g. 10 more hours this week for a thesis defence)."""
    user = await _get_user(db, user_id)
    now = utcnow()
    starts_at = booking.aware(body.starts_at) if body.starts_at else now
    until = booking.aware(body.until)
    if until <= max(now, starts_at):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "grant_until_past")
    grant = await policies.add_grant(db, user, body.extra_hours, until, body.reason, admin, starts_at)
    record(
        db,
        admin,
        "user.quota_grant",
        user.username,
        ip=request,
        extra_hours=grant.extra_hours,
        until=grant.until,
        reason=grant.reason,
    )
    await db.commit()
    return _grant_out(grant)


@router.delete("/users/{user_id}/quota-grants/{grant_id}", status_code=204)
async def revoke_quota_grant(
    user_id: int,
    grant_id: str,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, user_id)
    grant = await policies.revoke_grant(db, user, grant_id, admin)
    if grant is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    record(db, admin, "user.quota_revoke", user.username, ip=request, extra_hours=grant.extra_hours, until=grant.until)
    await db.commit()


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

SETTINGS_ACTIONS = ("settings.update", "settings.sensitive_update", "settings.undo")
_ORDER = {key: i for i, key in enumerate(app_settings.REGISTRY)}


def _setting_out(key: str, defaults: dict, overridden: set[str], *, mask: bool) -> SettingOut:
    spec = app_settings.REGISTRY[key]
    value = app_settings.plain(getattr(get_settings(), key))
    default = defaults.get(key)
    masked = spec.secret and (mask or spec.restart_required)
    return SettingOut(
        key=key,
        value="" if masked else value,
        default="" if masked else default,
        overridden=key in overridden,
        secret=spec.secret,
        kind=spec.kind,
        unit=spec.unit,
        min=spec.min,
        max=spec.max,
        choices=list(spec.choices) if spec.choices else None,
        env_var=app_settings.env_var(key),
        group=spec.group,
        card=spec.card,
        tier=spec.tier,
        order=_ORDER[key],
        depends_on=spec.depends_on,
        keywords=list(spec.keywords),
        scope=spec.scope,
        danger=spec.danger,
        restart_required=spec.restart_required,
        has_impact=spec.impact is not None,
        has_value=value not in (None, ""),
        source=app_settings.source(key, defaults, overridden),
    )


@router.get("/settings", response_model=list[SettingOut])
async def get_settings_view(
    request: Request,
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    include_deployment: bool = False,
    mask_secrets: bool = False,
):
    """Every editable setting with its value and registry metadata. ``include_deployment=true`` adds the
    read-only deployment parameters (``restart_required``; their secrets are always masked);
    ``mask_secrets=true`` returns ``""`` instead of secret values (``has_value`` tells whether one is set)."""
    defaults = request.app.state.settings_defaults
    overridden = await app_settings.overridden_keys(db)
    keys = app_settings.REGISTRY if include_deployment else app_settings.EDITABLE
    return [_setting_out(key, defaults, overridden, mask=mask_secrets) for key in keys]


def _change_out(change: app_settings.Change, secret_key: str) -> SettingChangeOut:
    spec = app_settings.EDITABLE[change.key]
    return SettingChangeOut(
        key=change.key,
        before=app_settings.audit_value(change.key, change.before, secret_key),
        after=app_settings.audit_value(change.key, change.after, secret_key),
        reset=change.reset,
        secret=spec.secret,
        danger=spec.danger,
    )


def _audit_settings(
    db: AsyncSession, admin: User, request: Request, planned: list[app_settings.Change], undo_of: int | None
) -> None:
    """One entry for ordinary settings and one ``settings.sensitive_update`` for root-equivalent ones, each with
    before/after (secrets as fingerprints), the keys reset to their default and the keys that had no override."""
    secret_key = get_settings().secret_key
    danger = [c for c in planned if app_settings.EDITABLE[c.key].danger]
    ordinary = [c for c in planned if not app_settings.EDITABLE[c.key].danger]
    for action, changes in (
        ("settings.undo" if undo_of else "settings.update", ordinary),
        ("settings.sensitive_update", danger),
    ):
        if not changes:
            continue
        detail: dict = {
            "before": {c.key: app_settings.audit_value(c.key, c.before, secret_key) for c in changes},
            "after": {c.key: app_settings.audit_value(c.key, c.after, secret_key) for c in changes},
            "reset": [c.key for c in changes if c.reset],
            "was_default": [c.key for c in changes if c.was_default],
        }
        if undo_of:
            detail["undo_of"] = undo_of
        record(db, admin, action, ", ".join(c.key for c in changes), ip=request, **detail)


async def _save_settings(
    request: Request,
    admin: User,
    db: AsyncSession,
    changes: dict,
    *,
    dry_run: bool,
    notify: bool,
    undo_of: int | None = None,
):
    settings = get_settings()
    try:
        planned = await app_settings.plan(db, settings, request.app.state.settings_defaults, changes)
    except app_settings.InvalidSetting as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_setting", key=exc.key, reason=exc.reason) from exc
    danger = [c.key for c in planned if app_settings.EDITABLE[c.key].danger]
    needs_sudo = bool(danger) and app_settings.ENFORCE_SUDO_FOR_DANGER
    impacts = await app_settings.dry_run(db, settings, planned) if (dry_run or notify) else []
    notify_ids = sorted({u["user_id"] for impact in impacts for u in impact["users"]})
    if dry_run:
        return SettingsPreviewOut(
            changes=[_change_out(c, settings.secret_key) for c in planned],
            impacts=impacts,
            sudo_required=needs_sudo and not sudo.is_active(request, admin.id),
            notify_user_ids=notify_ids,
        )
    if needs_sudo:
        await sudo_admin(request, admin)  # 403 sudo_required unless the password was entered recently
    await app_settings.store(db, settings, planned)
    _audit_settings(db, admin, request, planned, undo_of)
    keys = {c.key for c in planned}
    if "api_key_reveal" in keys and not settings.api_key_reveal:
        # Off means off: no copy of any key stays readable.
        await db.execute(update(ApiKey).values(key_encrypted=None))
    await db.commit()
    if keys:
        request.app.state.channels.reload_for(keys)
    notifier = request.app.state.notifier
    if danger:
        notifier.emit("settings_sensitive", by=admin.username, keys=", ".join(danger))
    if notify:
        after = {c.key: c.after for c in planned}
        for impact in impacts:
            for u in impact["users"]:
                notifier.emit(f"policy_{impact['key']}", user_id=u["user_id"], after=after.get(impact["key"], ""))
    return await get_settings_view(request, admin, db)


@router.put("/settings", response_model=list[SettingOut] | SettingsPreviewOut)
async def put_settings(
    changes: dict[str, bool | int | float | str | None],
    request: Request,
    dry_run: bool = False,
    notify: bool = False,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Body maps setting keys to new values; ``null`` restores the ``.env`` default.

    ``dry_run=true`` stores nothing and returns ``SettingsPreviewOut`` (changes with before/after and the impact
    on people and bookings). ``notify=true`` messages the people a new booking limit affects. Existing bookings
    are never cancelled by a settings change. Root-equivalent settings need sudo mode once
    ``ENFORCE_SUDO_FOR_DANGER`` is on (403 ``sudo_required``)."""
    return await _save_settings(request, admin, db, changes, dry_run=dry_run, notify=notify)


@router.get("/settings/{key}/history", response_model=list[SettingsHistoryOut])
async def setting_history(
    key: str,
    limit: int = Query(default=20, ge=1, le=200),
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Who changed ``key`` when, from what to what (newest first); secrets show fingerprints only."""
    spec = app_settings.EDITABLE.get(key)
    if spec is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action.in_(SETTINGS_ACTIONS), AuditLog.detail.contains(f'"{key}"'))
                .order_by(AuditLog.id.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        detail, _ip = split_detail(row.detail)
        before, after = detail.get("before"), detail.get("after")
        if not isinstance(before, dict) or key not in before:
            continue
        out.append(
            SettingsHistoryOut(
                audit_id=row.id,
                action=row.action,
                actor_name=row.actor_name,
                created_at=row.created_at,
                before=before[key],
                after=(after or {}).get(key),
                reset=key in (detail.get("reset") or []),
                undoable=not spec.secret,
            )
        )
        if len(out) >= limit:
            break
    return out


@router.post("/audit/{audit_id}/undo", response_model=list[SettingOut] | SettingsPreviewOut)
async def undo_settings_change(
    audit_id: int,
    request: Request,
    dry_run: bool = False,
    notify: bool = False,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Puts the settings of one audit entry back to their ``before`` values, through the same preview, sudo and
    audit rules as a save (the entry it writes is ``settings.undo`` with ``undo_of``). Secrets cannot be undone
    (they are only fingerprinted): they are left out, the admin enters them again."""
    row = await db.get(AuditLog, audit_id)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    detail, _ip = split_detail(row.detail)
    before = detail.get("before")
    if row.action not in SETTINGS_ACTIONS or not isinstance(before, dict):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "not_undoable")
    was_default = set(detail.get("was_default") or [])
    changes = {
        key: (None if key in was_default else value)
        for key, value in before.items()
        if key in app_settings.EDITABLE and not app_settings.EDITABLE[key].secret
    }
    if not changes:
        raise ApiError(status.HTTP_409_CONFLICT, "nothing_to_undo")
    return await _save_settings(request, admin, db, changes, dry_run=dry_run, notify=notify, undo_of=row.id)


# ---- audit log ------------------------------------------------------------


def _audit_query(
    actor_id: int | None, action: str | None, target: str | None, since: datetime | None, until: datetime | None
):
    stmt = select(AuditLog)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action:
        names = [a.strip() for a in action.split(",") if a.strip()]
        exact = [a for a in names if not a.endswith("*")]
        prefixes = [a[:-1] for a in names if a.endswith("*")]
        stmt = stmt.where(or_(AuditLog.action.in_(exact), *(AuditLog.action.startswith(p) for p in prefixes)))
    if target:
        stmt = stmt.where(AuditLog.target.contains(target))
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= booking.aware(since))
    if until is not None:
        stmt = stmt.where(AuditLog.created_at < booking.aware(until))
    return stmt


def _audit_out(r: AuditLog) -> AuditOut:
    detail, ip = split_detail(r.detail)
    return AuditOut(
        id=r.id,
        actor_id=r.actor_id,
        actor_name=r.actor_name,
        action=r.action,
        target=r.target,
        detail=detail,
        created_at=r.created_at,
        ip=ip,
    )


@router.get("/audit", response_model=AuditPageOut)
async def audit_log(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor_id: int | None = None,
    action: str | None = Query(default=None, max_length=500),
    target: str | None = Query(default=None, max_length=160),
    since: datetime | None = None,
    until: datetime | None = None,
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest first. ``action`` takes exact names separated by commas and ``domain.*`` prefixes; ``target``
    matches a part of the target."""
    stmt = _audit_query(actor_id, action, target, since, until)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(AuditLog.id.desc()).offset(offset).limit(limit))).scalars().all()
    return AuditPageOut(total=total, items=[_audit_out(r) for r in rows])


@router.get("/audit/actions", response_model=list[str])
async def audit_actions(_: User = Depends(admin_user)):
    """Every action name the server records (for the filter)."""
    return sorted(AUDIT_ACTIONS)


@router.get("/audit.csv")
async def audit_csv(
    actor_id: int | None = None,
    action: str | None = Query(default=None, max_length=500),
    target: str | None = Query(default=None, max_length=160),
    since: datetime | None = None,
    until: datetime | None = None,
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The filtered audit log as CSV (at most 50 000 rows, newest first)."""
    stmt = _audit_query(actor_id, action, target, since, until).order_by(AuditLog.id.desc()).limit(50_000)
    rows = (await db.execute(stmt)).scalars().all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "time", "actor", "action", "target", "ip", "detail"])
    for r in rows:
        out = _audit_out(r)
        detail = json.dumps(out.detail, ensure_ascii=False, default=str)
        writer.writerow(
            [
                out.id,
                out.created_at.isoformat(),
                _cell(out.actor_name),
                out.action,
                _cell(out.target),
                out.ip,
                _cell(detail),
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="slotserve-audit.csv"'},
    )


def _cell(text: str) -> str:
    """Keeps spreadsheets from reading a cell as a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


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
    source: str | None = Query(default=None, pattern="^(api|env|playground)$"),
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(UsageLog)
    count = select(func.count(UsageLog.id))
    if user_id is not None:
        stmt, count = stmt.where(UsageLog.user_id == user_id), count.where(UsageLog.user_id == user_id)
    if source is not None:
        stmt, count = stmt.where(UsageLog.source == source), count.where(UsageLog.source == source)
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
                source=u.source,
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
async def download_backup(
    name: str, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    if not _BACKUP_NAME.match(name):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    path = maintenance.backup_dir(get_settings()) / name
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    # A backup holds every account and the stored chat secrets: who took one is worth knowing.
    record(db, admin, "backup.download", name, ip=request)
    await db.commit()
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
