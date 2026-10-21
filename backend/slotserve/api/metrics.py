"""Prometheus text exposition at /metrics, guarded by a bearer token."""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..db import SessionLocal, utcnow
from ..orm import DownloadTask, Lane, LlmModel, ModelLoadEvent, Reservation, UsageDaily, UsageLog, UsageSample, User
from ..runtime.gpu import gpu_stats
from ..runtime.manager import RuntimeState
from ..security import constant_time_equals
from ..services import queries, usage_sampler

router = APIRouter(tags=["metrics"])


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            label_text = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            self.lines.append(f"{name}{{{label_text}}} {value}" if label_text else f"{name} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    token = get_settings().metrics_token
    auth = request.headers.get("authorization", "")
    if not token:
        return PlainTextResponse("metrics are disabled; set a metrics token in the admin settings\n", 404)
    if not auth.lower().startswith("bearer ") or not constant_time_equals(auth[7:].strip(), token):
        return PlainTextResponse("unauthorized\n", 401, headers={"WWW-Authenticate": "Bearer"})

    w = _Writer()
    lanes = request.app.state.lanes.ordered()
    snaps = [(ctx, ctx.runtime.snapshot()) for ctx in lanes]
    w.metric(
        "slotserve_runtime_state",
        "gauge",
        "1 for the current state of each lane's model server.",
        [({"lane": c.name, "state": s.value}, 1 if snap.state == s else 0) for c, snap in snaps for s in RuntimeState],
    )
    w.metric(
        "slotserve_runtime_model_info",
        "gauge",
        "The model loaded on each lane (value is always 1).",
        [
            ({"lane": c.name, "model": snap.model_name, "preset": snap.preset_name or "default"}, 1)
            for c, snap in snaps
            if snap.model_name
        ],
    )

    now = utcnow()
    async with SessionLocal() as db:
        users = dict((await db.execute(select(User.id, User.username))).all())
        running = []
        for ctx in lanes:
            active = await queries.active_reservation(db, now, ctx.id)
            running.append(
                ({"lane": ctx.name, "user": users.get(active.user_id, "?") if active else ""}, int(bool(active)))
            )
        w.metric("slotserve_reservation_active", "gauge", "1 while a booking is running on the lane.", running)
        upcoming = (
            await db.execute(
                select(func.count(Reservation.id)).where(Reservation.status == "booked", Reservation.start_at > now)
            )
        ).scalar_one()
        w.metric("slotserve_reservations_upcoming", "gauge", "Bookings that have not started.", [({}, upcoming)])

        # (user, model) -> [ok, error, prompt, completion, ms]; live rows plus rolled-up history
        totals: dict[tuple[int, str], list[int]] = {}
        live = await db.execute(
            select(
                UsageLog.user_id,
                UsageLog.model_name,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.status_code >= 400), 0),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.duration_ms), 0),
            ).group_by(UsageLog.user_id, UsageLog.model_name)
        )
        archived = await db.execute(
            select(
                UsageDaily.user_id,
                UsageDaily.model_name,
                func.sum(UsageDaily.requests),
                func.sum(UsageDaily.failed),
                func.sum(UsageDaily.prompt_tokens),
                func.sum(UsageDaily.completion_tokens),
                func.sum(UsageDaily.duration_ms),
            ).group_by(UsageDaily.user_id, UsageDaily.model_name)
        )
        for uid, model, n, failed, prompt, completion, ms in [*live.all(), *archived.all()]:
            t = totals.setdefault((uid, model or "?"), [0, 0, 0, 0, 0])
            for i, v in enumerate((n - failed, failed, prompt, completion, ms)):
                t[i] += int(v or 0)
        requests, tokens, seconds = [], [], []
        for (uid, model), (ok, err, prompt, completion, ms) in sorted(totals.items()):
            labels = {"user": users.get(uid, "?"), "model": model}
            requests.append(({**labels, "outcome": "ok"}, ok))
            requests.append(({**labels, "outcome": "error"}, err))
            tokens.append(({**labels, "type": "prompt"}, prompt))
            tokens.append(({**labels, "type": "completion"}, completion))
            seconds.append((labels, round(ms / 1000, 3)))
        w.metric("slotserve_requests_total", "counter", "Gateway requests.", requests)
        w.metric("slotserve_tokens_total", "counter", "Tokens processed through the gateway.", tokens)
        w.metric("slotserve_request_seconds_total", "counter", "Time spent serving requests.", seconds)

        loads = (
            await db.execute(
                select(LlmModel.name, ModelLoadEvent.success, func.count(ModelLoadEvent.id))
                .join(LlmModel, LlmModel.id == ModelLoadEvent.model_id)
                .group_by(LlmModel.name, ModelLoadEvent.success)
            )
        ).all()
        w.metric(
            "slotserve_model_loads_total",
            "counter",
            "Model load attempts.",
            [({"model": m, "result": "success" if ok else "failure"}, n) for m, ok, n in loads],
        )
        models = (await db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None)))).scalars().all()
        estimates = [
            ({"model": m.name}, round(await queries.estimate_load_seconds(db, m, get_settings()), 1)) for m in models
        ]
        w.metric("slotserve_model_load_seconds", "gauge", "Expected load time per model.", estimates)
        queued = (
            await db.execute(select(func.count(DownloadTask.id)).where(DownloadTask.status.in_(("queued", "running"))))
        ).scalar_one()
        w.metric("slotserve_downloads_pending", "gauge", "Downloads queued or running.", [({}, queued)])

        sampler = getattr(request.app.state, "usage_sampler", None)
        gpus = await sampler.stats() if sampler is not None else await gpu_stats()
        await _compute_metrics(w, db, lanes, users, gpus, sampler, now)

    lane_of: dict[int, str] = {}
    for ctx in lanes:
        for g in usage_sampler.lane_gpus(ctx.devices, gpus):
            lane_of.setdefault(g.index, ctx.name)
    for name, help_text, attr, scale in (
        ("slotserve_gpu_memory_used_bytes", "GPU memory in use.", "memory_used_mb", 1024 * 1024),
        ("slotserve_gpu_memory_total_bytes", "GPU memory size.", "memory_total_mb", 1024 * 1024),
        ("slotserve_gpu_utilization_ratio", "GPU utilization (0-1).", "utilization_pct", 0.01),
        ("slotserve_gpu_temperature_celsius", "GPU temperature.", "temperature_c", 1),
        ("slotserve_gpu_power_watts", "GPU power draw.", "power_w", 1),
    ):
        samples = [
            ({"gpu": g.index, "name": g.name, "lane": lane_of.get(g.index, "")}, round(getattr(g, attr) * scale, 3))
            for g in gpus
            if getattr(g, attr) is not None
        ]
        w.metric(name, "gauge", help_text, samples)
    return PlainTextResponse(w.text(), media_type="text/plain; version=0.0.4; charset=utf-8")


async def _compute_metrics(w: _Writer, db, lanes, users: dict[int, str], gpus, sampler, now) -> None:
    """Who holds each lane now, booked GPU-hours, and the sampled GPU time, busy time and energy."""
    occupants = []
    for ctx in lanes:
        occ = await usage_sampler.attribute(db, ctx, now, sampler.probes if sampler is not None else ())
        user = users.get(occ.user_id, "?") if occ.user_id is not None else ""
        occupants.append(({"lane": ctx.name, "kind": occ.kind, "user": user}, 1))
    w.metric(
        "slotserve_lane_occupant",
        "gauge",
        "What each lane is doing now (kind: model, gpu, devenv, idle_model, idle_devenv, free); value is always 1.",
        occupants,
    )

    lane_rows = list((await db.execute(select(Lane))).scalars())
    names = {lane.id: lane.name for lane in lane_rows}
    sizes = {lane.id: usage_sampler.gpu_count(lane.devices, len(gpus)) for lane in lane_rows}
    booked: dict[tuple[str, str, str], float] = {}
    rows = await db.execute(select(Reservation).where(Reservation.status == "booked", Reservation.start_at < now))
    for r in rows.scalars():
        key = (users.get(r.user_id, "?"), names.get(r.lane_id, "?"), usage_sampler.booking_kind(r))
        hours = (min(r.end_at, now) - r.start_at).total_seconds() / 3600 * sizes.get(r.lane_id, 1)
        booked[key] = booked.get(key, 0.0) + hours
    w.metric(
        "slotserve_booked_gpu_hours_total",
        "counter",
        "Booked GPU-hours that have elapsed (booked time x GPUs of the lane).",
        [({"user": u, "lane": lane, "kind": k}, round(v, 4)) for (u, lane, k), v in sorted(booked.items())],
    )

    sampled = (
        await db.execute(
            select(
                UsageSample.lane_id,
                UsageSample.gpu_index,
                UsageSample.kind,
                func.sum(UsageSample.seconds),
                func.sum(UsageSample.busy_seconds),
                func.sum(UsageSample.energy_wh),
            )
            .where(UsageSample.resource == "gpu")
            .group_by(UsageSample.lane_id, UsageSample.gpu_index, UsageSample.kind)
        )
    ).all()
    seconds, busy, energy = [], [], {}
    for lane_id, gpu, kind, sec, busy_sec, wh in sampled:
        labels = {"lane": names.get(lane_id, "?"), "gpu": "" if gpu < 0 else gpu, "kind": kind}
        seconds.append((labels, int(sec or 0)))
        busy.append((labels, int(busy_sec or 0)))
        key = (labels["lane"], labels["gpu"])
        energy[key] = energy.get(key, 0.0) + float(wh or 0)
    w.metric("slotserve_gpu_seconds_total", "counter", "Sampled GPU time by what the GPU was doing.", seconds)
    w.metric("slotserve_gpu_busy_seconds_total", "counter", "Sampled GPU time at or above the busy threshold.", busy)
    w.metric(
        "slotserve_gpu_energy_joules_total",
        "counter",
        "Energy drawn by the GPU, from sampled power.",
        [({"lane": lane, "gpu": g}, round(wh * 3600, 1)) for (lane, g), wh in sorted(energy.items(), key=str)],
    )
    per_user = (
        await db.execute(
            select(
                UsageSample.user_id, UsageSample.kind, func.sum(UsageSample.seconds), func.sum(UsageSample.busy_seconds)
            )
            .where(UsageSample.user_id.is_not(None), UsageSample.resource == "gpu")
            .group_by(UsageSample.user_id, UsageSample.kind)
        )
    ).all()
    w.metric(
        "slotserve_user_gpu_busy_seconds_total",
        "counter",
        "Sampled GPU time at or above the busy threshold during each person's bookings.",
        [({"user": users.get(uid, "?"), "kind": k}, int(b or 0)) for uid, k, _s, b in per_user],
    )
