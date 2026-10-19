from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user, get_runtime, get_scheduler
from ..errors import ApiError
from ..orm import LlmModel, ModelPreset, Reservation, User
from ..runtime.gpu import gpu_stats
from ..runtime.manager import LlamaRuntime
from ..runtime.scheduler import Scheduler
from ..schemas import ConfigOut, ManualOut, RuntimeOut, StatusOut
from ..services import queries
from ..services.audit import record

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/config", response_model=ConfigOut)
async def config(request: Request):
    s = get_settings()
    return ConfigOut(
        quota_weekly_hours=s.quota_weekly_hours,
        quota_upcoming=s.quota_upcoming,
        telegram_enabled=request.app.state.telegram.enabled,
        slot_minutes=s.slot_minutes,
        max_reservation_hours=s.max_reservation_hours,
        max_advance_days=s.max_advance_days,
        registration_open=s.registration_open,
        version=__version__,
    )


@router.get("/status", response_model=StatusOut)
async def get_status(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    runtime: LlamaRuntime = Depends(get_runtime),
    scheduler: Scheduler = Depends(get_scheduler),
):
    now = utcnow()
    snap = runtime.snapshot()
    target = scheduler.target
    manual = None
    if (m := scheduler.manual) is not None:
        model = await db.get(LlmModel, m.model_id) if m.model_id else None
        preset = await db.get(ModelPreset, m.preset_id) if m.preset_id else None
        manual = ManualOut(
            model_name=model.name if model else None,
            preset_name=preset.name if preset else None,
            until=m.until,
            by=m.by,
        )
    active = await queries.active_reservation(db, now)
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
    default = await queries.default_model(db)
    active_out = (await queries.reservation_out(db, [active], now))[0] if active else None
    return StatusOut(
        now=now,
        runtime=RuntimeOut(
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
        ),
        reason=target.reason if target else None,
        fallback_from=target.fallback_from if target else None,
        manual=manual,
        active_reservation=active_out,
        upcoming=await queries.reservation_out(db, list(upcoming), now),
        free_access=active is None,
        default_model=default.name if default else None,
        gpus=await gpu_stats(),
    )


@router.get("/runtime/logs")
async def runtime_logs(
    lines: int = Query(default=200, ge=1, le=2000),
    _: User = Depends(current_user),
    runtime: LlamaRuntime = Depends(get_runtime),
):
    return {"lines": runtime.tail_log(lines)}


@router.post("/runtime/restart", status_code=202)
async def runtime_restart(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    scheduler: Scheduler = Depends(get_scheduler),
):
    """Reload the current model (e.g. after editing its launch arguments)."""
    active = await queries.active_reservation(db, utcnow())
    if not user.is_admin and active is not None and active.user_id != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "gpu_reserved_by_other")
    record(db, user, "runtime.restart")
    await db.commit()
    scheduler.request_restart()
    return {"ok": True}
