"""Booking operations shared by the web API and the chat bots (A12 step 2).

The HTTP layer (``api/reservations.py``) and ``services/chat`` call these; they check the rules in
``services/booking.py`` (windows, conflicts, quota, approval), store the change and write the audit entry. The
caller sends the notifications that depend on the request.
"""

import uuid
from datetime import timedelta

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import utcnow
from ..errors import ApiError
from ..orm import DevTemplate, Reservation, User
from ..schemas import ReservationCreateIn, ReservationUpdateIn
from . import booking, queries
from .audit import record


async def create_core(app, db: AsyncSession, user: User, body: ReservationCreateIn) -> list[Reservation]:
    """Checks and stores new bookings and tells whoever needs to know (shared by the web API and the bot)."""
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
    lane = await booking.resolve_lane(db, body.lane_id)
    if (body.devenv_template_id is not None or body.devenv_project_id is not None) and model is not None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "devenv_with_model")
    if body.devenv_project_id is not None:
        project = await booking.resolve_project(db, settings, owner, body.devenv_project_id, None, None)
        template = await db.get(DevTemplate, project.template_id) if project.template_id else None
    else:
        template = await booking.resolve_devenv(db, settings, body.devenv_template_id, owner)
        project = await booking.resolve_project(db, settings, owner, None, body.devenv_project_name, template)

    pending = booking.needs_approval(settings, user, windows)
    async with booking.booking_lock:
        await booking.raise_if_conflicts(db, windows, lane_id=lane.id)
        await booking.raise_if_project_busy(db, project.id if project else None, windows)
        if owner.id == user.id:
            await booking.check_quota(db, settings, owner, windows)
        series = str(uuid.uuid4()) if len(windows) > 1 else None
        created = [
            Reservation(
                user_id=owner.id,
                model_id=model.id if model else None,
                preset_id=preset.id if preset else None,
                series_id=series,
                lane_id=lane.id,
                devenv_template_id=template.id if template else None,
                devenv_project_id=project.id if project else None,
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

    notifier = app.state.notifier
    for r in created:
        if r.user_id != user.id:
            notifier.emit("booking_created", user_id=r.user_id, reservation_id=r.id, by=user.username)
    if pending:
        notifier.emit("approval_requested", reservation_id=created[0].id, owner_id=owner.id, count=len(created))
    app.state.lanes.poke(lane.id)
    return created


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
    locked = {"start_at", "model_id", "preset_id", "lane_id", "devenv_template_id", "devenv_project_id"}
    if phase == "active" and (fields & locked):
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

    lane_id = (await booking.resolve_lane(db, body.lane_id)).id if "lane_id" in fields else r.lane_id
    devenv_id, project_id = r.devenv_template_id, r.devenv_project_id
    if fields & {"devenv_template_id", "devenv_project_id"}:
        owner_for_template = await db.get(User, r.user_id)
        if body.devenv_project_id is not None:
            project = await booking.resolve_project(
                db, settings, owner_for_template, body.devenv_project_id, None, None
            )
            devenv_id, project_id = project.template_id, project.id
        else:
            template = await booking.resolve_devenv(db, settings, body.devenv_template_id, owner_for_template)
            devenv_id, project_id = (template.id if template else None), None
    if model_id is not None:
        devenv_id = project_id = None  # running a model and a dev environment are different kinds of booking
    # Changing a not-yet-started booking is judged like a new one in approval mode.
    review = phase != "active" and r.user_id == user.id and booking.needs_approval(settings, user, [(start, end)])
    newly_pending = review and r.status != "pending"
    async with booking.booking_lock:
        await booking.raise_if_conflicts(db, [(start, end)], exclude={r.id}, lane_id=lane_id)
        await booking.raise_if_project_busy(db, project_id, [(start, end)], exclude={r.id})
        owner = await db.get(User, r.user_id)
        if owner is not None and owner.id == user.id:
            await booking.check_quota(db, settings, owner, [(start, end)], exclude={r.id})
        r.start_at, r.end_at, r.model_id, r.preset_id, r.lane_id = start, end, model_id, preset_id, lane_id
        r.devenv_template_id, r.devenv_project_id = devenv_id, project_id
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


async def extend_core(db: AsyncSession, user: User, r: Reservation, minutes: int) -> Reservation:
    """Moves the end of a booking ``minutes`` later, with the same rules as an edit (commits)."""
    if queries.reservation_phase(r, utcnow()) not in ("active", "upcoming", "pending"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    body = ReservationUpdateIn(end_at=r.end_at + timedelta(minutes=minutes))
    r, _ = await update_reservation_core(db, user, r, body)
    return r


async def end_core(db: AsyncSession, user: User, r: Reservation) -> str:
    """Cancels an upcoming reservation or releases (ends early) an active one; returns the action.

    The caller commits, so a whole series ends in one transaction."""
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
