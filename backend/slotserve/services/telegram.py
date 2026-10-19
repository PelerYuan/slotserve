"""Telegram bot: long polling (no public URL needed), account linking, a few commands and buttons."""

import asyncio
import logging
import secrets
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from ..db import SessionLocal, utcnow
from ..errors import ApiError
from ..orm import Reservation, User
from . import queries
from .notify import lang

log = logging.getLogger(__name__)

POLL_TIMEOUT = 25

HELP = {
    "en": (
        "SlotServe bot\n"
        "/status – what the GPU is doing\n"
        "/today – today's bookings\n"
        "/mine – your upcoming bookings\n"
        "/extend [minutes] – extend your running booking (default 30)\n"
        "/release – end your running booking now\n"
        "/unlink – stop notifications to this chat"
    ),
    "zh-CN": (
        "SlotServe 机器人\n"
        "/status – GPU 当前状态\n"
        "/today – 今天的预约\n"
        "/mine – 我接下来的预约\n"
        "/extend [分钟] – 延长我正在进行的预约（默认 30）\n"
        "/release – 立即结束我正在进行的预约\n"
        "/unlink – 解除与此聊天的绑定"
    ),
}

REPLIES = {
    "en": {
        "linked": "Linked to SlotServe account {username}. Send /help for commands.",
        "bad_token": "This link has expired. Open Settings in SlotServe and press “Link Telegram” again.",
        "not_linked": "This chat is not linked. Open Settings in SlotServe and press “Link Telegram”.",
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
    },
    "zh-CN": {
        "linked": "已绑定 SlotServe 账号 {username}。发送 /help 查看命令。",
        "bad_token": "链接已失效。请在 SlotServe 的「设置」里重新点「绑定 Telegram」。",
        "not_linked": "此聊天尚未绑定。请在 SlotServe 的「设置」里点「绑定 Telegram」。",
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
    },
}


def reply(user: User | None, key: str, **params: Any) -> str:
    return REPLIES[lang(user)][key].format(**params)


class TelegramError(Exception):
    pass


class TelegramBot:
    def __init__(self, app: FastAPI, transport: httpx.AsyncBaseTransport | None = None):
        self.app = app
        self.settings = app.state.settings
        self.transport = transport
        self.username: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._client_key: tuple | None = None
        self._reload = asyncio.Event()
        self.offset = 0

    # ---- plumbing -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    def reload(self) -> None:
        """Called after the token or proxy changed."""
        self.username = None
        self._reload.set()

    def _http(self) -> httpx.AsyncClient:
        key = (self.settings.telegram_bot_token, self.settings.telegram_proxy, self.settings.telegram_api_base)
        if self._client is None or self._client_key != key:
            if self._client is not None:
                asyncio.get_running_loop().create_task(self._client.aclose())
            self._client = httpx.AsyncClient(
                base_url=f"{self.settings.telegram_api_base}/bot{self.settings.telegram_bot_token}/",
                proxy=self.settings.telegram_proxy or None,
                transport=self.transport,
                timeout=httpx.Timeout(POLL_TIMEOUT + 15, connect=15),
            )
            self._client_key = key
        return self._client

    async def call(self, method: str, **params: Any) -> Any:
        resp = await self._http().post(method, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or resp.status_code}")
        return data.get("result")

    async def send(self, chat_id: int, message: str, buttons: list[list[tuple[str, str]]] | None = None) -> bool:
        """Returns ``False`` when Telegram could not be reached, so the caller can retry later."""
        if not self.enabled:
            return True
        params: dict[str, Any] = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]
            }
        try:
            await self.call("sendMessage", **params)
        except TelegramError as exc:
            # Telegram answered but refused (blocked bot, deleted chat): retrying will not help.
            log.warning("telegram refused message: %s", exc)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram send failed: %s", exc)
            return False
        return True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def run(self) -> None:
        delay = 5
        while True:
            self._reload.clear()
            if not self.enabled:
                await self._reload.wait()
                continue
            try:
                me = await self.call("getMe")
                self.username = me.get("username")
                log.info("telegram bot @%s connected", self.username)
                delay = 5
                while not self._reload.is_set():
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TelegramError, ValueError) as exc:
                log.warning("telegram polling error: %s (retrying in %ss)", exc, delay)
                try:
                    await asyncio.wait_for(self._reload.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 300)

    async def poll_once(self, timeout: int = POLL_TIMEOUT) -> None:
        updates = await self.call(
            "getUpdates", offset=self.offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )
        for update in updates or []:
            self.offset = max(self.offset, update["update_id"] + 1)
            try:
                await self.handle(update)
            except Exception:
                log.exception("telegram update failed")

    # ---- handling -----------------------------------------------------------

    async def handle(self, update: dict) -> None:
        if cq := update.get("callback_query"):
            chat_id = cq.get("message", {}).get("chat", {}).get("id")
            answer = await self.on_button(chat_id, cq.get("data") or "")
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer[:190])
            if answer and chat_id:
                await self.send(chat_id, answer)
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        body = (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not body.startswith("/"):
            return
        command, _, arg = body.partition(" ")
        command = command.split("@", 1)[0].lower()
        answer = await self.on_command(chat["id"], (msg.get("from") or {}).get("username"), command, arg.strip())
        if answer:
            await self.send(chat["id"], answer)

    async def _user_for_chat(self, db, chat_id: int) -> User | None:
        return (
            await db.execute(select(User).where(User.telegram_chat_id == chat_id, User.disabled_at.is_(None)))
        ).scalar_one_or_none()

    async def on_command(self, chat_id: int, tg_username: str | None, command: str, arg: str) -> str | None:
        async with SessionLocal() as db:
            if command == "/start" and arg:
                user = (await db.execute(select(User).where(User.telegram_link_token == arg))).scalar_one_or_none()
                if user is None or user.disabled_at is not None:
                    return reply(None, "bad_token")
                for other in (await db.execute(select(User).where(User.telegram_chat_id == chat_id))).scalars():
                    other.telegram_chat_id = None
                user.telegram_chat_id = chat_id
                user.telegram_username = tg_username
                user.telegram_link_token = None
                await db.commit()
                return reply(user, "linked", username=user.username)

            user = await self._user_for_chat(db, chat_id)
            if user is None:
                return reply(None, "not_linked")
            if command in ("/start", "/help"):
                return HELP[lang(user)]
            if command == "/unlink":
                user.telegram_chat_id = None
                user.telegram_username = None
                await db.commit()
                return reply(user, "unlinked")
            if command == "/status":
                return await self._status(db, user)
            if command == "/today":
                return await self._today(db, user)
            if command == "/mine":
                return await self._mine(db, user)
            if command == "/extend":
                minutes = int(arg) if arg.isdigit() else 30
                return await self._extend(db, user, None, minutes)
            if command == "/release":
                return await self._release(db, user, None)
            return HELP[lang(user)]

    async def on_button(self, chat_id: int | None, data: str) -> str:
        async with SessionLocal() as db:
            user = await self._user_for_chat(db, chat_id) if chat_id else None
            if user is None:
                return reply(None, "not_linked")
            parts = data.split(":")
            if parts[0] == "ext" and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                return await self._extend(db, user, int(parts[1]), int(parts[2]))
            if parts[0] == "rel" and len(parts) == 2 and parts[1].isdigit():
                return await self._release(db, user, int(parts[1]))
            return ""

    # ---- commands -----------------------------------------------------------

    async def _status(self, db, user: User) -> str:
        snap = self.app.state.runtime.snapshot()
        active = await queries.active_reservation(db, utcnow())
        notifier = self.app.state.notifier
        if active is None:
            holder = reply(user, "holder_free")
        else:
            owner = await db.get(User, active.user_id)
            holder = reply(user, "holder", user=owner.username if owner else "?", end=notifier.fmt_time(active.end_at))
        return reply(user, "state", model=snap.model_name or "—", state=snap.state.value, holder=holder)

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
                        Reservation.user_id == user.id, Reservation.status == "booked", Reservation.end_at > utcnow()
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
            r = await queries.active_reservation(db, utcnow())
            return r if r is not None and r.user_id == user.id else None
        r = await db.get(Reservation, reservation_id)
        return r if r is not None and (r.user_id == user.id or user.is_admin) else None

    async def _extend(self, db, user: User, reservation_id: int | None, minutes: int) -> str:
        from ..api.reservations import extend_core

        r = await self._own_reservation(db, user, reservation_id)
        if r is None:
            return reply(user, "no_active")
        try:
            r = await extend_core(db, user, r, max(5, min(minutes, 24 * 60)))
        except ApiError as exc:
            return reply(user, "error", code=exc.detail.get("code"))
        self.app.state.scheduler.poke()
        return reply(user, "extended", end=self.app.state.notifier.fmt_time(r.end_at))

    async def _release(self, db, user: User, reservation_id: int | None) -> str:
        from ..api.reservations import end_core

        r = await self._own_reservation(db, user, reservation_id)
        if r is None or queries.reservation_phase(r, utcnow()) != "active":
            return reply(user, "no_active")
        try:
            await end_core(db, user, r)
            await db.commit()
        except ApiError as exc:
            return reply(user, "error", code=exc.detail.get("code"))
        self.app.state.scheduler.poke()
        self.app.state.housekeeper.poke()
        return reply(user, "released")


def new_link_token() -> str:
    return secrets.token_urlsafe(16)
