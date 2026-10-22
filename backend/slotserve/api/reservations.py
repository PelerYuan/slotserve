from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Reservation, User
from ..schemas import ExtendIn, ReservationCreateIn, ReservationOut, ReservationUpdateIn
from ..services import booking, queries
from ..services.reservations import create_core, end_core, extend_core, update_reservation_core

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


def after_change(request: Request) -> None:
    """Re-plan the GPUs and give waitlisted people a chance at freed time."""
    request.app.state.lanes.poke()
    request.app.state.housekeeper.poke()


def request_review(request: Request, r: Reservation, count: int = 1) -> None:
    request.app.state.notifier.emit("approval_requested", reservation_id=r.id, owner_id=r.user_id, count=count)


def notify_owner(request: Request, actor: User, r: Reservation, event: str) -> None:
    if r.user_id != actor.id:
        request.app.state.notifier.emit(event, user_id=r.user_id, reservation_id=r.id, by=actor.username)


@router.get("", response_model=list[ReservationOut])
async def list_reservations(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    mine: bool = False,
    include_cancelled: bool = False,
    lane_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    now = utcnow()
    start = booking.aware(start) if start else now - timedelta(days=7)
    end = booking.aware(end) if end else now + timedelta(days=30)
    stmt = select(Reservation).where(Reservation.end_at > start, Reservation.start_at < end)
    if not include_cancelled:
        stmt = stmt.where(Reservation.status.in_(HOLDING_STATUSES))
    if mine:
        stmt = stmt.where(Reservation.user_id == user.id)
    if lane_id is not None:
        stmt = stmt.where(Reservation.lane_id == lane_id)
    rows = (await db.execute(stmt.order_by(Reservation.start_at))).scalars().all()
    return await queries.reservation_out(db, list(rows), now)


@router.post("", response_model=list[ReservationOut], status_code=201)
async def create_reservation(
    body: ReservationCreateIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Creates one booking, or a weekly series when ``repeat_weeks`` > 0. Returns every booking created."""
    created = await create_core(request.app, db, user, body)
    return await queries.reservation_out(db, created, utcnow())


async def _editable(db: AsyncSession, reservation_id: int, user: User) -> Reservation:
    r = await db.get(Reservation, reservation_id)
    if r is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if r.user_id != user.id and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")
    return r


@router.patch("/{reservation_id}", response_model=ReservationOut)
async def update_reservation(
    reservation_id: int,
    body: ReservationUpdateIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await _editable(db, reservation_id, user)
    r, newly_pending = await update_reservation_core(db, user, r, body)
    notify_owner(request, user, r, "booking_changed")
    if newly_pending:
        request_review(request, r)
    after_change(request)
    return (await queries.reservation_out(db, [r], utcnow()))[0]


@router.post("/{reservation_id}/extend", response_model=ReservationOut)
async def extend_reservation(
    reservation_id: int,
    body: ExtendIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await _editable(db, reservation_id, user)
    r = await extend_core(db, user, r, body.minutes)
    notify_owner(request, user, r, "booking_changed")
    request.app.state.lanes.poke(r.lane_id)
    return (await queries.reservation_out(db, [r], utcnow()))[0]


@router.delete("/{reservation_id}", response_model=list[ReservationOut])
async def cancel_or_release(
    reservation_id: int,
    request: Request,
    series: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancels an upcoming reservation, or releases an active one. ``series=true`` also cancels the rest of
    its weekly series. Returns the affected bookings."""
    r = await _editable(db, reservation_id, user)
    targets = [r]
    if series and r.series_id:
        rest = await db.execute(
            select(Reservation).where(
                Reservation.series_id == r.series_id,
                Reservation.id != r.id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.start_at > utcnow(),
            )
        )
        targets += list(rest.scalars())
    actions = [await end_core(db, user, t) for t in targets]
    await db.commit()
    for t, action in zip(targets, actions, strict=True):
        notify_owner(request, user, t, "booking_cancelled" if action == "cancel" else "booking_released")
    after_change(request)
    return await queries.reservation_out(db, targets, utcnow())
