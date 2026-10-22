from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user, get_lanes
from ..errors import ApiError
from ..orm import DevSession, DevTemplate, Lane, LlmModel, ModelPreset, Reservation, UsageLog, User
from ..runtime.gpu import gpu_stats
from ..runtime.lanes import LaneManager
from ..runtime.manager import RuntimeState
from ..schemas import ConfigOut, LaneStatusOut, ManualOut, RuntimeOut, StatusOut
from ..services import kv, queries
from ..services.audit import record

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


class ModelAvailabilityOut(BaseModel):
    """Whether the person asking can call a model right now (F05): the one place this is decided, so the
    Overview, Models, the Playground and QuickBook show the same badge. Mirrors the gateway's admission
    (gateway/proxy.py ``admit``): a model answers on a lane that serves it and is free or booked by the caller.

    ``state``: ``now`` (callable now; ``loading`` while it is still starting there), ``booking`` (installed but
    not running where the caller may use it: a booking loads it) or ``busy`` (running, but on a lane someone
    else has booked: ``holder`` until ``until``)."""

    id: int
    name: str
    is_default: bool = False
    state: Literal["now", "booking", "busy"]
    loading: bool = False
    lane_id: int | None = None
    lane_name: str | None = None
    holder: str | None = None
    until: datetime | None = None
    args_pending: bool = False  # launch arguments wait for an admin's approval (they are not used until then)


class StatusWithModelsOut(StatusOut):
    models: list[ModelAvailabilityOut] = []


_SERVING = (RuntimeState.READY, RuntimeState.LOADING)


async def model_availability(db: AsyncSession, user: User, views) -> list[ModelAvailabilityOut]:
    """``views``: ``(lane context, its running booking or None)`` for every lane, in lane order."""
    models = (
        (
            await db.execute(
                select(LlmModel)
                .where(LlmModel.deleted_at.is_(None), LlmModel.status == "ready")
                .order_by(LlmModel.is_default.desc(), LlmModel.name)
            )
        )
        .scalars()
        .all()
    )
    lanes = [(ctx, active, ctx.runtime.snapshot()) for ctx, active in views]
    holders: dict[int, str] = {}
    out: list[ModelAvailabilityOut] = []
    for m in models:
        item = ModelAvailabilityOut(
            id=m.id, name=m.name, is_default=m.is_default, state="booking", args_pending=bool(m.args_pending)
        )
        usable_hit = busy_hit = None  # (lane, still loading) / (lane, someone else's booking)
        for ctx, active, snap in lanes:
            serving = snap.model_name == m.name and snap.state in _SERVING
            if active is not None and active.user_id == user.id:
                # on the caller's own booked lane only the booked model answers (once it is loaded there)
                if active.model_id != m.id:
                    continue
                loading = not serving or snap.state != RuntimeState.READY
            elif active is not None:
                if serving and busy_hit is None:
                    busy_hit = (ctx, active)
                continue
            elif serving:
                loading = snap.state != RuntimeState.READY or snap.draining
            else:
                continue
            if usable_hit is None or (usable_hit[1] and not loading):
                usable_hit = (ctx, loading)
        if usable_hit is not None:
            ctx, loading = usable_hit
            item.state, item.loading, item.lane_id, item.lane_name = "now", loading, ctx.id, ctx.name
        elif busy_hit is not None:
            ctx, active = busy_hit
            if active.user_id not in holders:
                owner = await db.get(User, active.user_id)
                holders[active.user_id] = owner.username if owner else "?"
            item.state, item.lane_id, item.lane_name = "busy", ctx.id, ctx.name
            item.holder, item.until = holders[active.user_id], active.end_at
        out.append(item)
    return out


@router.get("/status", response_model=StatusWithModelsOut)
async def get_status(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """GPU state per lane; the top-level fields describe the first lane (single-GPU view)."""
    now = utcnow()
    lane_out: list[LaneStatusOut] = []
    views = []
    for ctx in lanes.ordered():
        target = ctx.scheduler.target
        active = await queries.active_reservation(db, now, ctx.id)
        views.append((ctx, active))
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
    return StatusWithModelsOut(
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
        gpus=await gpu_stats(),
        models=await model_availability(db, user, views),
    )


# ---- newcomers: "start here" and one-time suggestions (F01) -------------------------------------------------------

# An account older than this that already used the service does not get "start here" (it upgraded, it is not new).
NEW_ACCOUNT = timedelta(days=1)


class OnboardingOut(BaseModel):
    """``start``: show the "start here" card (until its goals are done or it is dismissed; never for people who
    used the service before it existed). ``done``: which goals are reached, from real activity (a successful
    model call, an environment that started, a booking). ``seen``: one-time suggestions already shown."""

    start: bool
    done: dict[str, bool]
    seen: list[str]


class OnboardingIn(BaseModel):
    dismiss_start: bool = False
    seen: list[str] = Field(default_factory=list, max_length=20)


def _onboarding_key(user: User) -> str:
    return f"onboarding:{user.id}"


async def _goals(db: AsyncSession, user: User) -> dict[str, bool]:
    async def any_row(stmt) -> bool:
        return bool((await db.execute(select(exists(stmt)))).scalar())

    return {
        "call": await any_row(select(UsageLog.id).where(UsageLog.user_id == user.id, UsageLog.status_code == 200)),
        "env": await any_row(
            select(DevSession.id).where(DevSession.user_id == user.id, DevSession.started_at.is_not(None))
        ),
        "book": await any_row(select(Reservation.id).where(Reservation.user_id == user.id)),
    }


async def _onboarding(db: AsyncSession, user: User) -> tuple[dict, dict[str, bool]]:
    done = await _goals(db, user)
    stored = await kv.get(db, _onboarding_key(user))
    state = stored if isinstance(stored, dict) else {}
    if "start" not in state:
        veteran = (done["call"] or done["env"]) and utcnow() - user.created_at > NEW_ACCOUNT
        state["start"] = "hidden" if veteran else "show"
        await kv.put(db, _onboarding_key(user), state)
        await db.commit()
    return state, done


def _onboarding_out(state: dict, done: dict[str, bool]) -> OnboardingOut:
    seen = [s for s in state.get("seen", []) if isinstance(s, str)]
    # the dev environment goal only counts while dev environments are on
    goals = [g for g in done if g != "env" or get_settings().devenv_enabled]
    finished = all(done[g] for g in goals)
    return OnboardingOut(start=state.get("start") == "show" and not finished, done=done, seen=seen)


@router.get("/me/onboarding", response_model=OnboardingOut)
async def onboarding(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    state, done = await _onboarding(db, user)
    return _onboarding_out(state, done)


@router.post("/me/onboarding", response_model=OnboardingOut)
async def update_onboarding(body: OnboardingIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """``dismiss_start``: never show "start here" again; ``seen``: suggestions shown once, kept per account."""
    state, done = await _onboarding(db, user)
    if body.dismiss_start:
        state["start"] = "dismissed"
    seen = [s for s in state.get("seen", []) if isinstance(s, str)]
    for item in body.seen:
        item = item.strip()[:48]
        if item and item not in seen:
            seen.append(item)
    state["seen"] = seen[-100:]
    await kv.put(db, _onboarding_key(user), state)
    await db.commit()
    return _onboarding_out(state, done)


@router.get("/runtime/logs")
async def runtime_logs(
    lines: int = Query(default=200, ge=1, le=2000),
    lane_id: int | None = None,
    _: User = Depends(current_user),
    lanes: LaneManager = Depends(get_lanes),
):
    return {"lines": lanes.get(lane_id).runtime.tail_log(lines)}


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
