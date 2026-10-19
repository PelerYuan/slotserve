import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user, get_scheduler
from ..errors import ApiError
from ..orm import LlmModel, Reservation, User
from ..runtime.scheduler import Scheduler
from ..schemas import ReservationCreateIn, ReservationOut
from ..services import queries

router = APIRouter(prefix="/api/reservations", tags=["reservations"])

# Serialises the overlap check + insert (single-process deployment).
_booking_lock = asyncio.Lock()
CLOCK_SKEW = timedelta(minutes=2)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@router.get("", response_model=list[ReservationOut])
async def list_reservations(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    mine: bool = False,
    include_cancelled: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    now = utcnow()
    start = _aware(start) if start else now - timedelta(days=7)
    end = _aware(end) if end else now + timedelta(days=30)
    stmt = select(Reservation).where(Reservation.end_at > start, Reservation.start_at < end)
    if not include_cancelled:
        stmt = stmt.where(Reservation.status == "booked")
    if mine:
        stmt = stmt.where(Reservation.user_id == user.id)
    rows = (await db.execute(stmt.order_by(Reservation.start_at))).scalars().all()
    return await queries.reservation_out(db, list(rows), now)


@router.post("", response_model=ReservationOut, status_code=201)
async def create_reservation(
    body: ReservationCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    scheduler: Scheduler = Depends(get_scheduler),
):
    settings = get_settings()
    now = utcnow()
    start, end = _aware(body.start_at), _aware(body.end_at)
    slot = timedelta(minutes=settings.slot_minutes)

    if start < now - slot:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_in_past")
    start = max(start, now)  # a slot that already began starts "now"
    if end <= start + timedelta(minutes=5):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_short")
    if end - start > timedelta(hours=settings.max_reservation_hours) + CLOCK_SKEW:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_long", hours=settings.max_reservation_hours)
    if start > now + timedelta(days=settings.max_advance_days):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_far", days=settings.max_advance_days)

    model = await db.get(LlmModel, body.model_id)
    if model is None or model.deleted_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if model.status != "ready":
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")

    async with _booking_lock:
        clash = (
            await db.execute(
                select(Reservation)
                .where(Reservation.status == "booked", Reservation.start_at < end, Reservation.end_at > start)
                .limit(1)
            )
        ).scalar_one_or_none()
        if clash is not None:
            owner = await db.get(User, clash.user_id)
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "reservation_conflict",
                username=owner.username if owner else "?",
                start_at=clash.start_at.isoformat(),
                end_at=clash.end_at.isoformat(),
            )
        reservation = Reservation(
            user_id=user.id, model_id=model.id, start_at=start, end_at=end, note=body.note.strip()
        )
        db.add(reservation)
        await db.commit()

    scheduler.poke()
    return (await queries.reservation_out(db, [reservation], utcnow()))[0]


@router.delete("/{reservation_id}", response_model=ReservationOut)
async def cancel_or_release(
    reservation_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    scheduler: Scheduler = Depends(get_scheduler),
):
    """Cancels an upcoming reservation, or releases (ends early) an active one."""
    r = await db.get(Reservation, reservation_id)
    if r is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if r.user_id != user.id and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")
    now = utcnow()
    phase = queries.reservation_phase(r, now)
    if phase == "upcoming":
        r.status = "cancelled"
    elif phase == "active":
        r.end_at = now
        r.released_at = now
    else:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    await db.commit()
    scheduler.poke()
    return (await queries.reservation_out(db, [r], now))[0]
