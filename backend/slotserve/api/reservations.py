import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Reservation, User
from ..schemas import ExtendIn, ReservationCreateIn, ReservationOut, ReservationUpdateIn
from ..services import booking, queries
from ..services.audit import record

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


def after_change(request: Request) -> None:
    """Re-plan the GPU and give waitlisted people a chance at freed time."""
    request.app.state.scheduler.poke()
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
    settings = get_settings()
    owner = user
    if body.user_id is not None and body.user_id != user.id:
        if not user.is_admin:
            raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
        owner = await db.get(User, body.user_id)
        if owner is None or owner.disabled_at is not None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")

    now = utcnow()
    start, end = booking.check_window(settings, body.start_at, body.end_at, now)
    windows = booking.series_windows(start, end, body.repeat_weeks)
    if windows[-1][0] > now + timedelta(days=settings.max_advance_days):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_far", days=settings.max_advance_days)
    model, preset = await booking.resolve_model(db, body.model_id, body.preset_id)

    pending = booking.needs_approval(settings, user, windows)
    async with booking.booking_lock:
        await booking.raise_if_conflicts(db, windows)
        if owner.id == user.id:
            await booking.check_quota(db, settings, owner, windows)
        series = str(uuid.uuid4()) if len(windows) > 1 else None
        created = [
            Reservation(
                user_id=owner.id,
                model_id=model.id if model else None,
                preset_id=preset.id if preset else None,
                series_id=series,
                start_at=s,
                end_at=e,
                note=body.note.strip(),
                status="pending" if pending else "booked",
            )
            for s, e in windows
        ]
        db.add_all(created)
        await db.flush()
        if owner.id != user.id:
            record(db, user, "reservation.create_for", f"#{created[0].id} {owner.username}", count=len(created))
        await db.commit()

    for r in created:
        notify_owner(request, user, r, "booking_created")
    if pending:
        request_review(request, created[0], len(created))
    request.app.state.scheduler.poke()
    return await queries.reservation_out(db, created, utcnow())


async def _editable(db: AsyncSession, reservation_id: int, user: User) -> Reservation:
    r = await db.get(Reservation, reservation_id)
    if r is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if r.user_id != user.id and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")
    return r


async def update_reservation_core(
    db: AsyncSession, user: User, r: Reservation, body: ReservationUpdateIn
) -> tuple[Reservation, bool]:
    """Applies an edit with the same rules as booking. Returns (reservation, needs_review)."""
    settings = get_settings()
    now = utcnow()
    phase = queries.reservation_phase(r, now)
    fields = body.model_fields_set
    if phase not in ("upcoming", "active", "pending"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    if phase == "active" and (fields & {"start_at", "model_id", "preset_id"}):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_active_locked")

    start = booking.aware(body.start_at) if body.start_at else r.start_at
    end = booking.aware(body.end_at) if body.end_at else r.end_at
    if phase in ("upcoming", "pending") and ("start_at" in fields or "end_at" in fields):
        start, end = booking.check_window(settings, start, end, now)
    elif phase == "active" and "end_at" in fields:
        if end <= now + timedelta(minutes=1):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_too_short")
        booking.check_length(settings, start, end)

    model_id = body.model_id if "model_id" in fields else r.model_id
    preset_id = body.preset_id if ("preset_id" in fields or "model_id" in fields) else r.preset_id
    if fields & {"model_id", "preset_id"}:
        model, preset = await booking.resolve_model(db, model_id, preset_id)
        model_id, preset_id = (model.id if model else None), (preset.id if preset else None)

    # Changing a not-yet-started booking is judged like a new one in approval mode.
    review = phase != "active" and r.user_id == user.id and booking.needs_approval(settings, user, [(start, end)])
    newly_pending = review and r.status != "pending"
    async with booking.booking_lock:
        await booking.raise_if_conflicts(db, [(start, end)], exclude={r.id})
        owner = await db.get(User, r.user_id)
        if owner is not None and owner.id == user.id:
            await booking.check_quota(db, settings, owner, [(start, end)], exclude={r.id})
        r.start_at, r.end_at, r.model_id, r.preset_id = start, end, model_id, preset_id
        if body.note is not None:
            r.note = body.note.strip()
        if review:
            r.status = "pending"
        if r.user_id != user.id:
            record(
                db,
                user,
                "reservation.update",
                f"#{r.id} {owner.username if owner else '?'}",
                start_at=start,
                end_at=end,
            )
        await db.commit()
    return r, newly_pending


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


async def extend_core(db: AsyncSession, user: User, r: Reservation, minutes: int) -> Reservation:
    if queries.reservation_phase(r, utcnow()) not in ("active", "upcoming", "pending"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    body = ReservationUpdateIn(end_at=r.end_at + timedelta(minutes=minutes))
    r, _ = await update_reservation_core(db, user, r, body)
    return r


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
    request.app.state.scheduler.poke()
    return (await queries.reservation_out(db, [r], utcnow()))[0]


async def end_core(db: AsyncSession, user: User, r: Reservation) -> str:
    """Cancels an upcoming reservation or releases (ends early) an active one; returns the action."""
    now = utcnow()
    phase = queries.reservation_phase(r, now)
    if phase in ("upcoming", "pending"):
        r.status = "cancelled"
    elif phase == "active":
        r.end_at = now
        r.released_at = now
    else:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    action = "release" if phase == "active" else "cancel"
    if r.user_id != user.id:
        owner = await db.get(User, r.user_id)
        record(db, user, f"reservation.{action}", f"#{r.id} {owner.username if owner else '?'}", start_at=r.start_at)
    return action


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
