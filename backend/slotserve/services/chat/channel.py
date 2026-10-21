"""What every chat platform has in common: settings, the connection loop, linking accounts and rendering buttons."""

import asyncio
import contextlib
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select

from ...db import utcnow
from ...orm import ChatLink, User
from ..notify import lang
from .booking import Buttons, Reply
from .commands import ChatCommands, parse

log = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CODE_MINUTES = 30
CHOICES_SECONDS = 3600

PICK_HINT = {"en": "Reply with a number.", "zh-CN": "回复编号进行选择。"}


class ChannelError(Exception):
    """The platform answered but refused (wrong credentials, missing permission, ...)."""


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


class Channel:
    """Base class. Subclasses set ``id`` and ``label`` and implement ``configured``, ``connect`` and ``send``."""

    id = ""
    label = ""
    # How messages reach SlotServe: polling | long_connection | stream | gateway | callback
    receive_mode = "long_connection"
    native_buttons = False  # False: buttons become numbered choices
    credential_keys: tuple[str, ...] = ()

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        self.app = app
        self.settings = app.state.settings
        self.transport = transport
        self.commands = ChatCommands(app)
        self.bot_name: str | None = None
        self.connected = False
        self.error: str | None = None
        self.last_event_at: datetime | None = None
        self._reload = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._choices: dict[str, tuple[float, list[str]]] = {}

    # ---- settings -------------------------------------------------------------

    @property
    def switched_on(self) -> bool:
        return bool(getattr(self.settings, f"{self.id}_enabled", False))

    @property
    def configured(self) -> bool:
        return all(getattr(self.settings, key, "") for key in self.credential_keys)

    @property
    def enabled(self) -> bool:
        return self.switched_on and self.configured

    def reload(self) -> None:
        """Called after one of the channel's settings changed."""
        self.connected = False
        self.error = None
        self.bot_name = None
        self._reload.set()

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "switched_on": self.switched_on,
            "configured": self.configured,
            "enabled": self.enabled,
            "connected": self.connected and self.enabled,
            "error": self.error if self.enabled else None,
            "bot_name": self.bot_name,
            "receive_mode": self.receive_mode,
            "last_event_at": self.last_event_at,
        }

    # ---- plumbing ---------------------------------------------------------------

    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(20, connect=10))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self) -> None:
        """Keeps the connection up while the channel is enabled; reconnects with back-off."""
        delay = 5
        while True:
            self._reload.clear()
            if not self.enabled:
                self.connected = False
                await self._reload.wait()
                continue
            try:
                await self._until_reload(self.connect())
                if self._reload.is_set():
                    continue
                self.connected = False
                await self._sleep(2)  # the platform closed the connection: reconnect soon
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure: report it and try again later
                self.connected = False
                self.error = str(exc)[:300] or type(exc).__name__
                log.warning("%s: %s (retrying in %ss)", self.id, self.error, delay)
                await self._sleep(delay)
                delay = min(delay * 2, 300)
            else:
                delay = 5

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._reload.wait(), seconds)

    async def _until_reload(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(self._reload.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
        finally:
            for t in (task, waiter):
                t.cancel()
            for t in (task, waiter):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    def mark_connected(self, bot_name: str | None = None) -> None:
        self.connected = True
        self.error = None
        if bot_name:
            self.bot_name = bot_name
        log.info("%s connected%s", self.id, f" as {bot_name}" if bot_name else "")

    async def connect(self) -> None:
        """Receives messages until the connection ends. Channels that only send just check their credentials."""
        raise NotImplementedError

    async def send(self, address, text: str, buttons: Buttons | None = None, locale: str = "en") -> bool:
        """Sends a message on the platform's own initiative.

        Returns ``False`` when the platform could not be reached, so the caller retries later.
        """
        raise NotImplementedError

    # ---- linking ------------------------------------------------------------------

    async def find_user(self, db, address) -> User | None:
        return (
            await db.execute(
                select(User)
                .join(ChatLink, ChatLink.user_id == User.id)
                .where(ChatLink.channel == self.id, ChatLink.address == str(address), User.disabled_at.is_(None))
            )
        ).scalar_one_or_none()

    async def link_of(self, db, user: User) -> ChatLink | None:
        return (
            await db.execute(select(ChatLink).where(ChatLink.user_id == user.id, ChatLink.channel == self.id))
        ).scalar_one_or_none()

    def address_of(self, user: User, link: ChatLink | None):
        return link.address if link is not None else None

    async def start_link(self, db, user: User) -> dict[str, Any]:
        """Creates a bind code the person sends to the bot. Caller commits."""
        link = await self.link_of(db, user)
        if link is None:
            link = ChatLink(user_id=user.id, channel=self.id)
            db.add(link)
        link.code = new_code()
        link.code_expires_at = utcnow() + timedelta(minutes=CODE_MINUTES)
        return {"code": link.code, "expires_at": link.code_expires_at, "link_url": self.open_url()}

    def open_url(self) -> str | None:
        """A link that opens the chat with the bot, where the platform has one."""
        return None

    async def redeem(self, db, code: str, address, name: str | None) -> User | None:
        """Links ``address`` to the person who was given ``code``. Caller commits."""
        code = code.strip().upper()
        if len(code) != 6:
            return None
        link = (
            await db.execute(select(ChatLink).where(ChatLink.channel == self.id, ChatLink.code == code))
        ).scalar_one_or_none()
        if link is None or link.code_expires_at is None or link.code_expires_at < utcnow():
            return None
        user = await db.get(User, link.user_id)
        if user is None or user.disabled_at is not None:
            return None
        # One SlotServe account per platform account.
        await db.execute(
            delete(ChatLink).where(
                ChatLink.channel == self.id, ChatLink.address == str(address), ChatLink.id != link.id
            )
        )
        link.address = str(address)
        link.display_name = (name or "")[:128] or None
        link.code = None
        link.code_expires_at = None
        link.linked_at = utcnow()
        return user

    async def unlink(self, db, user: User) -> None:
        await db.execute(delete(ChatLink).where(ChatLink.user_id == user.id, ChatLink.channel == self.id))

    # ---- incoming messages ---------------------------------------------------------

    async def incoming(self, address, text: str, *, name: str | None = None, group: bool = False) -> str | Reply | None:
        """Handles a text message: a numbered choice, a command, or anything else (answered with help)."""
        self.last_event_at = utcnow()
        text = (text or "").strip()
        if not text:
            return None
        if not group and text.isdigit() and (data := self.take_choice(address, int(text))):
            return await self.commands.button(self, address, data)
        parsed = parse(text)
        if parsed is None:
            if group:
                return None  # people talk in groups; only answer commands there
            parsed = ("/help", "")
        command, arg = parsed
        return await self.commands.command(self, address, name, command, arg, group=group)

    # ---- numbered choices for platforms without buttons --------------------------

    def render(self, address, text: str, buttons: Buttons | None, locale: str = "en") -> str:
        """Appends buttons as numbered choices and remembers them, so a reply "2" presses the second one."""
        rows = self.number(address, buttons)
        if not rows:
            return text
        lines = ["   ".join(f"{n}) {label}" for n, label, _ in row) for row in rows]
        return f"{text}\n\n" + "\n".join(lines) + f"\n{PICK_HINT.get(locale, PICK_HINT['en'])}"

    def number(self, address, buttons: Buttons | None) -> list[list[tuple[int, str, str]]]:
        if not buttons:
            return []
        flat: list[str] = []
        rows = []
        for row in buttons:
            numbered = []
            for label, data in row:
                flat.append(data)
                numbered.append((len(flat), label, data))
            rows.append(numbered)
        self._choices[str(address)] = (time.monotonic(), flat)
        if len(self._choices) > 5000:
            cutoff = time.monotonic() - CHOICES_SECONDS
            self._choices = {k: v for k, v in self._choices.items() if v[0] > cutoff}
        return rows

    def take_choice(self, address, number: int) -> str | None:
        stored = self._choices.get(str(address))
        if stored is None or time.monotonic() - stored[0] > CHOICES_SECONDS:
            return None
        flat = stored[1]
        return flat[number - 1] if 1 <= number <= len(flat) else None

    async def locale_for(self, address) -> str:
        from ...db import SessionLocal

        async with SessionLocal() as db:
            user = await self.find_user(db, address)
        return lang(user) if user is not None else self.settings.chat_default_locale
