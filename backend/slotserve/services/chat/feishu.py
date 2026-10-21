"""Feishu / Lark self-built app bot over the long connection (WebSocket) event mode: no public URL needed.

Receives ``im.message.receive_v1`` (private messages, and group messages that @mention the bot) and
``card.action.trigger`` (button presses on interactive cards). Sends with the IM API using the tenant token.

The long connection exchanges protobuf ``pbbp2.Frame`` messages; the few fields are encoded by hand below
instead of pulling in the SDK (whose WebSocket client needs an older ``websockets`` than uvicorn's).
"""

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .booking import Buttons, Reply
from .channel import Channel, ChannelError

log = logging.getLogger(__name__)

TOKEN_ERRORS = {99991663, 99991661, 99991668}  # invalid / missing / expired tenant token
METHOD_CONTROL, METHOD_DATA = 0, 1
MENTION = re.compile(r"@_user_\d+\s*")


# ---- protobuf frame (pbbp2.Frame) ---------------------------------------------------


@dataclass
class Frame:
    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: list[tuple[str, str]] = field(default_factory=list)
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    log_id_new: str = ""

    def header(self, key: str, default: str = "") -> str:
        return next((v for k, v in self.headers if k == key), default)


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _field(number: int, value: int | bytes | str) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(raw)) + raw


def _fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, value


def encode_frame(frame: Frame) -> bytes:
    out = _field(1, frame.seq_id) + _field(2, frame.log_id) + _field(3, frame.service) + _field(4, frame.method)
    for key, value in frame.headers:
        out += _field(5, _field(1, key) + _field(2, value))
    if frame.payload_encoding:
        out += _field(6, frame.payload_encoding)
    if frame.payload_type:
        out += _field(7, frame.payload_type)
    if frame.payload:
        out += _field(8, frame.payload)
    if frame.log_id_new:
        out += _field(9, frame.log_id_new)
    return out


def decode_frame(data: bytes) -> Frame:
    frame = Frame()
    for number, value in _fields(data):
        if number == 1:
            frame.seq_id = value
        elif number == 2:
            frame.log_id = value
        elif number == 3:
            frame.service = value
        elif number == 4:
            frame.method = value
        elif number == 5:
            kv = dict(_fields(value))
            frame.headers.append((kv.get(1, b"").decode(), kv.get(2, b"").decode()))
        elif number == 6:
            frame.payload_encoding = value.decode()
        elif number == 7:
            frame.payload_type = value.decode()
        elif number == 8:
            frame.payload = bytes(value)
        elif number == 9:
            frame.log_id_new = value.decode()
    return frame


def card(text: str, buttons: Buttons | None = None) -> dict:
    """An interactive card (message card JSON 1.0): the text, then one row of buttons per button row."""
    elements: list[dict] = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    for row in buttons or []:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label},
                        "type": "default",
                        "value": {"d": data},
                    }
                    for label, data in row
                ],
            }
        )
    return {"config": {"wide_screen_mode": True, "update_multi": True}, "elements": elements}


class FeishuChannel(Channel):
    id = "feishu"
    label = "Feishu / Lark"
    receive_mode = "long_connection"
    native_buttons = True
    credential_keys = ("feishu_app_id", "feishu_app_secret")

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(app, transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._ws = None
        self._ping_interval = 120.0
        self._parts: dict[str, list[bytes | None]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._seen: deque[str] = deque(maxlen=500)

    @property
    def base(self) -> str:
        return self.settings.feishu_domain.rstrip("/")

    def reload(self) -> None:
        self._token = None
        super().reload()

    def open_url(self) -> str | None:
        app_id = self.settings.feishu_app_id
        host = "applink.larksuite.com" if "larksuite" in self.base else "applink.feishu.cn"
        return f"https://{host}/client/bot/open?appId={app_id}" if app_id else None

    # ---- API ------------------------------------------------------------------

    async def token(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token_expires:
            return self._token
        resp = await self.http().post(
            f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.settings.feishu_app_id, "app_secret": self.settings.feishu_app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise ChannelError(f"tenant token: {data.get('msg') or resp.status_code}")
        self._token = data["tenant_access_token"]
        self._token_expires = time.monotonic() + int(data.get("expire", 7200)) - 300
        return self._token

    async def api(self, method: str, path: str, **kwargs: Any) -> dict:
        for attempt in (0, 1):
            token = await self.token(refresh=attempt == 1)
            resp = await self.http().request(
                method, f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            data = resp.json()
            if data.get("code") not in TOKEN_ERRORS:
                return data
        return data

    async def send(self, address, text: str, buttons: Buttons | None = None, locale: str = "en") -> bool:
        """``address`` is a user's open_id, or ``chat:<chat_id>`` for a group."""
        if not self.enabled:
            return True
        address = str(address)
        id_type, receive_id = ("chat_id", address[5:]) if address.startswith("chat:") else ("open_id", address)
        if buttons:
            body = {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card(text, buttons))}
        else:
            body = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text})}
        try:
            data = await self.api("POST", "/open-apis/im/v1/messages", params={"receive_id_type": id_type}, json=body)
        except (httpx.HTTPError, ValueError, ChannelError) as exc:
            log.warning("feishu send failed: %s", exc)
            return False
        if data.get("code") != 0:
            log.warning("feishu refused message: %s", data.get("msg"))
            self.error = str(data.get("msg"))[:300]
        return True

    # ---- long connection --------------------------------------------------------

    async def endpoint(self) -> str:
        resp = await self.http().post(
            f"{self.base}/callback/ws/endpoint",
            json={"AppID": self.settings.feishu_app_id, "AppSecret": self.settings.feishu_app_secret},
            headers={"locale": "zh"},
        )
        data = resp.json()
        if data.get("code") != 0 or not (data.get("data") or {}).get("URL"):
            raise ChannelError(f"long connection endpoint: {data.get('msg') or resp.status_code}")
        self._configure((data["data"].get("ClientConfig")) or {})
        return data["data"]["URL"]

    def _configure(self, config: dict) -> None:
        if config.get("PingInterval"):
            self._ping_interval = float(config["PingInterval"])

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        await self.token(refresh=True)
        info = await self.api("GET", "/open-apis/bot/v3/info")
        url = await self.endpoint()
        service = int((parse_qs(urlsplit(url).query).get("service_id") or ["0"])[0] or 0)
        async with connect(url, open_timeout=20, max_size=None) as ws:
            self._ws = ws
            self.mark_connected((info.get("bot") or {}).get("app_name"))
            pinger = asyncio.create_task(self._ping(ws, service))
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        await self._frame(ws, decode_frame(raw))
            finally:
                pinger.cancel()
                self._ws = None

    async def _ping(self, ws, service: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await ws.send(encode_frame(Frame(service=service, method=METHOD_CONTROL, headers=[("type", "ping")])))
            await asyncio.sleep(self._ping_interval)

    async def _frame(self, ws, frame: Frame) -> None:
        kind = frame.header("type")
        if frame.method == METHOD_CONTROL:
            if kind == "pong" and frame.payload:
                with contextlib.suppress(ValueError):
                    self._configure(json.loads(frame.payload))
            return
        payload = self._combine(frame)
        if payload is None:
            return
        started = time.monotonic()
        result: Any = None
        code = 200
        try:
            if kind == "event":
                result = await self.event(json.loads(payload))
        except Exception:
            log.exception("feishu event failed")
            code = 500
        response: dict[str, Any] = {"code": code}
        if result is not None:
            response["data"] = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        frame.headers.append(("biz_rt", str(int((time.monotonic() - started) * 1000))))
        frame.payload = json.dumps(response).encode()
        await ws.send(encode_frame(frame))

    def _combine(self, frame: Frame) -> bytes | None:
        total = int(frame.header("sum", "1") or 1)
        if total <= 1:
            return frame.payload
        message_id, seq = frame.header("message_id"), int(frame.header("seq", "0") or 0)
        parts = self._parts.setdefault(message_id, [None] * total)
        parts[seq] = frame.payload
        if any(p is None for p in parts):
            return None
        del self._parts[message_id]
        return b"".join(p for p in parts if p)

    # ---- events ---------------------------------------------------------------------

    async def event(self, body: dict) -> Any:
        header = body.get("header") or {}
        event = body.get("event") or {}
        kind = header.get("event_type")
        if event_id := header.get("event_id"):
            if event_id in self._seen:
                return None  # Feishu delivers again when it missed the acknowledgement
            self._seen.append(event_id)
        if kind == "im.message.receive_v1":
            task = asyncio.get_running_loop().create_task(self._message(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return None
        if kind == "card.action.trigger":
            return await self._card_action(event)
        return None

    async def _message(self, event: dict) -> None:
        message = event.get("message") or {}
        sender = (event.get("sender") or {}).get("sender_id") or {}
        open_id = sender.get("open_id")
        if not open_id or message.get("message_type") != "text":
            return
        try:
            content = json.loads(message.get("content") or "{}").get("text", "")
        except ValueError:
            return
        group = message.get("chat_type") != "p2p"
        text = MENTION.sub("", content).strip()
        try:
            answer = await self.incoming(open_id, text, group=group)
            target = f"chat:{message.get('chat_id')}" if group else open_id
            if isinstance(answer, Reply):
                await self.send(target, answer.text, None if group else answer.buttons)
            elif answer:
                await self.send(target, answer)
        except Exception:
            log.exception("feishu message failed")

    async def _card_action(self, event: dict) -> dict:
        from ...db import utcnow

        self.last_event_at = utcnow()
        open_id = (event.get("operator") or {}).get("open_id")
        data = ((event.get("action") or {}).get("value") or {}).get("d", "")
        answer = await self.commands.button(self, open_id, str(data))
        if isinstance(answer, Reply):
            if answer.edit:
                return {"card": {"type": "raw", "data": card(answer.text, answer.buttons)}}
            await self.send(open_id, answer.text, answer.buttons)
            return {}
        if answer:
            await self.send(open_id, answer)
            return {"toast": {"type": "info", "content": answer[:100]}}
        return {}
