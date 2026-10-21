"""DingTalk internal app robot in Stream mode: a WebSocket SlotServe opens itself, so no public URL is needed.

Robot messages arrive on the ``/v1.0/im/bot/messages/get`` callback topic. Answers go back through the
message's session webhook; notifications use the robot's one-to-one message API. DingTalk robot messages
have no callback buttons without a card template, so choices are numbered markdown links: tapping one
sends its number back to the robot (``dtmd://dingtalkclient/sendMessage``).
"""

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from .booking import Buttons, Reply
from .channel import PICK_HINT, Channel, ChannelError

log = logging.getLogger(__name__)

BOT_TOPIC = "/v1.0/im/bot/messages/get"


def markdown(channel: Channel, address, text: str, buttons: Buttons | None, locale: str) -> str:
    lines = [line for line in text.split("\n")]
    body = "  \n".join(lines)
    rows = channel.number(address, buttons)
    if not rows:
        return body
    links = [
        "   ".join(f"[{n}) {label}](dtmd://dingtalkclient/sendMessage?content={quote(str(n))})" for n, label, _ in row)
        for row in rows
    ]
    return body + "  \n\n" + "  \n".join(links) + "  \n\n" + PICK_HINT.get(locale, PICK_HINT["en"])


class DingTalkChannel(Channel):
    id = "dingtalk"
    label = "DingTalk"
    receive_mode = "stream"
    credential_keys = ("dingtalk_client_id", "dingtalk_client_secret")

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(app, transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._tasks: set[asyncio.Task] = set()

    @property
    def base(self) -> str:
        return self.settings.dingtalk_api_base.rstrip("/")

    @property
    def robot_code(self) -> str:
        return self.settings.dingtalk_robot_code or self.settings.dingtalk_client_id

    def reload(self) -> None:
        self._token = None
        super().reload()

    async def token(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token_expires:
            return self._token
        resp = await self.http().post(
            f"{self.base}/v1.0/oauth2/accessToken",
            json={"appKey": self.settings.dingtalk_client_id, "appSecret": self.settings.dingtalk_client_secret},
        )
        data = resp.json()
        if not data.get("accessToken"):
            raise ChannelError(f"access token: {data.get('message') or data.get('code') or resp.status_code}")
        self._token = data["accessToken"]
        self._token_expires = time.monotonic() + int(data.get("expireIn", 7200)) - 300
        return self._token

    # ---- sending ----------------------------------------------------------------

    def _message(self, address, text: str, buttons: Buttons | None, locale: str) -> tuple[str, dict]:
        if buttons:
            return "sampleMarkdown", {
                "title": text.split("\n", 1)[0][:30],
                "text": markdown(self, address, text, buttons, locale),
            }
        return "sampleText", {"content": text}

    async def send(self, address, text: str, buttons: Buttons | None = None, locale: str = "en") -> bool:
        if not self.enabled:
            return True
        key, param = self._message(address, text, buttons, locale)
        body = {"robotCode": self.robot_code, "userIds": [str(address)], "msgKey": key, "msgParam": json.dumps(param)}
        try:
            for attempt in (0, 1):
                token = await self.token(refresh=attempt == 1)
                resp = await self.http().post(
                    f"{self.base}/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": token},
                    json=body,
                )
                if resp.status_code != 401:
                    break
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
        except (httpx.HTTPError, ValueError, ChannelError) as exc:
            log.warning("dingtalk send failed: %s", exc)
            return False
        if resp.status_code >= 400:
            log.warning("dingtalk refused message: %s", resp.text[:300])
            self.error = resp.text[:300]
        return True

    async def reply(self, webhook: str, address, answer: str | Reply, locale: str, group: bool) -> None:
        """Answers through the session webhook of the message being answered."""
        if isinstance(answer, Reply):
            text, buttons = answer.text, (None if group else answer.buttons)
        else:
            text, buttons = answer, None
        if buttons:
            body: dict[str, Any] = {
                "msgtype": "markdown",
                "markdown": {
                    "title": text.split("\n", 1)[0][:30],
                    "text": markdown(self, address, text, buttons, locale),
                },
            }
        else:
            body = {"msgtype": "text", "text": {"content": text}}
        resp = await self.http().post(webhook, json=body)
        if resp.status_code >= 400:
            log.warning("dingtalk reply refused: %s", resp.text[:300])

    # ---- stream connection --------------------------------------------------------

    async def connect(self) -> None:
        from websockets.asyncio.client import connect

        await self.token(refresh=True)
        resp = await self.http().post(
            f"{self.base}/v1.0/gateway/connections/open",
            json={
                "clientId": self.settings.dingtalk_client_id,
                "clientSecret": self.settings.dingtalk_client_secret,
                "subscriptions": [{"type": "CALLBACK", "topic": BOT_TOPIC}],
                "ua": "slotserve",
            },
        )
        data = resp.json()
        if not data.get("endpoint") or not data.get("ticket"):
            raise ChannelError(f"stream connection: {data.get('message') or data.get('code') or resp.status_code}")
        url = f"{data['endpoint']}?ticket={quote_plus(data['ticket'])}"
        async with connect(url, open_timeout=20, max_size=None) as ws:
            self.mark_connected(self.robot_code)
            async for raw in ws:
                frame = json.loads(raw)
                if await self._frame(ws, frame) == "disconnect":
                    return

    async def _frame(self, ws, frame: dict) -> str | None:
        headers = frame.get("headers") or {}
        topic = headers.get("topic")
        ack = {
            "code": 200,
            "headers": {"contentType": "application/json", "messageId": headers.get("messageId")},
            "message": "OK",
            "data": frame.get("data") or "",
        }
        if frame.get("type") == "SYSTEM":
            if topic == "disconnect":
                return "disconnect"
            await ws.send(json.dumps(ack))  # ping: echo it back
            return None
        if frame.get("type") == "CALLBACK" and topic == BOT_TOPIC:
            ack["data"] = json.dumps({"response": None})
            await ws.send(json.dumps(ack))
            task = asyncio.get_running_loop().create_task(self._bot_message(json.loads(frame.get("data") or "{}")))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return None
        ack["code"] = 404
        ack["message"] = "unknown topic"
        await ws.send(json.dumps(ack))
        return None

    async def _bot_message(self, message: dict) -> None:
        address = message.get("senderStaffId")
        content = ((message.get("text") or {}).get("content") or "").strip()
        webhook = message.get("sessionWebhook")
        if not address or message.get("msgtype", "text") != "text":
            return
        group = str(message.get("conversationType")) == "2"
        try:
            answer = await self.incoming(address, content, name=message.get("senderNick"), group=group)
            if not answer:
                return
            locale = await self.locale_for(address)
            if webhook:
                await self.reply(webhook, address, answer, locale, group)
            elif isinstance(answer, Reply):
                await self.send(address, answer.text, answer.buttons, locale)
            else:
                await self.send(address, answer)
        except Exception:
            log.exception("dingtalk message failed")
