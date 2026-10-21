"""QQ official bot (QQ open platform) over its WebSocket gateway: private (C2C) messages and @mentions in groups.

Answers are passive replies to the message being answered (``msg_id``), which QQ allows for a while after
each message. Notifications are proactive messages; QQ limits or refuses those depending on the bot's
permissions, so they may not arrive. Buttons become numbered choices.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import httpx

from .booking import Buttons, Reply
from .channel import Channel, ChannelError

log = logging.getLogger(__name__)

INTENT_GROUP_AND_C2C = 1 << 25
OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME, OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO = 0, 1, 2, 6, 7, 9, 10
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"


class QQChannel(Channel):
    id = "qq"
    label = "QQ"
    receive_mode = "gateway"
    credential_keys = ("qq_app_id", "qq_app_secret")

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(app, transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._session_id: str | None = None
        self._seq: int | None = None
        self._reply_seq: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def base(self) -> str:
        default = "https://api.sgroup.qq.com"
        if self.settings.qq_sandbox and self.settings.qq_api_base.rstrip("/") == default:
            return SANDBOX_BASE
        return self.settings.qq_api_base.rstrip("/")

    def reload(self) -> None:
        self._token = None
        self._session_id = None
        self._seq = None
        super().reload()

    async def token(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token_expires:
            return self._token
        resp = await self.http().post(
            self.settings.qq_token_url,
            json={"appId": self.settings.qq_app_id, "clientSecret": self.settings.qq_app_secret},
        )
        data = resp.json()
        if not data.get("access_token"):
            raise ChannelError(f"access token: {data.get('message') or data.get('code') or resp.status_code}")
        self._token = data["access_token"]
        self._token_expires = time.monotonic() + int(data.get("expires_in") or 7200) - 60
        return self._token

    async def api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in (0, 1):
            token = await self.token(refresh=attempt == 1)
            headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": self.settings.qq_app_id}
            resp = await self.http().request(method, f"{self.base}{path}", headers=headers, **kwargs)
            if resp.status_code != 401:
                break
        if resp.status_code >= 500:
            raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
        return resp

    async def post_message(self, path: str, content: str, msg_id: str | None = None) -> bool:
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            seq = self._reply_seq.get(msg_id, 0) + 1
            self._reply_seq[msg_id] = seq
            if len(self._reply_seq) > 1000:
                self._reply_seq.clear()
            body.update(msg_id=msg_id, msg_seq=seq)
        try:
            resp = await self.api("POST", path, json=body)
        except (httpx.HTTPError, ValueError, ChannelError) as exc:
            log.warning("qq send failed: %s", exc)
            return False
        if resp.status_code >= 400:
            log.warning("qq refused message: %s", resp.text[:300])
            self.error = resp.text[:300]
        return True

    async def send(self, address, text: str, buttons: Buttons | None = None, locale: str = "en") -> bool:
        if not self.enabled:
            return True
        return await self.post_message(f"/v2/users/{address}/messages", self.render(address, text, buttons, locale))

    # ---- gateway ------------------------------------------------------------------

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        await self.token()
        resp = await self.api("GET", "/gateway")
        url = resp.json().get("url")
        if not url:
            raise ChannelError(f"gateway: {resp.text[:200]}")
        async with connect(url, open_timeout=20, max_size=None) as ws:
            heartbeat: asyncio.Task | None = None
            try:
                async for raw in ws:
                    payload = json.loads(raw)
                    op = payload.get("op")
                    if payload.get("s") is not None:
                        self._seq = payload["s"]
                    if op == OP_HELLO:
                        interval = (payload.get("d") or {}).get("heartbeat_interval") or 30000
                        heartbeat = asyncio.create_task(self._heartbeat(ws, interval / 1000))
                        await self._identify(ws)
                    elif op == OP_DISPATCH:
                        await self._dispatch(payload.get("t"), payload.get("d") or {})
                    elif op == OP_RECONNECT:
                        return
                    elif op == OP_INVALID_SESSION:
                        self._session_id = None
                        self._seq = None
                        raise ChannelError("invalid session (check the bot's intents and permissions)")
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()

    async def _identify(self, ws) -> None:
        token = f"QQBot {await self.token()}"
        if self._session_id:
            d = {"token": token, "session_id": self._session_id, "seq": self._seq or 0}
            await ws.send(json.dumps({"op": OP_RESUME, "d": d}))
        else:
            d = {"token": token, "intents": INTENT_GROUP_AND_C2C, "shard": [0, 1], "properties": {}}
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": d}))

    async def _heartbeat(self, ws, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))

    async def _dispatch(self, event: str | None, d: dict) -> None:
        if event == "READY":
            self._session_id = d.get("session_id")
            self.mark_connected((d.get("user") or {}).get("username"))
        elif event == "RESUMED":
            self.mark_connected()
        elif event in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            task = asyncio.get_running_loop().create_task(self._message(event, d))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _message(self, event: str, d: dict) -> None:
        author = d.get("author") or {}
        group = event == "GROUP_AT_MESSAGE_CREATE"
        address = author.get("member_openid") if group else author.get("user_openid")
        if not address:
            return
        try:
            answer = await self.incoming(address, d.get("content") or "", group=group)
            if not answer:
                return
            path = f"/v2/groups/{d.get('group_openid')}/messages" if group else f"/v2/users/{address}/messages"
            if isinstance(answer, Reply):
                buttons = None if group else answer.buttons
                text = self.render(address, answer.text, buttons, await self.locale_for(address))
            else:
                text = answer
            await self.post_message(path, text, msg_id=d.get("id"))
        except Exception:
            log.exception("qq message failed")
