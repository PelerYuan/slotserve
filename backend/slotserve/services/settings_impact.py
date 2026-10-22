"""Change preview of booking limits (F07, A04 ``impact``): who and which bookings a new value affects.

Existing bookings are never cancelled or changed by a settings change: they are kept, and the preview lists the
ones that break the new rule (so editing or extending them would now fail) and the people they belong to.

Every function has the signature ``(db, settings, key, before, after) -> Impact`` where ``before`` is the old
value of ``key`` and ``after`` maps every changed key to its new value (``settings`` still holds the old values).
Impact (JSON)::

    {key, text_key, params,
     users: [{user_id, username, ...numbers, over}],
     reservations: [{id, user_id, username, lane_id, start_at, end_at, status, reason}]}

``text_key`` is an i18n key of the frontend (``settings.impact.<key>``); ``params`` fill it in. Admins are
exempt from these limits and never listed. ``placement.dry_run`` (A03) will answer the same question for
placement changes with the same shape.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import utcnow
from ..orm import HOLDING_STATUSES, Reservation, User
from . import booking, policies

LIST_LIMIT = 200  # bookings listed per impact (the counts in params are complete)


def _impact(key: str, params: dict, users: list[dict], reservations: list[dict]) -> dict:
    return {
        "key": key,
        "text_key": f"settings.impact.{key}",
        "params": {**params, "users": len(users), "reservations": len(reservations)},
        "users": users,
        "reservations": reservations[:LIST_LIMIT],
    }


def _res(r: Reservation, username: str, reason: str) -> dict:
    return {
        "id": r.id,
        "user_id": r.user_id,
        "username": username,
        "lane_id": r.lane_id,
        "start_at": booking.aware(r.start_at).isoformat(),
        "end_at": booking.aware(r.end_at).isoformat(),
        "status": r.status,
        "reason": reason,
    }


async def _member_bookings(db: AsyncSession, *where) -> list[tuple[Reservation, str]]:
    """Holding bookings of enabled non-admin accounts that have not ended, with the owner's name."""
    stmt = (
        select(Reservation, User.username)
        .join(User, User.id == Reservation.user_id)
        .where(
            User.is_admin.is_(False),
            User.disabled_at.is_(None),
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.end_at > utcnow(),
            *where,
        )
        .order_by(Reservation.start_at)
    )
    return [(r, name) for r, name in (await db.execute(stmt)).all()]


def _users(rows: list[tuple[Reservation, str]]) -> list[dict]:
    seen: dict[int, dict] = {}
    for r, name in rows:
        entry = seen.setdefault(r.user_id, {"user_id": r.user_id, "username": name, "bookings": 0, "over": True})
        entry["bookings"] += 1
    return list(seen.values())


async def quota_weekly_hours(db: AsyncSession, settings: Settings, key: str, before: Any, after: dict) -> dict:
    """Weekly hours already booked per person against the new quota (with their extra hours)."""
    new = int(after.get("quota_weekly_hours", settings.quota_weekly_hours))
    now = utcnow()
    this_week, this_week_end = booking.week_bounds(settings, now)
    if not new:
        return _impact(key, {"before": before, "after": new}, [], [])
    rows = await _member_bookings(db, Reservation.end_at > this_week)
    hours: dict[int, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    by_week: dict[tuple[int, datetime], list[Reservation]] = defaultdict(list)
    names: dict[int, str] = {}
    for r, name in rows:
        names[r.user_id] = name
        start, end = booking.aware(r.start_at), booking.aware(r.end_at)
        week, _ = booking.week_bounds(settings, start)
        while week < end:
            week_end = week + timedelta(days=7)
            overlap = (min(end, week_end) - max(start, week)).total_seconds() / 3600
            if overlap > 0:
                hours[r.user_id][week] += overlap
                by_week[(r.user_id, week)].append(r)
            week = week_end
    users, reservations = [], []
    for uid, weeks in hours.items():
        grants = await policies.grants(db, uid)
        over_weeks = []
        for week, booked in sorted(weeks.items()):
            limit = new + policies.extra_hours(grants, week, week + timedelta(days=7))
            if booked > limit + 0.01:
                over_weeks.append(week)
                reservations += [_res(r, names[uid], "over_quota") for r in by_week[(uid, week)]]
        this = weeks.get(this_week, 0.0)
        extra = policies.extra_hours(grants, this_week, this_week_end)
        if this > 0 or over_weeks:
            users.append(
                {
                    "user_id": uid,
                    "username": names[uid],
                    "hours_this_week": round(this, 2),
                    "limit_this_week": new + extra,
                    "extra_hours": extra,
                    "over": bool(over_weeks),
                    "over_weeks": [w.isoformat() for w in over_weeks],
                }
            )
    users.sort(key=lambda u: (not u["over"], -u["hours_this_week"], u["username"]))
    reservations = list({r["id"]: r for r in reservations}.values())
    params = {"before": before, "after": new, "over": sum(1 for u in users if u["over"])}
    return _impact(key, params, users, reservations)


async def quota_upcoming(db: AsyncSession, settings: Settings, key: str, before: Any, after: dict) -> dict:
    """People holding more open bookings than the new limit."""
    new = int(after.get("quota_upcoming", settings.quota_upcoming))
    if not new:
        return _impact(key, {"before": before, "after": new}, [], [])
    per_user: dict[int, list[tuple[Reservation, str]]] = defaultdict(list)
    for r, name in await _member_bookings(db):
        per_user[r.user_id].append((r, name))
    users, reservations = [], []
    for uid, rows in per_user.items():
        if len(rows) > new:
            users.append({"user_id": uid, "username": rows[0][1], "open_bookings": len(rows), "over": True})
            reservations += [_res(r, name, "over_upcoming") for r, name in rows[new:]]
    params = {"before": before, "after": new, "over": len(users)}
    return _impact(key, params, users, reservations)


async def max_reservation_hours(db: AsyncSession, settings: Settings, key: str, before: Any, after: dict) -> dict:
    """Bookings longer than the new maximum."""
    new = int(after.get("max_reservation_hours", settings.max_reservation_hours))
    limit = timedelta(hours=new) + booking.CLOCK_SKEW
    rows = [
        (r, n) for r, n in await _member_bookings(db) if booking.aware(r.end_at) - booking.aware(r.start_at) > limit
    ]
    reservations = [_res(r, n, "too_long") for r, n in rows]
    return _impact(key, {"before": before, "after": new}, _users(rows), reservations)


async def max_advance_days(db: AsyncSession, settings: Settings, key: str, before: Any, after: dict) -> dict:
    """Bookings that start further ahead than the new limit."""
    new = int(after.get("max_advance_days", settings.max_advance_days))
    rows = await _member_bookings(db, Reservation.start_at > utcnow() + timedelta(days=new))
    reservations = [_res(r, n, "too_far") for r, n in rows]
    return _impact(key, {"before": before, "after": new}, _users(rows), reservations)


async def approval(db: AsyncSession, settings: Settings, key: str, before: Any, after: dict) -> dict:
    """Approval mode / hours: upcoming bookings that would now need approval (they stay booked), or pending
    requests that stay pending when approval is switched off."""
    mode = after.get("approval_mode", settings.approval_mode)
    hours = int(after.get("approval_hours", settings.approval_hours))
    now = utcnow()
    params = {
        "mode_before": settings.approval_mode,
        "mode": mode,
        "hours_before": settings.approval_hours,
        "hours": hours,
    }
    if mode == "off":
        rows = await _member_bookings(db, Reservation.status == "pending")
        reservations = [_res(r, n, "still_pending") for r, n in rows]
        return _impact("approval_mode", params, _users(rows), reservations)
    rows = await _member_bookings(db, Reservation.status == "booked", Reservation.start_at > now)
    limit = timedelta(hours=hours)
    hit = [(r, n) for r, n in rows if mode == "all" or booking.aware(r.end_at) - booking.aware(r.start_at) > limit]
    reservations = [_res(r, n, "would_need_approval") for r, n in hit]
    params["pending"] = len(await _member_bookings(db, Reservation.status == "pending"))
    return _impact("approval_mode", params, _users(hit), reservations)
