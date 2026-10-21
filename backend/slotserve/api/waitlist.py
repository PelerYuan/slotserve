from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import Lane, LlmModel, ModelPreset, User, WaitlistEntry
from ..schemas import WaitlistIn, WaitlistOut
from ..services import booking

router = APIRouter(prefix="/api/waitlist", tags=["waitlist"])

OPEN = ("waiting", "notified")


async def _out(db: AsyncSession, rows: list[WaitlistEntry]) -> list[WaitlistOut]:
    out = []
    for e in rows:
        user = await db.get(User, e.user_id)
        model = await db.get(LlmModel, e.model_id) if e.model_id else None
        preset = await db.get(ModelPreset, e.preset_id) if e.preset_id else None
        lane = await db.get(Lane, e.lane_id) if e.lane_id else None
        out.append(
            WaitlistOut(
                id=e.id,
                lane_id=e.lane_id,
                lane_name=lane.name if lane else None,
                user_id=e.user_id,
                username=user.username if user else "?",
                model_id=e.model_id,
                model_name=model.name if model else None,
                preset_name=preset.name if preset else None,
                exclusive=e.model_id is None,
                start_at=e.start_at,
                end_at=e.end_at,
                note=e.note,
                auto_book=e.auto_book,
                status=e.status,
                reservation_id=e.reservation_id,
                created_at=e.created_at,
            )
        )
    return out


@router.get("", response_model=list[WaitlistOut])
async def list_waitlist(everyone: bool = False, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Open waitlist entries: your own, or everyone's with ``everyone=true``."""
    stmt = select(WaitlistEntry).where(WaitlistEntry.status.in_(OPEN), WaitlistEntry.end_at > utcnow())
    if not everyone:
        stmt = stmt.where(WaitlistEntry.user_id == user.id)
    rows = (await db.execute(stmt.order_by(WaitlistEntry.start_at))).scalars().all()
    return await _out(db, list(rows))


@router.post("", response_model=WaitlistOut, status_code=201)
async def join_waitlist(
    body: WaitlistIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    start, end = booking.check_window(get_settings(), body.start_at, body.end_at, utcnow())
    model, preset = await booking.resolve_model(db, body.model_id, body.preset_id)
    lane = await booking.resolve_lane(db, body.lane_id)
    await booking.check_model_on_lane(db, model, lane)
    entry = WaitlistEntry(
        user_id=user.id,
        lane_id=lane.id,
        model_id=model.id if model else None,
        preset_id=preset.id if preset else None,
        start_at=start,
        end_at=end,
        note=body.note.strip(),
        auto_book=body.auto_book,
    )
    db.add(entry)
    await db.commit()
    request.app.state.housekeeper.poke()
    return (await _out(db, [entry]))[0]


@router.delete("/{entry_id}", status_code=204)
async def leave_waitlist(entry_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    entry = await db.get(WaitlistEntry, entry_id)
    if entry is None or (entry.user_id != user.id and not user.is_admin):
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if entry.status in OPEN:
        entry.status, entry.resolved_at = "cancelled", utcnow()
        await db.commit()
