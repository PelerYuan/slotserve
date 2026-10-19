"""Booking rules shared by the web API, the waitlist and the Telegram bot."""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import utcnow
from ..errors import ApiError
from ..orm import LlmModel, ModelPreset, Reservation, User

# Serialises every overlap check + write (single-process deployment).
booking_lock = asyncio.Lock()
CLOCK_SKEW = timedelta(minutes=2)
MIN_LENGTH = timedelta(minutes=5)

Window = tuple[datetime, datetime]


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


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


async def find_conflict(db: AsyncSession, start: datetime, end: datetime, exclude: set[int] = frozenset()):
    stmt = select(Reservation).where(
        Reservation.status == "booked", Reservation.start_at < end, Reservation.end_at > start
    )
    if exclude:
        stmt = stmt.where(Reservation.id.not_in(exclude))
    return (await db.execute(stmt.order_by(Reservation.start_at).limit(1))).scalar_one_or_none()


async def raise_if_conflicts(
    db: AsyncSession, windows: list[Window], exclude: set[int] = frozenset()
) -> None:
    clashes = [c for start, end in windows if (c := await find_conflict(db, start, end, exclude)) is not None]
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
    return monday.astimezone(timezone.utc)


async def check_quota(
    db: AsyncSession, settings: Settings, user: User, windows: list[Window], exclude: set[int] = frozenset()
) -> None:
    """Per-person limits; admins are exempt."""
    if user.is_admin:
        return
    now = utcnow()
    if settings.quota_upcoming:
        stmt = select(Reservation.id).where(
            Reservation.user_id == user.id, Reservation.status == "booked", Reservation.end_at > now
        )
        existing = [rid for rid in (await db.execute(stmt)).scalars() if rid not in exclude]
        if len(existing) + len(windows) > settings.quota_upcoming:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "quota_upcoming", limit=settings.quota_upcoming)
    if settings.quota_weekly_hours:
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("UTC")
        weeks: dict[datetime, float] = defaultdict(float)
        for start, end in windows:
            weeks[_week_start(start, tz)] += (end - start).total_seconds() / 3600
        for week, hours in weeks.items():
            week_end = week + timedelta(days=7)
            rows = await db.execute(
                select(Reservation.id, Reservation.start_at, Reservation.end_at).where(
                    Reservation.user_id == user.id,
                    Reservation.status == "booked",
                    Reservation.start_at < week_end,
                    Reservation.end_at > week,
                )
            )
            for rid, s, e in rows.all():
                if rid not in exclude:
                    hours += (min(e, week_end) - max(s, week)).total_seconds() / 3600
            if hours > settings.quota_weekly_hours + 0.01:
                raise ApiError(status.HTTP_400_BAD_REQUEST, "quota_weekly", limit=settings.quota_weekly_hours)


def series_windows(start: datetime, end: datetime, repeat_weeks: int) -> list[Window]:
    return [(start + timedelta(weeks=i), end + timedelta(weeks=i)) for i in range(repeat_weeks + 1)]
