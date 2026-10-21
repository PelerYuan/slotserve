import csv
import io
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..orm import ApiKey, Lane, UsageDaily, UsageLog, UsageSample, User
from ..schemas import UsageDayRow, UsageModelRow, UsageOut, UsageTotals, UsageUserRow
from ..services import usage_report

router = APIRouter(prefix="/api/usage", tags=["usage"])

SCOPE = Query(default="all", pattern="^(all|mine)$", description="Everyone's usage or only the caller's")
TZ = Query(default=0, ge=-840, le=840, description="Minutes east of UTC for local buckets, dates and times")


async def _detected_gpus(request: Request) -> int:
    sampler = getattr(request.app.state, "usage_sampler", None)
    return len(await sampler.stats()) if sampler is not None else 0


async def node_lanes(request: Request, db: AsyncSession, node_id: int | None) -> set[int] | None:
    """The lanes of one machine (``None``: every machine)."""
    if node_id is None:
        return None
    nodes = request.app.state.nodes
    return {
        lane_id
        for lane_id, lane_node in (await db.execute(select(Lane.id, Lane.node_id))).all()
        if (nodes.is_local(node_id) if lane_node is None else lane_node == node_id)
    }


def _cpu_on(request: Request, node_id: int | None) -> bool:
    """CPU-only environments run on the local node (their usage rows do not name a machine)."""
    return node_id is None or request.app.state.nodes.is_local(node_id)


async def _report(
    request: Request, db: AsyncSession, days: int, tz_offset: int, user_id: int | None, node_id: int | None = None
):
    window = usage_report.Window(days, tz_offset, utcnow())
    names = dict((await db.execute(select(User.id, User.username))).all())
    rep = await usage_report.build(
        db,
        window,
        names,
        await _detected_gpus(request),
        user_id,
        lane_ids=await node_lanes(request, db, node_id),
        include_cpu=_cpu_on(request, node_id),
    )
    return window, names, rep


@router.get("", response_model=UsageOut)
async def usage(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    tz_offset: int = TZ,
    scope: str = SCOPE,
    node_id: int | None = Query(default=None, description="Only this machine's lanes (see /api/machines)"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    only = user.id if scope == "mine" else None
    window, names, rep = await _report(request, db, days, tz_offset, only, node_id)
    since = window.since
    lane_ids = await node_lanes(request, db, node_id)

    def logs(stmt):
        stmt = stmt.where(UsageLog.created_at >= since)
        if lane_ids is not None:
            stmt = stmt.where(UsageLog.lane_id.in_(lane_ids))
        return stmt.where(UsageLog.user_id == only) if only is not None else stmt

    per_user = (
        await db.execute(
            logs(
                select(
                    UsageLog.user_id,
                    func.count(UsageLog.id),
                    func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                ).group_by(UsageLog.user_id)
            )
        )
    ).all()
    users: dict[int, UsageUserRow] = {}

    def row(uid: int) -> UsageUserRow:
        return users.setdefault(
            uid,
            UsageUserRow(
                user_id=uid,
                username=names.get(uid, "?"),
                requests=0,
                prompt_tokens=0,
                completion_tokens=0,
                reserved_hours=0.0,
            ),
        )

    for uid, n, p, c in per_user:
        r = row(uid)
        r.requests, r.prompt_tokens, r.completion_tokens = n, p, c
    for uid, cores in rep.cpu_core_seconds.items():
        if uid is not None:
            row(uid).cpu_core_hours = round(cores / 3600, 2)
    for uid in set(rep.booked) | set(rep.people):
        r = row(uid)
        r.bookings = rep.bookings.get(uid, 0)
        r.booked_gpu_hours = round(rep.booked.get(uid, 0.0) / 3600, 2)
        r.reserved_hours = round(rep.calendar.get(uid, 0.0) / 3600, 2)
        acc = rep.people.get(uid)
        if acc is not None:
            r.used_gpu_hours, r.avg_utilization = acc.used_hours, acc.avg_util
            r.peak_memory_mb, r.energy_wh = acc.peak, round(acc.wh, 1)

    per_model = (
        await db.execute(
            logs(
                select(
                    UsageLog.model_name,
                    func.count(UsageLog.id),
                    func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                ).group_by(UsageLog.model_name)
            )
        )
    ).all()

    day = func.strftime("%Y-%m-%d", UsageLog.created_at, f"{tz_offset:+d} minutes")
    per_day = (
        await db.execute(
            logs(
                select(
                    day,
                    UsageLog.user_id,
                    func.count(UsageLog.id),
                    func.coalesce(func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens), 0),
                )
                .group_by(day, UsageLog.user_id)
                .order_by(day)
            )
        )
    ).all()

    requests = sum(r.requests for r in users.values())
    tokens = sum(r.prompt_tokens + r.completion_tokens for r in users.values())
    return UsageOut(
        days=days,
        users=sorted(
            users.values(), key=lambda r: (r.booked_gpu_hours, r.prompt_tokens + r.completion_tokens), reverse=True
        ),
        models=[
            UsageModelRow(model_name=m or "?", requests=n, prompt_tokens=p, completion_tokens=c)
            for m, n, p, c in sorted(per_model, key=lambda r: r[1], reverse=True)
        ],
        daily=[UsageDayRow(date=d, username=names.get(uid, "?"), requests=n, tokens=t) for d, uid, n, t in per_day],
        scope=scope,
        busy_percent=request.app.state.settings.usage_busy_percent,
        metered=rep.all.metered > 0,
        totals=UsageTotals(
            booked_gpu_hours=round(sum(rep.booked.values()) / 3600, 2),
            used_gpu_hours=rep.all.used_hours,
            sampled_gpu_hours=round(rep.all.seconds / 3600, 2),
            # Everyone: how busy the GPUs were while booked; mine: during my bookings.
            avg_utilization=rep.booking_acc.avg_util,
            energy_wh=round(rep.all.wh, 1),
            peak_memory_mb=rep.all.peak,
            requests=requests,
            tokens=tokens,
            cpu_core_hours=round(sum(rep.cpu_core_seconds.values()) / 3600, 2),
        ),
        lanes=rep.lane_rows(),
        gpus=rep.gpu_rows(),
        kinds=rep.kind_rows(),
        bucket_hours=window.bucket_hours,
        buckets=window.labels(),
        hours_by_person=rep.series_by_person(),
        hours_by_kind=rep.series_by_kind(),
        utilization_by_lane=rep.utilization_series(),
    )


def _csv(name: str, rows: list[list]) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    filename = f"slotserve-{name}-{utcnow():%Y%m%d}.csv"
    # BOM so Excel detects UTF-8.
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _pct(util: float, metered: float) -> float | str:
    return round(util / metered, 1) if metered else ""


@router.get("/export")
async def export_usage(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    kind: str = Query(default="summary", pattern="^(summary|requests|compute|people)$"),
    tz_offset: int = TZ,
    scope: str = SCOPE,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """CSV for spreadsheets.

    ``summary``: requests and tokens per day, person and model; ``requests``: one row per request;
    ``compute``: GPU time per day, lane, GPU, kind and person; ``people``: one row per person.
    """
    only = user.id if scope == "mine" else None
    names = dict((await db.execute(select(User.id, User.username))).all())
    if kind == "people":
        data = (await usage(request, days, tz_offset, scope, None, user, db)).users
        rows: list[list] = [
            [
                "user",
                "bookings",
                "booked_hours",
                "booked_gpu_hours",
                "used_gpu_hours",
                "avg_utilization_pct",
                "peak_memory_mb",
                "energy_wh",
                "requests",
                "prompt_tokens",
                "completion_tokens",
            ]
        ]
        for u in data:
            rows.append(
                [
                    u.username,
                    u.bookings,
                    u.reserved_hours,
                    u.booked_gpu_hours,
                    u.used_gpu_hours,
                    "" if u.avg_utilization is None else u.avg_utilization,
                    "" if u.peak_memory_mb is None else u.peak_memory_mb,
                    u.energy_wh,
                    u.requests,
                    u.prompt_tokens,
                    u.completion_tokens,
                ]
            )
        return _csv(kind, rows)
    if kind == "compute":
        window = usage_report.Window(days, tz_offset, utcnow())
        lanes = dict((await db.execute(select(Lane.id, Lane.name))).all())
        day = func.strftime("%Y-%m-%d", UsageSample.start, f"{tz_offset:+d} minutes")
        keys = (UsageSample.lane_id, UsageSample.gpu_index, UsageSample.kind, UsageSample.user_id)
        stmt = (
            select(
                day,
                *keys,
                func.sum(UsageSample.seconds),
                func.sum(UsageSample.busy_seconds),
                func.sum(UsageSample.util_seconds),
                func.sum(UsageSample.metered_seconds),
                func.max(UsageSample.mem_peak_mb),
                func.sum(UsageSample.energy_wh),
            )
            .where(UsageSample.start >= window.since, UsageSample.resource == "gpu")
            .group_by(day, *keys)
            .order_by(day, *keys)
        )
        if only is not None:
            stmt = stmt.where(UsageSample.user_id == only)
        rows = [
            [
                "date",
                "lane",
                "gpu",
                "kind",
                "user",
                "gpu_hours",
                "used_gpu_hours",
                "avg_utilization_pct",
                "peak_memory_mb",
                "energy_wh",
            ]
        ]
        for d, lane_id, gpu, k, uid, sec, busy, util, metered, peak, wh in (await db.execute(stmt)).all():
            rows.append(
                [
                    d,
                    lanes.get(lane_id, "?"),
                    "" if gpu < 0 else gpu,
                    k,
                    names.get(uid, "?") if uid is not None else "",
                    round((sec or 0) / 3600, 3),
                    round((busy or 0) / 3600, 3),
                    _pct(util or 0, metered or 0),
                    "" if peak is None else peak,
                    round(wh or 0, 1),
                ]
            )
        return _csv(kind, rows)

    since = utcnow() - timedelta(days=days)
    if kind == "requests":
        keys_ = dict((await db.execute(select(ApiKey.id, ApiKey.name))).all())
        rows = [
            [
                "time",
                "user",
                "api_key",
                "model",
                "endpoint",
                "stream",
                "status",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
            ]
        ]
        tz = timezone(timedelta(minutes=tz_offset))
        stmt = select(UsageLog).where(UsageLog.created_at >= since).order_by(UsageLog.created_at)
        if only is not None:
            stmt = stmt.where(UsageLog.user_id == only)
        for u in (await db.execute(stmt)).scalars():
            rows.append(
                [
                    u.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                    names.get(u.user_id, "?"),
                    keys_.get(u.api_key_id, ""),
                    u.model_name,
                    u.endpoint,
                    int(u.stream),
                    u.status_code,
                    u.prompt_tokens,
                    u.completion_tokens,
                    u.duration_ms,
                ]
            )
        return _csv(kind, rows)

    day = func.strftime("%Y-%m-%d", UsageLog.created_at, f"{tz_offset:+d} minutes")
    stmt = (
        select(
            day,
            UsageLog.user_id,
            UsageLog.model_name,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0),
        )
        .where(UsageLog.created_at >= since)
        .group_by(day, UsageLog.user_id, UsageLog.model_name)
        .order_by(day)
    )
    if only is not None:
        stmt = stmt.where(UsageLog.user_id == only)
    summary = [(d, uid, model, n, p, c) for d, uid, model, n, p, c in (await db.execute(stmt)).all()]
    # Days older than the retention period only exist as UTC roll-ups.
    first_raw = min((r[0] for r in summary), default="9999-99-99")
    archived_stmt = select(UsageDaily).where(UsageDaily.date >= since.strftime("%Y-%m-%d"), UsageDaily.date < first_raw)
    if only is not None:
        archived_stmt = archived_stmt.where(UsageDaily.user_id == only)
    summary += [
        (a.date, a.user_id, a.model_name, a.requests, a.prompt_tokens, a.completion_tokens)
        for a in (await db.execute(archived_stmt)).scalars()
    ]
    rows = [["date", "user", "model", "requests", "prompt_tokens", "completion_tokens"]]
    for d, uid, model, n, p, c in sorted(summary, key=lambda r: (r[0], names.get(r[1], ""), r[2])):
        rows.append([d, names.get(uid, "?"), model, n, p, c])
    return _csv(kind, rows)
