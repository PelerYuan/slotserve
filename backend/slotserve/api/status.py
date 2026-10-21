from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user, get_lanes
from ..errors import ApiError
from ..orm import DevSession, DevTemplate, Lane, LlmModel, ModelPreset, Reservation, User
from ..runtime.gpu import gpu_stats
from ..runtime.lanes import LaneManager
from ..schemas import ConfigOut, GpuOut, LaneStatusOut, ManualOut, RuntimeOut, StatusOut
from ..services import queries
from ..services.audit import record
from .lanes import node_view

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/config", response_model=ConfigOut)
async def config(request: Request):
    s = get_settings()
    return ConfigOut(
        quota_weekly_hours=s.quota_weekly_hours,
        quota_upcoming=s.quota_upcoming,
        approval_mode=s.approval_mode,
        approval_hours=s.approval_hours,
        devenv_enabled=s.devenv_enabled,
        telegram_enabled=request.app.state.telegram.enabled,
        slot_minutes=s.slot_minutes,
        max_reservation_hours=s.max_reservation_hours,
        max_advance_days=s.max_advance_days,
        registration_open=s.registration_open,
        version=__version__,
    )


def _runtime_out(snap, progress=None) -> RuntimeOut:
    return RuntimeOut(
        stage=progress.stage if progress else None,
        log_percent=progress.percent if progress else None,
        state=snap.state,
        model_id=snap.model_id,
        model_name=snap.model_name,
        preset_name=snap.preset_name,
        load_started_at=snap.load_started_at,
        ready_at=snap.ready_at,
        est_load_seconds=snap.est_load_seconds,
        error=snap.error,
        error_hint=snap.error_hint,
        draining=snap.draining,
    )


async def _manual_out(db: AsyncSession, scheduler) -> ManualOut | None:
    if (m := scheduler.manual) is None:
        return None
    model = await db.get(LlmModel, m.model_id) if m.model_id else None
    preset = await db.get(ModelPreset, m.preset_id) if m.preset_id else None
    return ManualOut(
        model_name=model.name if model else None,
        preset_name=preset.name if preset else None,
        until=m.until,
        by=m.by,
    )


@router.get("/status", response_model=StatusOut)
async def get_status(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """GPU state per lane; the top-level fields describe the first lane (single-GPU view)."""
    now = utcnow()
    lane_out: list[LaneStatusOut] = []
    for ctx in lanes.ordered():
        target = ctx.scheduler.target
        active = await queries.active_reservation(db, now, ctx.id)
        upcoming = await queries.next_reservation(db, now, ctx.id)
        default = await queries.default_model(db, ctx.id)
        shown = await queries.reservation_out(db, [r for r in (active, upcoming) if r], now)
        lane = await db.get(Lane, ctx.id)
        idle_template = await db.get(DevTemplate, lane.idle_template_id) if lane and lane.idle_template_id else None
        shared_state = (
            await db.execute(
                select(DevSession.state)
                .where(DevSession.kind == "shared", DevSession.lane_id == ctx.id, DevSession.state != "stopped")
                .order_by(DevSession.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        lane_out.append(
            LaneStatusOut(
                id=ctx.id,
                name=ctx.name,
                gpus=ctx.devices,
                runtime=_runtime_out(ctx.runtime.snapshot(), ctx.runtime.log_progress()),
                reason=target.reason if target else None,
                fallback_from=target.fallback_from if target else None,
                manual=await _manual_out(db, ctx.scheduler),
                active_reservation=shown[0] if active else None,
                next_reservation=shown[-1] if upcoming else None,
                default_model=default.name if default else None,
                idle_mode=lane.mode if lane else "model",
                idle_template_name=idle_template.name if idle_template else None,
                shared_state=shared_state,
            )
        )
        lane_out[-1].node_id, lane_out[-1].node_name, lane_out[-1].node_online = node_view(lanes, ctx.node_id)
    first = lane_out[0]
    upcoming = (
        (
            await db.execute(
                select(Reservation)
                .where(Reservation.status == "booked", Reservation.start_at > now)
                .order_by(Reservation.start_at)
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    return StatusOut(
        now=now,
        lanes=lane_out,
        runtime=first.runtime,
        reason=first.reason,
        fallback_from=first.fallback_from,
        manual=first.manual,
        active_reservation=first.active_reservation,
        upcoming=await queries.reservation_out(db, list(upcoming), now),
        free_access=first.active_reservation is None,
        default_model=first.default_model,
        gpus=await all_gpus(lanes),
    )


async def all_gpus(lanes: LaneManager) -> list[GpuOut]:
    """This machine's GPUs, then those of connected nodes (tagged with their node id)."""
    local = lanes.nodes.local.node_id
    gpus = [g.model_copy(update={"node_id": local}) for g in await gpu_stats()]
    for executor in lanes.nodes.all():
        if not executor.is_local and executor.online:
            gpus += [g.model_copy(update={"node_id": executor.node_id}) for g in await executor.gpu_stats()]
    return gpus


@router.get("/runtime/logs")
async def runtime_logs(
    lines: int = Query(default=200, ge=1, le=2000),
    lane_id: int | None = None,
    _: User = Depends(current_user),
    lanes: LaneManager = Depends(get_lanes),
):
    return {"lines": await lanes.get(lane_id).runtime.log_tail(lines)}


@router.post("/runtime/restart", status_code=202)
async def runtime_restart(
    lane_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Reload the current model of a lane (e.g. after editing its launch arguments)."""
    ctx = lanes.get(lane_id)
    active = await queries.active_reservation(db, utcnow(), ctx.id)
    if not user.is_admin and active is not None and active.user_id != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "gpu_reserved_by_other")
    record(db, user, "runtime.restart", ctx.name)
    await db.commit()
    ctx.scheduler.request_restart()
    return {"ok": True}
