"""Private iCalendar feeds so bookings show up in phone and desktop calendar apps."""

import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import LlmModel, ModelPreset, Reservation, User
from ..schemas import CalendarOut

router = APIRouter(tags=["calendar"])


def _base_url(request: Request) -> str:
    return get_settings().public_url or str(request.base_url).rstrip("/")


def _out(request: Request, user: User) -> CalendarOut:
    base = f"{_base_url(request)}/ical/{user.ical_token}.ics"
    return CalendarOut(mine_url=f"{base}?scope=mine", all_url=f"{base}?scope=all")


@router.get("/api/calendar", response_model=CalendarOut)
async def calendar_links(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not user.ical_token:
        user.ical_token = secrets.token_urlsafe(24)
        await db.commit()
    return _out(request, user)


@router.post("/api/calendar/reset", response_model=CalendarOut)
async def reset_calendar_links(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Issues a new secret link; the old one stops working."""
    user.ical_token = secrets.token_urlsafe(24)
    await db.commit()
    return _out(request, user)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out, current = [], b""
    for ch in line:
        encoded = ch.encode()
        if len(current) + len(encoded) > (75 if not out else 74):
            out.append(current.decode())
            current = b""
        current += encoded
    out.append(current.decode())
    return "\r\n ".join(out)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


@router.get("/ical/{token}.ics", include_in_schema=False)
async def ical_feed(token: str, request: Request, scope: str = "mine", db: AsyncSession = Depends(get_db)):
    user = None
    if len(token) >= 16:
        user = (await db.execute(select(User).where(User.ical_token == token))).scalar_one_or_none()
    if user is None or user.disabled_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")

    now = utcnow()
    stmt = select(Reservation).where(
        Reservation.status == "booked",
        Reservation.end_at > now - timedelta(days=30),
        Reservation.start_at < now + timedelta(days=366),
    )
    if scope != "all":
        stmt = stmt.where(Reservation.user_id == user.id)
    rows = (await db.execute(stmt.order_by(Reservation.start_at))).scalars().all()

    names = dict((await db.execute(select(User.id, User.username))).all())
    models = dict((await db.execute(select(LlmModel.id, LlmModel.name))).all())
    presets = dict((await db.execute(select(ModelPreset.id, ModelPreset.name))).all())
    host = request.url.hostname or "slotserve"
    zh = (user.locale or "").startswith("zh")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SlotServe//Bookings//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{'GPU 预约' if zh else 'GPU bookings'}{'' if scope == 'all' else ' · ' + user.username}",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for r in rows:
        if r.model_id is None:
            what = "整卡" if zh else "Whole GPU"
        else:
            what = models.get(r.model_id, "?")
            if r.preset_id in presets:
                what += f" ({presets[r.preset_id]})"
        owner = names.get(r.user_id, "?")
        summary = f"GPU · {what}" if scope != "all" else f"GPU · {owner} · {what}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:slotserve-{r.id}@{host}",
            f"DTSTAMP:{_stamp(r.created_at)}",
            f"DTSTART:{_stamp(r.start_at)}",
            f"DTEND:{_stamp(r.end_at)}",
            f"SUMMARY:{_escape(summary)}",
        ]
        if r.note:
            lines.append(f"DESCRIPTION:{_escape(r.note)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    body = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return Response(body, media_type="text/calendar; charset=utf-8", headers={"cache-control": "no-cache"})
