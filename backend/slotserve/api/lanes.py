from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cluster import inventory
from ..db import get_db, utcnow
from ..deps import admin_user, current_user, get_lanes
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, DevTemplate, Lane, LlmModel, Node, Reservation, User
from ..runtime.gpu import gpu_stats
from ..runtime.lanes import LaneManager
from ..schemas import LaneIn, LaneOut
from ..services.audit import record

router = APIRouter(tags=["lanes"])


def node_view(manager: LaneManager | None, node_id: int | None) -> tuple[int | None, str | None, bool]:
    """(node id, name, online) of a lane's node (lanes without one are on the built-in local node)."""
    if manager is None:
        return node_id, None, True
    executor = manager.nodes.find(node_id) or manager.nodes.local
    return executor.node_id, executor.name, executor.online


async def _out(db: AsyncSession, lanes: list[Lane], manager: LaneManager | None = None) -> list[LaneOut]:
    names = dict((await db.execute(select(LlmModel.id, LlmModel.name))).all())
    templates = dict((await db.execute(select(DevTemplate.id, DevTemplate.name))).all())
    out = [
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
    for item, lane in zip(out, lanes, strict=True):
        item.node_id, item.node_name, item.node_online = node_view(manager, lane.node_id)
    return out


async def _all(db: AsyncSession) -> list[Lane]:
    return list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())


@router.get("/api/lanes", response_model=list[LaneOut])
async def list_lanes(
    _: User = Depends(current_user), db: AsyncSession = Depends(get_db), lanes: LaneManager = Depends(get_lanes)
):
    return await _out(db, await _all(db), lanes)


async def _node_id(db: AsyncSession, manager: LaneManager, body: LaneIn, lane: Lane | None) -> int | None:
    """The lane's node as stored: ``None`` for the local node."""
    if lane is not None and "node_id" not in body.model_fields_set:
        return lane.node_id
    if body.node_id is None or manager.nodes.is_local(body.node_id):
        return None
    node = await db.get(Node, body.node_id)
    if node is None or node.revoked_at is not None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_node_invalid")
    return node.id


async def _node_gpus(manager: LaneManager, node_id: int | None) -> set[int]:
    """GPU indices a node reported (empty when unknown)."""
    if manager.nodes.is_local(node_id):
        return {g.index for g in await gpu_stats()}
    executor = manager.nodes.find(node_id)
    known = getattr(executor, "known_gpus", None)
    return {g.index for g in known()} if known is not None else set()


async def _validate(
    db: AsyncSession, body: LaneIn, lane_id: int | None, manager: LaneManager, node_id: int | None = None
) -> None:
    taken = await db.execute(select(Lane.id).where(func.lower(Lane.name) == body.name.strip().lower()))
    if any(i != lane_id for i in taken.scalars()):
        raise ApiError(status.HTTP_409_CONFLICT, "lane_name_taken")
    gpus = set(body.gpus)
    if len(gpus) != len(body.gpus):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_gpus_invalid")
    detected = await _node_gpus(manager, node_id)
    if detected and not gpus <= detected:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_gpus_invalid")
    for other in await _all(db):
        other_node = None if manager.nodes.is_local(other.node_id) else other.node_id
        if other.id != lane_id and other_node == node_id and gpus & set(other.devices or []):
            raise ApiError(status.HTTP_409_CONFLICT, "lane_gpus_overlap", lane=other.name)
    if body.default_model_id is not None:
        model = await db.get(LlmModel, body.default_model_id)
        if model is None or model.deleted_at is not None or model.status != "ready":
            raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")
        if not await inventory.model_on_node(db, model.id, node_id, manager.nodes.local.node_id):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_on_node")
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
    node_id = await _node_id(db, lanes, body, None)
    await _validate(db, body, None, lanes, node_id)
    position = body.position
    if position is None:
        position = ((await db.execute(select(func.max(Lane.position)))).scalar_one() or 0) + 1
    lane = Lane(
        name=body.name.strip(),
        gpu_indices=",".join(str(g) for g in sorted(body.gpus)),
        default_model_id=body.default_model_id,
        position=position,
        node_id=node_id,
    )
    _apply_idle(lane, body)
    db.add(lane)
    await db.flush()
    await _sync_flags(db)
    record(db, admin, "lane.create", lane.name, gpus=lane.gpu_indices, node=node_id)
    await db.commit()
    await lanes.sync()
    return (await _out(db, [lane], lanes))[0]


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
    node_id = await _node_id(db, lanes, body, lane)
    if node_id != lane.node_id:
        # Moving a lane to another machine would strand its bookings' models and environments.
        open_bookings = await db.execute(
            select(Reservation.id).where(
                Reservation.lane_id == lane.id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.end_at > utcnow(),
            )
        )
        if open_bookings.first() is not None:
            raise ApiError(status.HTTP_409_CONFLICT, "lane_has_reservations")
    await _validate(db, body, lane.id, lanes, node_id)
    lane.node_id = node_id
    lane.name = body.name.strip()
    lane.gpu_indices = ",".join(str(g) for g in sorted(body.gpus))
    lane.default_model_id = body.default_model_id
    if body.position is not None:
        lane.position = body.position
    _apply_idle(lane, body)
    await _sync_flags(db)
    record(db, admin, "lane.update", lane.name, gpus=lane.gpu_indices, idle_mode=lane.mode, node=node_id)
    await db.commit()
    await lanes.sync()
    return (await _out(db, [lane], lanes))[0]


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
