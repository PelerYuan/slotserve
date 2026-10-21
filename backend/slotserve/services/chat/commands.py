"""Bot commands and button actions shared by every chat platform.

A channel hands over who wrote (its own address for the chat or user), the command and its argument;
the answer is plain text or a ``Reply`` with buttons, which the channel renders its own way.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ...db import SessionLocal, utcnow
from ...errors import ApiError
from ...orm import HOLDING_STATUSES, Reservation, User
from .. import queries
from ..notify import lang
from .booking import TEXT as BOOKING_TEXT
from .booking import BookingFlow, Reply

if TYPE_CHECKING:
    from .channel import Channel

HELP = {
    "en": (
        "SlotServe bot\n"
        "/status – what the GPU is doing\n"
        "/book – book GPU time\n"
        "/free – free time today and tomorrow\n"
        "/cancel – cancel an upcoming booking\n"
        "/today – today's bookings\n"
        "/mine – your upcoming bookings\n"
        "/extend [minutes] – extend your running booking (default 30)\n"
        "/release – end your running booking now\n"
        "/unlink – stop notifications to this chat"
    ),
    "zh-CN": (
        "SlotServe 机器人\n"
        "/status – GPU 当前状态\n"
        "/book – 预约 GPU 时段\n"
        "/free – 今天和明天的空闲时段\n"
        "/cancel – 取消一个未开始的预约\n"
        "/today – 今天的预约\n"
        "/mine – 我接下来的预约\n"
        "/extend [分钟] – 延长我正在进行的预约（默认 30）\n"
        "/release – 立即结束我正在进行的预约\n"
        "/unlink – 解除与此聊天的绑定"
    ),
}

# Shown below the command list on platforms without a "/" command menu.
HELP_FOOTER = {
    "en": "\n\nCommands work with or without the “/”. Where I offer numbered choices, reply with the number.",
    "zh-CN": "\n\n命令可以不带 “/”，也可以用中文：状态、预约、空闲、取消、今天、我的、延长、结束、解绑。"
    "有编号的选项时，回复编号即可。",
}

REPLIES = {
    "en": {
        "linked": "Linked to SlotServe account {username}. Send /help for commands.",
        "bad_token": "This link has expired. Open Settings in SlotServe and press “Link Telegram” again.",
        "not_linked": "This chat is not linked. Open Settings in SlotServe and press “Link Telegram”.",
        "bad_code": "That code is not valid or has expired. Open Settings in SlotServe and get a new one.",
        "not_linked_code": "This chat is not linked to SlotServe yet. Open Settings in SlotServe, press “Link” "
        "next to {channel} and send me the code like this: bind CODE",
        "too_many": "Too many wrong codes. Try again in {minutes} minutes.",
        "private_only": "Send this to me in a private chat.",
        "unlinked": "Unlinked. You will not get notifications here any more.",
        "no_active": "You have no running booking.",
        "extended": "Extended until {end}.",
        "released": "Booking ended. Thanks!",
        "none_today": "No bookings today.",
        "none_mine": "You have no upcoming bookings.",
        "state": "Model: {model} ({state})\n{holder}",
        "holder_free": "Open to everyone.",
        "holder": "Booked by {user} until {end}.",
        "error": "Could not do that: {code}",
        "approved": "Approved ({count}).",
        "rejected": "Rejected ({count}).",
    },
    "zh-CN": {
        "linked": "已绑定 SlotServe 账号 {username}。发送 /help 查看命令。",
        "bad_token": "链接已失效。请在 SlotServe 的「设置」里重新点「绑定 Telegram」。",
        "not_linked": "此聊天尚未绑定。请在 SlotServe 的「设置」里点「绑定 Telegram」。",
        "bad_code": "绑定码无效或已过期。请在 SlotServe 的「设置」里重新获取。",
        "not_linked_code": "此聊天还没有绑定 SlotServe。请在 SlotServe 的「设置」里点 {channel} 旁的「绑定」，"
        "然后把绑定码发给我，例如：绑定 ABC123",
        "too_many": "错误的绑定码太多，请 {minutes} 分钟后再试。",
        "private_only": "请在私聊里给我发这个命令。",
        "unlinked": "已解除绑定，之后不会再在这里收到通知。",
        "no_active": "你当前没有进行中的预约。",
        "extended": "已延长到 {end}。",
        "released": "预约已结束，谢谢！",
        "none_today": "今天没有预约。",
        "none_mine": "你接下来没有预约。",
        "state": "模型：{model}（{state}）\n{holder}",
        "holder_free": "当前所有人可用。",
        "holder": "{user} 预约中，直到 {end}。",
        "error": "操作失败：{code}",
        "approved": "已通过（{count} 个）。",
        "rejected": "已拒绝（{count} 个）。",
    },
}

# Chinese command words and spellings without "/" map onto the commands above.
ALIASES = {
    "状态": "status",
    "预约": "book",
    "空闲": "free",
    "取消": "cancel",
    "今天": "today",
    "我的": "mine",
    "延长": "extend",
    "结束": "release",
    "解绑": "unlink",
    "帮助": "help",
    "菜单": "help",
    "绑定": "bind",
}
COMMANDS = {"start", "help", "bind", "status", "book", "free", "cancel", "today", "mine", "extend", "release", "unlink"}
# Commands that make sense in a group chat; the others touch one person's bookings.
GROUP_COMMANDS = {"/status", "/free", "/today", "/help", "/start"}


def reply(user: User | None, key: str, **params: Any) -> str:
    return REPLIES[lang(user)][key].format(**params)


def parse(text: str) -> tuple[str, str] | None:
    """``"/extend 60"``, ``"extend 60"`` and ``"延长 60"`` all give ``("/extend", "60")``; other text gives None."""
    first, _, arg = text.replace("　", " ").strip().partition(" ")
    word = first.lstrip("/").split("@", 1)[0].lower()
    word = ALIASES.get(word, word)
    if word not in COMMANDS:
        return None
    return "/" + word, arg.strip()


class ChatCommands:
    def __init__(self, app):
        self.app = app
        self.flow = BookingFlow(app)

    def guest(self) -> User:
        """Stands in for an unknown person in a group chat: default language, no rights."""
        return User(id=0, username="", is_admin=False, locale=self.app.state.settings.chat_default_locale)

    def help(self, channel: "Channel", user: User | None) -> str:
        language = lang(user)
        return HELP[language] + ("" if channel.id == "telegram" else HELP_FOOTER[language])

    async def command(
        self, channel: "Channel", address, name: str | None, command: str, arg: str, *, group: bool = False
    ) -> str | Reply | None:
        async with SessionLocal() as db:
            if command in ("/start", "/bind") and arg and not group:
                return await self._bind(db, channel, address, name, arg)

            user = await channel.find_user(db, address)
            if group:
                if command not in GROUP_COMMANDS:
                    return reply(user or self.guest(), "private_only")
                user = user or self.guest()
            elif user is None:
                if channel.id == "telegram":
                    return reply(None, "not_linked")
                return reply(self.guest(), "not_linked_code", channel=channel.label)
            if command in ("/start", "/help", "/bind"):
                return self.help(channel, user)
            if command == "/unlink":
                await channel.unlink(db, user)
                await db.commit()
                return reply(user, "unlinked")
            if command == "/status":
                return await self._status(db, user)
            if command == "/book":
                return await self.flow.step(db, user, [])
            if command == "/free":
                answer = await self.flow.free(db, user)
                return answer.text if group else answer
            if command == "/cancel":
                return await self.flow.cancel_list(db, user)
            if command == "/today":
                return await self._today(db, user)
            if command == "/mine":
                return await self._mine(db, user)
            if command == "/extend":
                minutes = int(arg) if arg.isdigit() else 30
                return await self._extend(db, user, None, minutes)
            if command == "/release":
                return await self._release(db, user, None)
            return self.help(channel, user)

    async def _bind(self, db, channel: "Channel", address, name: str | None, code: str) -> str:
        limiter = self.app.state.limiter
        key = f"{channel.id}:{address}"
        try:
            limiter.check(ip=key)
        except ApiError as exc:
            return reply(self.guest(), "too_many", minutes=exc.detail.get("params", {}).get("minutes", 15))
        user = await channel.redeem(db, code, address, name)
        if user is None:
            limiter.failure(ip=key)
            if channel.id == "telegram":
                return reply(None, "bad_token")
            return reply(self.guest(), "bad_code")
        await db.commit()
        return reply(user, "linked", username=user.username)

    async def button(self, channel: "Channel", address, data: str) -> str | Reply:
        async with SessionLocal() as db:
            user = await channel.find_user(db, address) if address is not None else None
            if user is None:
                if channel.id == "telegram":
                    return reply(None, "not_linked")
                return reply(self.guest(), "not_linked_code", channel=channel.label)
            parts = data.split(":")
            if parts[0] == "bk":
                return await self.flow.step(db, user, parts[1:])
            if parts[0] in ("bko", "bkw"):
                return await self.flow.book(db, user, parts[1:], waitlist=parts[0] == "bkw")
            if parts[0] in ("cx", "cxy") and len(parts) == 2 and parts[1].isdigit():
                return await self.flow.cancel(db, user, int(parts[1]), confirmed=parts[0] == "cxy")
            if parts[0] == "cxn":
                return Reply(BOOKING_TEXT[lang(user)]["kept"], edit=True)
            if parts[0] == "ext" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                return await self._extend(db, user, int(parts[1]), int(parts[2]))
            if parts[0] == "rel" and len(parts) == 2 and parts[1].isdigit():
                return await self._release(db, user, int(parts[1]))
            if parts[0] in ("apr", "rej") and len(parts) == 2 and parts[1].isdigit():
                return await self._review(db, user, int(parts[1]), approve=parts[0] == "apr")
            return ""

    # ---- commands -----------------------------------------------------------

    async def _status(self, db, user: User) -> str:
        notifier = self.app.state.notifier
        lanes = self.app.state.lanes.ordered()
        parts = []
        for ctx in lanes:
            snap = ctx.runtime.snapshot()
            active = await queries.active_reservation(db, utcnow(), ctx.id)
            if active is None:
                holder = reply(user, "holder_free")
            else:
                owner = await db.get(User, active.user_id)
                end = notifier.fmt_time(active.end_at)
                holder = reply(user, "holder", user=owner.username if owner else "?", end=end)
            text = reply(user, "state", model=snap.model_name or "—", state=snap.state.value, holder=holder)
            parts.append(f"[{ctx.name}]\n{text}" if len(lanes) > 1 else text)
        return "\n\n".join(parts)

    async def _list(self, db, user: User, rows: list[Reservation]) -> str:
        notifier = self.app.state.notifier
        lines = []
        for r in rows:
            owner = await db.get(User, r.user_id)
            what = await notifier.describe(db, r, user)
            lines.append(f"• {notifier.fmt_window(r)}  {owner.username if owner else '?'}  {what}")
        return "\n".join(lines)

    async def _today(self, db, user: User) -> str:
        now = utcnow()
        rows = (
            (
                await db.execute(
                    select(Reservation)
                    .where(
                        Reservation.status == "booked",
                        Reservation.end_at > now,
                        Reservation.start_at < now + timedelta(hours=24),
                    )
                    .order_by(Reservation.start_at)
                )
            )
            .scalars()
            .all()
        )
        return await self._list(db, user, list(rows)) if rows else reply(user, "none_today")

    async def _mine(self, db, user: User) -> str:
        rows = (
            (
                await db.execute(
                    select(Reservation)
                    .where(
                        Reservation.user_id == user.id,
                        Reservation.status.in_(HOLDING_STATUSES),
                        Reservation.end_at > utcnow(),
                    )
                    .order_by(Reservation.start_at)
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        return await self._list(db, user, list(rows)) if rows else reply(user, "none_mine")

    async def _own_reservation(self, db, user: User, reservation_id: int | None) -> Reservation | None:
        if reservation_id is None:
            rows = await db.execute(
                select(Reservation).where(
                    Reservation.user_id == user.id,
                    Reservation.status == "booked",
                    Reservation.start_at <= utcnow(),
                    Reservation.end_at > utcnow(),
                )
            )
            return rows.scalars().first()
        r = await db.get(Reservation, reservation_id)
        return r if r is not None and (r.user_id == user.id or user.is_admin) else None

    async def _extend(self, db, user: User, reservation_id: int | None, minutes: int) -> str:
        from ...api.reservations import extend_core

        r = await self._own_reservation(db, user, reservation_id)
        if r is None:
            return reply(user, "no_active")
        try:
            r = await extend_core(db, user, r, max(5, min(minutes, 24 * 60)))
        except ApiError as exc:
            return reply(user, "error", code=exc.detail.get("code"))
        self.app.state.lanes.poke()
        return reply(user, "extended", end=self.app.state.notifier.fmt_time(r.end_at))

    async def _review(self, db, user: User, reservation_id: int, approve: bool) -> str:
        from ..review import review

        if not user.is_admin:
            return reply(user, "error", code="admin_required")
        r = await db.get(Reservation, reservation_id)
        if r is None:
            return reply(user, "error", code="not_found")
        try:
            done = await review(db, user, r, approve, series=True)
        except ApiError as exc:
            return reply(user, "error", code=exc.detail.get("code"))
        event = "booking_approved" if approve else "booking_rejected"
        self.app.state.notifier.emit(event, user_id=r.user_id, reservation_id=r.id, count=len(done))
        self.app.state.lanes.poke()
        self.app.state.housekeeper.poke()
        return reply(user, "approved" if approve else "rejected", count=len(done))

    async def _release(self, db, user: User, reservation_id: int | None) -> str:
        from ...api.reservations import end_core

        r = await self._own_reservation(db, user, reservation_id)
        if r is None or queries.reservation_phase(r, utcnow()) != "active":
            return reply(user, "no_active")
        try:
            await end_core(db, user, r)
            await db.commit()
        except ApiError as exc:
            return reply(user, "error", code=exc.detail.get("code"))
        self.app.state.lanes.poke()
        self.app.state.housekeeper.poke()
        return reply(user, "released")
