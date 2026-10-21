"""Telegram bot: long polling (no public URL needed), account linking with a deep link, commands and buttons.

Commands and buttons are handled by ``chat.commands`` like on every other platform.
"""

import asyncio
import logging
import secrets
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from ..orm import User
from .chat.booking import Reply
from .chat.channel import Channel
from .chat.commands import HELP, REPLIES, reply

log = logging.getLogger(__name__)

POLL_TIMEOUT = 25

__all__ = ["HELP", "REPLIES", "TelegramBot", "TelegramError", "new_link_token", "reply"]


class TelegramError(Exception):
    pass


class TelegramBot(Channel):
    id = "telegram"
    label = "Telegram"
    receive_mode = "polling"
    native_buttons = True

    def __init__(self, app: FastAPI, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(app, transport)
        self._client_key: tuple | None = None
        self.offset = 0
        self.flow = self.commands.flow

    # ---- plumbing -----------------------------------------------------------

    @property
    def username(self) -> str | None:
        return self.bot_name

    @username.setter
    def username(self, value: str | None) -> None:
        self.bot_name = value

    @property
    def switched_on(self) -> bool:
        return bool(self.settings.telegram_bot_token)  # an empty token is the off switch

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token)

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

    @staticmethod
    def _markup(buttons: list[list[tuple[str, str]]] | None) -> dict:
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons or []]}

    async def send(
        self, chat_id, message: str, buttons: list[list[tuple[str, str]]] | None = None, locale: str = "en"
    ) -> bool:
        """Returns ``False`` when Telegram could not be reached, so the caller can retry later."""
        if not self.enabled:
            return True
        params: dict[str, Any] = {"chat_id": int(chat_id), "text": message, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = self._markup(buttons)
        try:
            await self.call("sendMessage", **params)
        except TelegramError as exc:
            # Telegram answered but refused (blocked bot, deleted chat): retrying will not help.
            log.warning("telegram refused message: %s", exc)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram send failed: %s", exc)
            return False
        return True

    async def run(self) -> None:
        delay = 5
        while True:
            self._reload.clear()
            if not self.enabled:
                self.connected = False
                await self._reload.wait()
                continue
            try:
                me = await self.call("getMe")
                self.mark_connected(me.get("username"))
                await self._register_commands()
                delay = 5
                while not self._reload.is_set():
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TelegramError, ValueError) as exc:
                self.connected = False
                self.error = str(exc)[:300] or type(exc).__name__
                log.warning("telegram polling error: %s (retrying in %ss)", exc, delay)
                await self._sleep(delay)
                delay = min(delay * 2, 300)

    async def _register_commands(self) -> None:
        """The command menu Telegram shows when typing "/" (English by default, Chinese for Chinese clients)."""
        for language, text in HELP.items():
            commands = [
                {"command": line[1:].split(" ", 1)[0], "description": line.split(" – ", 1)[1]}
                for line in text.splitlines()
                if line.startswith("/")
            ]
            params: dict[str, Any] = {"commands": commands}
            if language != "en":
                params["language_code"] = language.split("-")[0]
            try:
                await self.call("setMyCommands", **params)
            except TelegramError as exc:
                log.info("telegram command menu not set: %s", exc)

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
        from ..db import utcnow

        self.last_event_at = utcnow()
        if cq := update.get("callback_query"):
            message = cq.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            answer = await self.on_button(chat_id, cq.get("data") or "")
            if isinstance(answer, Reply):
                await self.call("answerCallbackQuery", callback_query_id=cq["id"])
                if chat_id and answer.edit and message.get("message_id"):
                    try:
                        await self.call(
                            "editMessageText",
                            chat_id=chat_id,
                            message_id=message["message_id"],
                            text=answer.text,
                            reply_markup=self._markup(answer.buttons),
                        )
                    except TelegramError as exc:  # e.g. the message is too old to edit
                        log.info("telegram edit failed (%s); sending instead", exc)
                        await self.send(chat_id, answer.text, answer.buttons)
                elif chat_id:
                    await self.send(chat_id, answer.text, answer.buttons)
                return
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
        if isinstance(answer, Reply):
            await self.send(chat["id"], answer.text, answer.buttons)
        elif answer:
            await self.send(chat["id"], answer)

    async def on_command(self, chat_id: int, tg_username: str | None, command: str, arg: str) -> str | Reply | None:
        return await self.commands.command(self, chat_id, tg_username, command, arg)

    async def on_button(self, chat_id: int | None, data: str) -> str | Reply:
        return await self.commands.button(self, chat_id, data)

    # ---- linking: kept on the users table -------------------------------------------

    async def find_user(self, db, address) -> User | None:
        if address is None:
            return None
        return (
            await db.execute(select(User).where(User.telegram_chat_id == int(address), User.disabled_at.is_(None)))
        ).scalar_one_or_none()

    def address_of(self, user: User, link) -> int | None:
        return user.telegram_chat_id

    async def link_of(self, db, user: User):
        return None

    async def start_link(self, db, user: User) -> dict[str, Any]:
        user.telegram_link_token = new_link_token()
        url = f"https://t.me/{self.username}?start={user.telegram_link_token}" if self.username else None
        return {"code": None, "expires_at": None, "link_url": url}

    async def redeem(self, db, code: str, address, name: str | None) -> User | None:
        user = (await db.execute(select(User).where(User.telegram_link_token == code))).scalar_one_or_none()
        if user is None or user.disabled_at is not None:
            return None
        for other in (await db.execute(select(User).where(User.telegram_chat_id == int(address)))).scalars():
            other.telegram_chat_id = None
        user.telegram_chat_id = int(address)
        user.telegram_username = name
        user.telegram_link_token = None
        return user

    async def unlink(self, db, user: User) -> None:
        user.telegram_chat_id = None
        user.telegram_username = None
        user.telegram_link_token = None


def new_link_token() -> str:
    return secrets.token_urlsafe(16)
