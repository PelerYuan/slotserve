"""Approving and rejecting bookings that wait for an admin (approval mode)."""

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..errors import ApiError
from ..orm import Reservation, User
from .audit import record


async def review(
    db: AsyncSession, admin: User, r: Reservation, approve: bool, note: str = "", series: bool = False
) -> list[Reservation]:
    """Approves or rejects a pending booking (and, with ``series``, the rest of its pending series). Commits."""
    if r.status != "pending":
        raise ApiError(status.HTTP_409_CONFLICT, "not_pending")
    now = utcnow()
    targets = [r]
    if series and r.series_id:
        rest = await db.execute(
            select(Reservation).where(
                Reservation.series_id == r.series_id, Reservation.id != r.id, Reservation.status == "pending"
            )
        )
        targets += list(rest.scalars())
    targets = [t for t in targets if t.end_at > now]
    if not targets:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "reservation_finished")
    for t in targets:
        t.status = "booked" if approve else "rejected"
        t.reviewed_by = admin.username
        t.review_note = note.strip() or None
    owner = await db.get(User, r.user_id)
    record(
        db,
        admin,
        "reservation.approve" if approve else "reservation.reject",
        f"#{r.id} {owner.username if owner else '?'}",
        count=len(targets),
        note=note.strip() or None,
    )
    await db.commit()
    return targets
