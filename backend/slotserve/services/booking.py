"""Booking rules shared by the web API, the waitlist and the Telegram bot."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import utcnow
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Lane, LlmModel, ModelPreset, Reservation, User
from . import policies

# Serialises every overlap check + write (single-process deployment).
booking_lock = asyncio.Lock()
CLOCK_SKEW = timedelta(minutes=2)
MIN_LENGTH = timedelta(minutes=5)

Window = tuple[datetime, datetime]


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def resolve_model(
    db: AsyncSession, model_id: int | None, preset_id: int | None
) -> tuple[LlmModel | None, ModelPreset | None]:
    """``model_id`` ``None`` means an exclusive booking (no model, no preset)."""
    if model_id is None:
        if preset_id is not None:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "preset_without_model")
        return None, None
    model = await db.get(LlmModel, model_id)
    if model is None or model.deleted_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if model.status != "ready":
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")
    preset = None
    if preset_id is not None:
        preset = await db.get(ModelPreset, preset_id)
        if preset is None or preset.model_id != model.id:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return model, preset


def check_window(settings: Settings, start: datetime, end: datetime, now: datetime) -> Window:
    """Validates a new (not yet started) booking window; a start in the current slot snaps to now."""
    start, end = aware(start), aware(end)
    if start < now - timedelta(minutes=settings.slot_minutes):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_in_past")
    start = max(start, now)
    if end <= start + MIN_LENGTH:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_short")
    check_length(settings, start, end)
    if start > now + timedelta(days=settings.max_advance_days):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_far", days=settings.max_advance_days)
    return start, end


def check_length(settings: Settings, start: datetime, end: datetime) -> None:
    if end - start > timedelta(hours=settings.max_reservation_hours) + CLOCK_SKEW:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_long", hours=settings.max_reservation_hours)


async def resolve_lane(db: AsyncSession, lane_id: int | None) -> Lane:
    """The lane a booking goes to (``None``: the first lane)."""
    if lane_id is None:
        lane = (await db.execute(select(Lane).order_by(Lane.position, Lane.id).limit(1))).scalar_one_or_none()
    else:
        lane = await db.get(Lane, lane_id)
    if lane is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return lane


async def find_conflict(
    db: AsyncSession, start: datetime, end: datetime, exclude: set[int] = frozenset(), lane_id: int | None = None
):
    """An overlapping booking on the same lane (any lane when ``lane_id`` is ``None``)."""
    stmt = select(Reservation).where(
        Reservation.status.in_(HOLDING_STATUSES), Reservation.start_at < end, Reservation.end_at > start
    )
    if lane_id is not None:
        stmt = stmt.where(Reservation.lane_id == lane_id)
    if exclude:
        stmt = stmt.where(Reservation.id.not_in(exclude))
    return (await db.execute(stmt.order_by(Reservation.start_at).limit(1))).scalar_one_or_none()


async def raise_if_conflicts(
    db: AsyncSession, windows: list[Window], exclude: set[int] = frozenset(), lane_id: int | None = None
) -> None:
    clashes = [c for start, end in windows if (c := await find_conflict(db, start, end, exclude, lane_id)) is not None]
    if clashes:
        first = clashes[0]
        owner = await db.get(User, first.user_id)
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "reservation_conflict",
            username=owner.username if owner else "?",
            start_at=first.start_at.isoformat(),
            end_at=first.end_at.isoformat(),
            count=len(clashes),
        )


def _week_start(dt: datetime, tz: ZoneInfo) -> datetime:
    local = dt.astimezone(tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(UTC)


def quota_tz(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def week_bounds(settings: Settings, at: datetime) -> Window:
    """The quota week (Monday 00:00 in the configured time zone) that contains ``at``."""
    start = _week_start(at, quota_tz(settings))
    return start, start + timedelta(days=7)


async def check_quota(
    db: AsyncSession, settings: Settings, user: User, windows: list[Window], exclude: set[int] = frozenset()
) -> None:
    """Per-person limits; admins are exempt."""
    if user.is_admin:
        return
    now = utcnow()
    if settings.quota_upcoming:
        stmt = select(Reservation.id).where(
            Reservation.user_id == user.id, Reservation.status.in_(HOLDING_STATUSES), Reservation.end_at > now
        )
        existing = [rid for rid in (await db.execute(stmt)).scalars() if rid not in exclude]
        if len(existing) + len(windows) > settings.quota_upcoming:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "quota_upcoming", limit=settings.quota_upcoming)
    if settings.quota_weekly_hours:
        tz = quota_tz(settings)
        grants = await policies.grants(db, user.id)  # temporary extra hours an admin gave this person
        weeks: dict[datetime, float] = defaultdict(float)
        for start, end in windows:
            weeks[_week_start(start, tz)] += (end - start).total_seconds() / 3600
        for week, hours in weeks.items():
            week_end = week + timedelta(days=7)
            rows = await db.execute(
                select(Reservation.id, Reservation.start_at, Reservation.end_at).where(
                    Reservation.user_id == user.id,
                    Reservation.status.in_(HOLDING_STATUSES),
                    Reservation.start_at < week_end,
                    Reservation.end_at > week,
                )
            )
            for rid, s, e in rows.all():
                if rid not in exclude:
                    hours += (min(e, week_end) - max(s, week)).total_seconds() / 3600
            limit = settings.quota_weekly_hours + policies.extra_hours(grants, week, week_end)
            if hours > limit + 0.01:
                raise ApiError(
                    status.HTTP_400_BAD_REQUEST,
                    "quota_weekly",
                    limit=int(limit) if float(limit).is_integer() else round(limit, 2),
                )


def series_windows(start: datetime, end: datetime, repeat_weeks: int) -> list[Window]:
    return [(start + timedelta(weeks=i), end + timedelta(weeks=i)) for i in range(repeat_weeks + 1)]


def needs_approval(settings: Settings, actor: User, windows: list[Window]) -> bool:
    """Whether bookings made by ``actor`` must wait for an admin. Admins (also when booking for others) never do."""
    if actor.is_admin or settings.approval_mode == "off":
        return False
    if settings.approval_mode == "all":
        return True
    limit = timedelta(hours=settings.approval_hours)
    return any(end - start > limit for start, end in windows)


async def free_windows(db: AsyncSession, lane_id: int, start: datetime, end: datetime) -> list[Window]:
    """Gaps between holding bookings on a lane within [start, end)."""
    rows = (
        await db.execute(
            select(Reservation.start_at, Reservation.end_at)
            .where(
                Reservation.lane_id == lane_id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
            .order_by(Reservation.start_at)
        )
    ).all()
    gaps: list[Window] = []
    cursor = start
    for s, e in rows:
        s, e = aware(s), aware(e)
        if s > cursor:
            gaps.append((cursor, min(s, end)))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


async def check_devenv_login(db: AsyncSession, settings: Settings, owner: User, template=None) -> None:
    """A dev environment needs a way in: an SSH key, or an environment password when password login is on - unless
    its template has a web interface (used in the browser, no credentials needed)."""
    from ..containers.contributors.ssh import needs_no_login
    from ..containers.names import load_web_ports
    from ..orm import SshKey

    if template is not None and needs_no_login(load_web_ports(template.web_ports)):
        return
    if settings.devenv_password_login and owner.devenv_password_hash:
        return
    if (await db.execute(select(SshKey.id).where(SshKey.user_id == owner.id).limit(1))).first() is None:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, "ssh_login_required" if settings.devenv_password_login else "ssh_key_required"
        )


async def resolve_devenv(db: AsyncSession, settings: Settings, template_id: int | None, owner: User):
    """The template of a dev environment booking, checked for the person it is for."""
    from ..orm import DevTemplate
    from .devenv import visible

    if template_id is None:
        return None
    if not settings.devenv_enabled:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_disabled")
    template = await db.get(DevTemplate, template_id)
    if not visible(template, owner):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await check_devenv_login(db, settings, owner, template)
    return template


async def resolve_project(
    db: AsyncSession, settings: Settings, owner: User, project_id: int | None, new_name: str | None, template
):
    """The kept environment a dev environment booking continues, or a new one started from ``template``.

    A new project is added to the session (not committed): it is saved together with the booking.
    """
    from ..orm import DevProject

    if project_id is not None:
        if not settings.devenv_enabled:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_disabled")
        project = await db.get(DevProject, project_id)
        if project is None or project.owner_id != owner.id:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        from ..orm import DevTemplate

        template = await db.get(DevTemplate, project.template_id) if project.template_id else None
        await check_devenv_login(db, settings, owner, template)
        return project
    if not new_name or not new_name.strip():
        return None
    if template is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_project_template")
    count = (await db.execute(select(func.count(DevProject.id)).where(DevProject.owner_id == owner.id))).scalar_one()
    if count >= settings.devenv_projects:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_project_limit", limit=settings.devenv_projects)
    project = DevProject(
        owner_id=owner.id,
        name=new_name.strip()[:64],
        template_id=template.id,
        image=template.image,
        persist_paths=template.persist_paths,
        state="new",
    )
    db.add(project)
    await db.flush()
    return project


async def raise_if_project_busy(
    db: AsyncSession, project_id: int | None, windows: list[Window], exclude: set[int] = frozenset()
) -> None:
    """One kept environment cannot run twice at the same time (e.g. booked on two lanes)."""
    if project_id is None:
        return
    for start, end in windows:
        stmt = select(Reservation.id).where(
            Reservation.devenv_project_id == project_id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.start_at < end,
            Reservation.end_at > start,
        )
        if exclude:
            stmt = stmt.where(Reservation.id.not_in(exclude))
        if (await db.execute(stmt.limit(1))).first() is not None:
            raise ApiError(status.HTTP_409_CONFLICT, "devenv_project_busy")
