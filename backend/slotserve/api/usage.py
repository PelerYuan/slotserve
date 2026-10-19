import csv
import io
from collections import defaultdict
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..orm import ApiKey, Reservation, UsageDaily, UsageLog, User
from ..schemas import UsageDayRow, UsageModelRow, UsageOut, UsageUserRow

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("", response_model=UsageOut)
async def usage(
    days: int = Query(default=30, ge=1, le=365),
    tz_offset: int = Query(default=0, ge=-840, le=840, description="Minutes east of UTC for daily buckets"),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    now = utcnow()
    since = now - timedelta(days=days)
    names = dict((await db.execute(select(User.id, User.username))).all())

    per_user = (
        await db.execute(
            select(
                UsageLog.user_id,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
            )
            .where(UsageLog.created_at >= since)
            .group_by(UsageLog.user_id)
        )
    ).all()

    reserved: dict[int, float] = defaultdict(float)
    res_rows = (
        await db.execute(
            select(Reservation.user_id, Reservation.start_at, Reservation.end_at).where(
                Reservation.status == "booked", Reservation.end_at > since, Reservation.start_at < now
            )
        )
    ).all()
    for uid, start, end in res_rows:
        reserved[uid] += (min(end, now) - max(start, since)).total_seconds() / 3600

    users = {
        uid: UsageUserRow(
            user_id=uid,
            username=names.get(uid, "?"),
            requests=n,
            prompt_tokens=p,
            completion_tokens=c,
            reserved_hours=0.0,
        )
        for uid, n, p, c in per_user
    }
    for uid, hours in reserved.items():
        row = users.setdefault(
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
        row.reserved_hours = round(hours, 2)

    per_model = (
        await db.execute(
            select(
                UsageLog.model_name,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
            )
            .where(UsageLog.created_at >= since)
            .group_by(UsageLog.model_name)
        )
    ).all()

    day = func.strftime("%Y-%m-%d", UsageLog.created_at, f"{tz_offset:+d} minutes")
    per_day = (
        await db.execute(
            select(
                day,
                UsageLog.user_id,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens), 0),
            )
            .where(UsageLog.created_at >= since)
            .group_by(day, UsageLog.user_id)
            .order_by(day)
        )
    ).all()

    return UsageOut(
        days=days,
        users=sorted(users.values(), key=lambda r: r.prompt_tokens + r.completion_tokens, reverse=True),
        models=[
            UsageModelRow(model_name=m or "?", requests=n, prompt_tokens=p, completion_tokens=c)
            for m, n, p, c in sorted(per_model, key=lambda r: r[1], reverse=True)
        ],
        daily=[UsageDayRow(date=d, username=names.get(uid, "?"), requests=n, tokens=t) for d, uid, n, t in per_day],
    )


@router.get("/export")
async def export_usage(
    days: int = Query(default=30, ge=1, le=365),
    kind: str = Query(default="summary", pattern="^(summary|requests)$"),
    tz_offset: int = Query(default=0, ge=-840, le=840, description="Minutes east of UTC for dates and times"),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """CSV for spreadsheets: one row per day, person and model (``summary``) or per request (``requests``)."""
    since = utcnow() - timedelta(days=days)
    names = dict((await db.execute(select(User.id, User.username))).all())
    buf = io.StringIO()
    writer = csv.writer(buf)
    if kind == "requests":
        keys = dict((await db.execute(select(ApiKey.id, ApiKey.name))).all())
        writer.writerow(
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
        )
        tz = timezone(timedelta(minutes=tz_offset))
        rows = await db.execute(select(UsageLog).where(UsageLog.created_at >= since).order_by(UsageLog.created_at))
        for u in rows.scalars():
            writer.writerow(
                [
                    u.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                    names.get(u.user_id, "?"),
                    keys.get(u.api_key_id, ""),
                    u.model_name,
                    u.endpoint,
                    int(u.stream),
                    u.status_code,
                    u.prompt_tokens,
                    u.completion_tokens,
                    u.duration_ms,
                ]
            )
    else:
        day = func.strftime("%Y-%m-%d", UsageLog.created_at, f"{tz_offset:+d} minutes")
        writer.writerow(["date", "user", "model", "requests", "prompt_tokens", "completion_tokens"])
        rows = await db.execute(
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
        summary = [(d, uid, model, n, p, c) for d, uid, model, n, p, c in rows.all()]
        # Days older than the retention period only exist as UTC roll-ups.
        first_raw = min((r[0] for r in summary), default="9999-99-99")
        archived = await db.execute(
            select(UsageDaily).where(UsageDaily.date >= since.strftime("%Y-%m-%d"), UsageDaily.date < first_raw)
        )
        summary += [
            (a.date, a.user_id, a.model_name, a.requests, a.prompt_tokens, a.completion_tokens)
            for a in archived.scalars()
        ]
        for d, uid, model, n, p, c in sorted(summary, key=lambda r: (r[0], names.get(r[1], ""), r[2])):
            writer.writerow([d, names.get(uid, "?"), model, n, p, c])
    filename = f"slotserve-{kind}-{utcnow():%Y%m%d}.csv"
    # BOM so Excel detects UTF-8.
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )
