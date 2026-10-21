from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import admin_user, current_user, get_lanes
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, DevTemplate, Lane, LlmModel, Reservation, User
from ..runtime.gpu import gpu_stats
from ..runtime.lanes import LaneManager
from ..schemas import LaneIn, LaneOut
from ..services.audit import record

router = APIRouter(tags=["lanes"])


async def _out(db: AsyncSession, lanes: list[Lane]) -> list[LaneOut]:
    names = dict((await db.execute(select(LlmModel.id, LlmModel.name))).all())
    templates = dict((await db.execute(select(DevTemplate.id, DevTemplate.name))).all())
    return [
        LaneOut(
            id=lane.id,
            name=lane.name,
            gpus=lane.devices or [],
            default_model_id=lane.default_model_id,
            default_model_name=names.get(lane.default_model_id),
            position=lane.position,
            idle_mode=lane.mode,
            idle_template_id=lane.idle_template_id,
            idle_template_name=templates.get(lane.idle_template_id),
        )
        for lane in lanes
    ]


async def _all(db: AsyncSession) -> list[Lane]:
    return list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())


@router.get("/api/lanes", response_model=list[LaneOut])
async def list_lanes(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _out(db, await _all(db))


async def _validate(db: AsyncSession, body: LaneIn, lane_id: int | None) -> None:
    taken = await db.execute(select(Lane.id).where(func.lower(Lane.name) == body.name.strip().lower()))
    if any(i != lane_id for i in taken.scalars()):
        raise ApiError(status.HTTP_409_CONFLICT, "lane_name_taken")
    gpus = set(body.gpus)
    if len(gpus) != len(body.gpus):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_gpus_invalid")
    detected = {g.index for g in await gpu_stats()}
    if detected and not gpus <= detected:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_gpus_invalid")
    for other in await _all(db):
        if other.id != lane_id and gpus & set(other.devices or []):
            raise ApiError(status.HTTP_409_CONFLICT, "lane_gpus_overlap", lane=other.name)
    if body.default_model_id is not None:
        model = await db.get(LlmModel, body.default_model_id)
        if model is None or model.deleted_at is not None or model.status != "ready":
            raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")
    if body.idle_mode == "devenv":
        # The shared environment is built from a template offered to everyone (a built-in or shared one).
        template = await db.get(DevTemplate, body.idle_template_id) if body.idle_template_id else None
        if template is None or template.deleted_at is not None or template.kind != "builtin":
            raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_idle_template")


def _apply_idle(lane: Lane, body: LaneIn) -> None:
    if body.idle_mode is None:
        return
    lane.idle_mode = None if body.idle_mode == "model" else body.idle_mode
    lane.idle_template_id = body.idle_template_id if body.idle_mode == "devenv" else None


async def _sync_flags(db: AsyncSession) -> None:
    from .models import sync_default_flags

    await sync_default_flags(db)


@router.post("/api/admin/lanes", response_model=LaneOut, status_code=201)
async def create_lane(
    body: LaneIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    await _validate(db, body, None)
    position = body.position
    if position is None:
        position = ((await db.execute(select(func.max(Lane.position)))).scalar_one() or 0) + 1
    lane = Lane(
        name=body.name.strip(),
        gpu_indices=",".join(str(g) for g in sorted(body.gpus)),
        default_model_id=body.default_model_id,
        position=position,
    )
    _apply_idle(lane, body)
    db.add(lane)
    await db.flush()
    await _sync_flags(db)
    record(db, admin, "lane.create", lane.name, gpus=lane.gpu_indices)
    await db.commit()
    await lanes.sync()
    return (await _out(db, [lane]))[0]


@router.patch("/api/admin/lanes/{lane_id}", response_model=LaneOut)
async def update_lane(
    lane_id: int,
    body: LaneIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Renames a lane, changes its GPUs (its process restarts) or its default model."""
    lane = await db.get(Lane, lane_id)
    if lane is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await _validate(db, body, lane.id)
    lane.name = body.name.strip()
    lane.gpu_indices = ",".join(str(g) for g in sorted(body.gpus))
    lane.default_model_id = body.default_model_id
    if body.position is not None:
        lane.position = body.position
    _apply_idle(lane, body)
    await _sync_flags(db)
    record(db, admin, "lane.update", lane.name, gpus=lane.gpu_indices, idle_mode=lane.mode)
    await db.commit()
    await lanes.sync()
    return (await _out(db, [lane]))[0]


@router.delete("/api/admin/lanes/{lane_id}", status_code=204)
async def delete_lane(
    lane_id: int,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    lane = await db.get(Lane, lane_id)
    if lane is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if len(await _all(db)) <= 1:
        raise ApiError(status.HTTP_409_CONFLICT, "last_lane")
    open_bookings = await db.execute(
        select(Reservation.id).where(
            Reservation.lane_id == lane.id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.end_at > utcnow(),
        )
    )
    if open_bookings.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "lane_has_reservations")
    record(db, admin, "lane.delete", lane.name)
    await db.delete(lane)
    await _sync_flags(db)
    await db.commit()
    await lanes.sync()
    request.app.state.runtime = lanes.primary.runtime
    request.app.state.scheduler = lanes.primary.scheduler
