"""Booking from any chat platform: /book walks through lane → model → day → start → length with buttons; /free lists
open time; /cancel cancels an upcoming booking. Everything goes through the same checks as the web UI.

Button data (Telegram allows 64 bytes) carries the choices made so far:
``bk[:lane[:model[:day[:start[:minutes]]]]]`` where model is an id or ``x`` (whole GPU), day is ``YYYYMMDD`` in
the configured time zone and start is ``HHMM`` or ``n`` (now). ``bko:…`` books, ``bkw:…`` joins the waitlist,
``cx:id`` asks to cancel and ``cxy:id`` cancels.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from ...config import get_settings
from ...db import utcnow
from ...errors import ApiError
from ...orm import HOLDING_STATUSES, Lane, LlmModel, Reservation, User, WaitlistEntry
from .. import booking, queries
from ..notify import lang
from ..reservations import create_core, end_core

Buttons = list[list[tuple[str, str]]]

DURATIONS = (30, 60, 120, 180, 240, 360, 480, 720)
MAX_MODELS = 20

TEXT = {
    "en": {
        "pick_lane": "Book: which GPU lane?",
        "pick_model": "Book{lane}: what do you want to run?",
        "whole_gpu": "Whole GPU (no model)",
        "pick_day": "{what}: which day?",
        "pick_start": "{what}, {day}: start at?",
        "no_start": "{what}, {day}: nothing free that day. Pick another day.",
        "pick_length": "{what}, {day} from {start}: how long?",
        "confirm": "Book {what}\n{day} {start}–{end}{lane}\n{approval}",
        "needs_approval": "An admin has to approve this booking.",
        "book": "Book",
        "back": "◀ Back",
        "cancel": "Cancel",
        "now": "Now",
        "today": "Today",
        "tomorrow": "Tomorrow",
        "booked": "Booked: {what}, {day} {start}–{end}.",
        "requested": "Requested: {what}, {day} {start}–{end}. You will be told when an admin decides.",
        "dropped": "Nothing booked.",
        "taken": "That time was just taken ({user} {start}–{end}).",
        "join_waitlist": "Join waitlist",
        "waitlisted": "On the waitlist. If that time frees up, it is booked for you automatically.",
        "free_title": "Free time",
        "free_none": "fully booked",
        "free_book": "Book",
        "cancel_pick": "Which booking do you want to cancel?",
        "cancel_none": "You have no upcoming bookings. A running booking is ended with /release.",
        "cancel_confirm": "Cancel {what}, {window}?",
        "cancel_yes": "Yes, cancel",
        "cancel_no": "Keep it",
        "cancelled": "Cancelled: {what}, {window}.",
        "kept": "Kept.",
        "errors": {
            "reservation_conflict": "That time is taken.",
            "reservation_in_past": "That time has passed.",
            "reservation_too_long": "Bookings can be at most {hours} h.",
            "reservation_too_far": "Bookings can be at most {days} days ahead.",
            "quota_weekly": "That would go over your weekly limit of {limit} h.",
            "quota_upcoming": "You already have {limit} upcoming bookings.",
            "model_not_ready": "That model is not ready.",
            "not_found": "Not found any more.",
        },
    },
    "zh-CN": {
        "pick_lane": "预约：选择 GPU 通道",
        "pick_model": "预约{lane}：要运行什么？",
        "whole_gpu": "整卡（不加载模型）",
        "pick_day": "{what}：哪一天？",
        "pick_start": "{what}，{day}：几点开始？",
        "no_start": "{what}，{day}：这天已经约满，换一天吧。",
        "pick_length": "{what}，{day} {start} 开始：用多久？",
        "confirm": "预约 {what}\n{day} {start}–{end}{lane}\n{approval}",
        "needs_approval": "这个预约需要管理员审批。",
        "book": "确认预约",
        "back": "◀ 返回",
        "cancel": "取消",
        "now": "现在",
        "today": "今天",
        "tomorrow": "明天",
        "booked": "已预约：{what}，{day} {start}–{end}。",
        "requested": "已提交申请：{what}，{day} {start}–{end}。审批结果会通知你。",
        "dropped": "已放弃，没有预约。",
        "taken": "这个时间刚被预约了（{user} {start}–{end}）。",
        "join_waitlist": "加入候补",
        "waitlisted": "已加入候补。这个时间空出来时会自动为你预约。",
        "free_title": "空闲时段",
        "free_none": "已约满",
        "free_book": "预约",
        "cancel_pick": "要取消哪个预约？",
        "cancel_none": "你接下来没有预约。正在进行的预约请用 /release 结束。",
        "cancel_confirm": "取消 {what}，{window}？",
        "cancel_yes": "确认取消",
        "cancel_no": "保留",
        "cancelled": "已取消：{what}，{window}。",
        "kept": "已保留。",
        "errors": {
            "reservation_conflict": "这个时间已被预约。",
            "reservation_in_past": "这个时间已经过去了。",
            "reservation_too_long": "单次预约最长 {hours} 小时。",
            "reservation_too_far": "最多只能提前 {days} 天预约。",
            "quota_weekly": "会超过你每周 {limit} 小时的上限。",
            "quota_upcoming": "你已经有 {limit} 个未开始的预约了。",
            "model_not_ready": "这个模型还不能用。",
            "not_found": "已经不存在了。",
        },
    },
}
WEEKDAYS = {"en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "zh-CN": "一二三四五六日"}


@dataclass
class Reply:
    text: str
    buttons: Buttons = field(default_factory=list)
    edit: bool = False  # replace the message the button was on instead of sending a new one


def _t(user: User, key: str, /, **params: Any) -> str:
    return TEXT[lang(user)][key].format(**params)


def error_text(user: User, exc: ApiError) -> str:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code", "")
    template = TEXT[lang(user)]["errors"].get(code)
    if template is None:
        return code or "error"
    try:
        return template.format(**detail.get("params", {}))
    except (KeyError, IndexError):
        return template


class BookingFlow:
    def __init__(self, app):
        self.app = app

    # ---- time helpers --------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(get_settings().timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def _day_start(self, token: str) -> datetime:
        return datetime.strptime(token, "%Y%m%d").replace(tzinfo=self.tz)

    def _day_label(self, user: User, day: datetime) -> str:
        today = utcnow().astimezone(self.tz).date()
        prefix = ""
        if day.date() == today:
            prefix = _t(user, "today") + " "
        elif day.date() == today + timedelta(days=1):
            prefix = _t(user, "tomorrow") + " "
        names = WEEKDAYS[lang(user)]
        week = f"周{names[day.weekday()]}" if lang(user) == "zh-CN" else names[day.weekday()]
        return f"{prefix}{day.month}/{day.day} {week}"

    def _hm(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).strftime("%H:%M")

    def _start_of(self, day: datetime, token: str) -> datetime:
        if token == "n":
            return utcnow()
        return day.replace(hour=int(token[:2]), minute=int(token[2:]))

    # ---- /book ---------------------------------------------------------------

    async def lanes(self, db) -> list[Lane]:
        return list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())

    async def what(self, db, user: User, model_token: str) -> str:
        if model_token == "x":
            return _t(user, "whole_gpu")
        model = await db.get(LlmModel, int(model_token))
        return model.name if model else "?"

    async def step(self, db, user: User, parts: list[str]) -> Reply:
        """``parts`` are the choices after ``bk``; returns the next question."""
        if parts == ["q"]:
            return Reply(_t(user, "dropped"), edit=True)
        lanes = await self.lanes(db)
        edit_first = parts == [""]
        parts = [p for p in parts if p]
        if not parts:
            if len(lanes) == 1:
                parts = [str(lanes[0].id)]
            else:
                rows = [[(lane.name, f"bk:{lane.id}")] for lane in lanes]
                return Reply(_t(user, "pick_lane"), [*rows, [(_t(user, "cancel"), "bk:q")]], edit=edit_first)
        # "bk:" (empty choice) goes back to the lane list; with a single lane the first step has no way back.
        back = "bk:" + ":".join(parts[:-1]) if len(parts) > 1 else "bk:" if len(lanes) > 1 else "bk:q"
        nav = [(_t(user, "back"), back), (_t(user, "cancel"), "bk:q")]
        lane = await db.get(Lane, int(parts[0]))
        if lane is None:
            return Reply(error_text(user, ApiError(404, "not_found")), edit=True)
        lane_suffix = f" · {lane.name}" if len(lanes) > 1 else ""
        prefix = "bk:" + ":".join(parts)

        if len(parts) == 1:
            models = (
                await db.execute(
                    select(LlmModel)
                    .where(LlmModel.deleted_at.is_(None), LlmModel.status == "ready")
                    .order_by(LlmModel.name)
                    .limit(MAX_MODELS)
                )
            ).scalars()
            rows = [[(m.name, f"{prefix}:{m.id}")] for m in models]
            rows.append([(_t(user, "whole_gpu"), f"{prefix}:x")])
            return Reply(_t(user, "pick_model", lane=lane_suffix), [*rows, nav], edit=True)

        what = await self.what(db, user, parts[1])
        settings = get_settings()
        now = utcnow()
        if len(parts) == 2:
            today = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
            days = [today + timedelta(days=i) for i in range(min(7, settings.max_advance_days + 1))]
            buttons = [(self._day_label(user, d), f"{prefix}:{d.strftime('%Y%m%d')}") for d in days]
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            return Reply(_t(user, "pick_day", what=what), [*rows, nav], edit=True)

        day = self._day_start(parts[2])
        day_label = self._day_label(user, day)
        if len(parts) == 3:
            slot = timedelta(minutes=settings.slot_minutes)
            gaps = await booking.free_windows(db, lane.id, max(day, now), day + timedelta(days=1))
            buttons: list[tuple[str, str]] = []
            if gaps and gaps[0][0] <= now + timedelta(minutes=1) and gaps[0][1] - now >= slot:
                buttons.append((_t(user, "now"), f"{prefix}:n"))
            for hour in range(24):
                t = day.replace(hour=hour)
                if t <= now:
                    continue
                if any(s <= t and t + slot <= e for s, e in gaps):
                    buttons.append((t.strftime("%H:%M"), f"{prefix}:{t.strftime('%H%M')}"))
            if not buttons:
                return Reply(_t(user, "no_start", what=what, day=day_label), [nav], edit=True)
            rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
            return Reply(_t(user, "pick_start", what=what, day=day_label), [*rows, nav], edit=True)

        start = self._start_of(day, parts[3])
        start_label = _t(user, "now") if parts[3] == "n" else self._hm(start)
        if len(parts) == 4:
            limit = timedelta(hours=settings.max_reservation_hours)
            nxt = await booking.find_conflict(db, start, start + limit, lane_id=lane.id)
            room = (booking.aware(nxt.start_at) - start) if nxt else limit
            choices = [m for m in DURATIONS if timedelta(minutes=m) <= min(room, limit)]
            if not choices:
                return Reply(_t(user, "no_start", what=what, day=day_label), [nav], edit=True)
            buttons = [(_duration(user, m), f"{prefix}:{m}") for m in choices]
            rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
            return Reply(_t(user, "pick_length", what=what, day=day_label, start=start_label), [*rows, nav], edit=True)

        end = start + timedelta(minutes=int(parts[4]))
        windows = [(start, end)]
        approval = _t(user, "needs_approval") if booking.needs_approval(settings, user, windows) else ""
        text = _t(
            user,
            "confirm",
            what=what,
            day=day_label,
            start=start_label,
            end=self._hm(end),
            lane=lane_suffix,
            approval=approval,
        ).rstrip()
        payload = ":".join(parts)
        return Reply(text, [[(_t(user, "book"), f"bko:{payload}")], nav], edit=True)

    async def book(self, db, user: User, parts: list[str], waitlist: bool = False) -> Reply:
        from ...schemas import ReservationCreateIn

        if len(parts) != 5:
            return Reply(_t(user, "dropped"), edit=True)
        lane_id, model_token, day_token, start_token, minutes = parts
        day = self._day_start(day_token)
        start = self._start_of(day, start_token)
        end = start + timedelta(minutes=int(minutes))
        model_id = None if model_token == "x" else int(model_token)
        what = await self.what(db, user, model_token)
        label = {"what": what, "day": self._day_label(user, day), "start": self._hm(start), "end": self._hm(end)}
        if waitlist:
            try:
                start, end = booking.check_window(get_settings(), start, end, utcnow())
                await booking.resolve_model(db, model_id, None)
            except ApiError as exc:
                return Reply(error_text(user, exc), edit=True)
            db.add(
                WaitlistEntry(
                    user_id=user.id, lane_id=int(lane_id), model_id=model_id, start_at=start, end_at=end, auto_book=True
                )
            )
            await db.commit()
            self.app.state.housekeeper.poke()
            return Reply(_t(user, "waitlisted"), edit=True)
        body = ReservationCreateIn(model_id=model_id, start_at=start, end_at=end, lane_id=int(lane_id))
        try:
            created = await create_core(self.app, db, user, body)
        except ApiError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("code") == "reservation_conflict":
                p = detail.get("params", {})
                text = _t(
                    user,
                    "taken",
                    user=p.get("username", "?"),
                    start=self._hm(datetime.fromisoformat(p["start_at"])) if p.get("start_at") else "?",
                    end=self._hm(datetime.fromisoformat(p["end_at"])) if p.get("end_at") else "?",
                )
                payload = ":".join(parts)
                buttons = [
                    [(_t(user, "join_waitlist"), f"bkw:{payload}")],
                    [(_t(user, "back"), f"bk:{lane_id}:{model_token}:{day_token}")],
                ]
                return Reply(text, buttons, edit=True)
            return Reply(error_text(user, exc), edit=True)
        r = created[0]
        label["start"], label["end"] = self._hm(r.start_at), self._hm(r.end_at)
        return Reply(_t(user, "requested" if r.status == "pending" else "booked", **label), edit=True)

    # ---- /free ---------------------------------------------------------------

    async def free(self, db, user: User) -> Reply:
        now = utcnow()
        local_today = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        lanes = await self.lanes(db)
        slot = timedelta(minutes=get_settings().slot_minutes)
        blocks = []
        for lane in lanes:
            lines = [f"[{lane.name}]"] if len(lanes) > 1 else []
            for offset in (0, 1):
                day = local_today + timedelta(days=offset)
                gaps = await booking.free_windows(db, lane.id, max(day, now), day + timedelta(days=1))
                gaps = [(s, e) for s, e in gaps if e - s >= slot]
                spans = ", ".join(
                    f"{self._hm(s)}–{'24:00' if e == day + timedelta(days=1) else self._hm(e)}" for s, e in gaps
                )
                lines.append(f"{self._day_label(user, day)}: {spans or _t(user, 'free_none')}")
            blocks.append("\n".join(lines))
        text = _t(user, "free_title") + "\n\n" + "\n\n".join(blocks)
        return Reply(text, [[(_t(user, "free_book"), "bk")]])

    # ---- /cancel -------------------------------------------------------------

    async def cancel_list(self, db, user: User) -> Reply:
        notifier = self.app.state.notifier
        rows = (
            (
                await db.execute(
                    select(Reservation)
                    .where(
                        Reservation.user_id == user.id,
                        Reservation.status.in_(HOLDING_STATUSES),
                        Reservation.start_at > utcnow(),
                    )
                    .order_by(Reservation.start_at)
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return Reply(_t(user, "cancel_none"))
        buttons = [
            [(f"{notifier.fmt_window(r)} {await notifier.describe(db, r, user)}"[:60], f"cx:{r.id}")] for r in rows
        ]
        return Reply(_t(user, "cancel_pick"), buttons)

    async def cancel(self, db, user: User, reservation_id: int, confirmed: bool) -> Reply:

        notifier = self.app.state.notifier
        r = await db.get(Reservation, reservation_id)
        if r is None or r.user_id != user.id or queries.reservation_phase(r, utcnow()) not in ("upcoming", "pending"):
            return Reply(error_text(user, ApiError(404, "not_found")), edit=True)
        label = {"what": await notifier.describe(db, r, user), "window": notifier.fmt_window(r)}
        if not confirmed:
            buttons = [[(_t(user, "cancel_yes"), f"cxy:{r.id}"), (_t(user, "cancel_no"), "cxn")]]
            return Reply(_t(user, "cancel_confirm", **label), buttons, edit=True)
        await end_core(db, user, r)
        await db.commit()
        self.app.state.lanes.poke(r.lane_id)
        self.app.state.housekeeper.poke()
        return Reply(_t(user, "cancelled", **label), edit=True)


def _duration(user: User, minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min" if lang(user) == "en" else f"{minutes} 分钟"
    hours = minutes / 60
    text = f"{hours:g}"
    return f"{text} h" if lang(user) == "en" else f"{text} 小时"
