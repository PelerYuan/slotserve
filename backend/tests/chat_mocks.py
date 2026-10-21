"""In-process stand-ins for the Feishu, DingTalk, WeCom and QQ bot APIs and the group webhooks.

HTTP APIs are FastAPI apps reached through ``httpx.ASGITransport``; the long connections (Feishu, DingTalk
Stream, QQ gateway) are real WebSocket servers on 127.0.0.1 whose address the fake HTTP API hands out.
"""

import asyncio
import base64
import contextlib
import itertools
import json
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from websockets.asyncio.server import serve

from slotserve.services.chat.feishu import Frame, decode_frame, encode_frame


class _WsServer:
    def __init__(self):
        self.server = None
        self.port = 0
        self.ws = None
        self.connected = asyncio.Event()
        self.received: list = []

    async def start(self) -> int:
        if self.server is None:
            self.server = await serve(self._handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def _handler(self, ws):
        self.ws = ws
        self.connected.set()
        await self.opened(ws)
        with contextlib.suppress(Exception):
            async for raw in ws:
                self.received.append(raw)
                await self.on_message(ws, raw)

    async def opened(self, ws):
        pass

    async def on_message(self, ws, raw):
        pass

    async def wait_connected(self, timeout: float = 10):
        await asyncio.wait_for(self.connected.wait(), timeout)

    async def close(self):
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()


# ---- Feishu / Lark ----------------------------------------------------------------------


class FakeFeishu(_WsServer):
    def __init__(self):
        super().__init__()
        self.app = FastAPI()
        self.sent: list[dict] = []
        self.pings = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)

        @self.app.post("/open-apis/auth/v3/tenant_access_token/internal")
        async def token(request: Request):
            body = await request.json()
            if body.get("app_secret") != "fs-secret":
                return {"code": 10014, "msg": "app secret invalid"}
            return {"code": 0, "tenant_access_token": "t-123", "expire": 7200}

        @self.app.get("/open-apis/bot/v3/info")
        async def info():
            return {"code": 0, "bot": {"app_name": "SlotBot", "open_id": "ou_bot"}}

        @self.app.post("/callback/ws/endpoint")
        async def endpoint(request: Request):
            body = await request.json()
            assert body["AppID"] == "cli_test" and body["AppSecret"] == "fs-secret"
            port = await self.start()
            url = f"ws://127.0.0.1:{port}/ws?device_id=dev1&service_id=7"
            return {"code": 0, "data": {"URL": url, "ClientConfig": {"PingInterval": 120}}}

        @self.app.post("/open-apis/im/v1/messages")
        async def send(request: Request):
            assert request.headers["authorization"] == "Bearer t-123"
            body = await request.json()
            body["receive_id_type"] = request.query_params["receive_id_type"]
            body["content"] = json.loads(body["content"])
            self.sent.append(body)
            return {"code": 0, "data": {"message_id": f"om_{len(self.sent)}"}}

    async def on_message(self, ws, raw):
        frame = decode_frame(raw)
        if frame.method == 0:
            self.pings += 1
            return
        future = self._pending.pop(frame.header("message_id"), None)
        if future is not None:
            future.set_result(frame)

    async def event(self, event_type: str, event: dict, parts: int = 1) -> dict | None:
        """Pushes an event over the long connection; returns what the client answered (decoded)."""
        message_id = f"msg{next(self._ids)}"
        payload = json.dumps({"schema": "2.0", "header": {"event_type": event_type}, "event": event}).encode()
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        size = -(-len(payload) // parts)
        for seq in range(parts):
            headers = [("type", "event"), ("message_id", message_id), ("sum", str(parts)), ("seq", str(seq))]
            headers.append(("trace_id", "trace"))
            chunk = payload[seq * size : (seq + 1) * size]
            await self.ws.send(encode_frame(Frame(service=7, method=1, headers=headers, payload=chunk)))
        frame = await asyncio.wait_for(future, 10)
        response = json.loads(frame.payload)
        assert response["code"] == 200, response
        return json.loads(base64.b64decode(response["data"])) if response.get("data") else None

    async def message(self, open_id: str, text: str, chat_type: str = "p2p", chat_id: str = "oc_1"):
        content = json.dumps({"text": text})
        await self.event(
            "im.message.receive_v1",
            {
                "sender": {"sender_id": {"open_id": open_id}},
                "message": {"message_type": "text", "content": content, "chat_type": chat_type, "chat_id": chat_id},
            },
        )

    async def press(self, open_id: str, data: str) -> dict | None:
        return await self.event(
            "card.action.trigger",
            {"operator": {"open_id": open_id}, "action": {"value": {"d": data}}, "context": {"open_message_id": "om"}},
        )

    def texts(self, receive_id: str) -> list[str]:
        out = []
        for m in self.sent:
            if m["receive_id"] != receive_id:
                continue
            if m["msg_type"] == "text":
                out.append(m["content"]["text"])
            else:
                out.append(m["content"]["elements"][0]["text"]["content"])
        return out


# ---- DingTalk ------------------------------------------------------------------------------


class FakeDingTalk(_WsServer):
    def __init__(self):
        super().__init__()
        self.app = FastAPI()
        self.sent: list[dict] = []  # robot one-to-one messages
        self.replies: list[dict] = []  # session webhook answers
        self.group: list[dict] = []  # custom robot webhook
        self.acks: list[dict] = []
        self._ids = itertools.count(1)

        @self.app.post("/v1.0/oauth2/accessToken")
        async def token(request: Request):
            body = await request.json()
            assert body == {"appKey": "ding-id", "appSecret": "ding-secret"}
            return {"accessToken": "dt-token", "expireIn": 7200}

        @self.app.post("/v1.0/gateway/connections/open")
        async def open_connection(request: Request):
            body = await request.json()
            assert body["clientId"] == "ding-id"
            assert {"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"} in body["subscriptions"]
            port = await self.start()
            return {"endpoint": f"ws://127.0.0.1:{port}/connect", "ticket": "tk/1+2"}

        @self.app.post("/v1.0/robot/oToMessages/batchSend")
        async def batch_send(request: Request):
            assert request.headers["x-acs-dingtalk-access-token"] == "dt-token"
            body = await request.json()
            body["msgParam"] = json.loads(body["msgParam"])
            self.sent.append(body)
            return {"processQueryKey": "q"}

        @self.app.post("/session/{conversation}")
        async def session_webhook(conversation: str, request: Request):
            body = await request.json()
            body["conversation"] = conversation
            self.replies.append(body)
            return {"errcode": 0, "errmsg": "ok"}

        @self.app.post("/robot/send")
        async def robot_send(request: Request):
            body = await request.json()
            body["query"] = dict(request.query_params)
            self.group.append(body)
            return {"errcode": 0, "errmsg": "ok"}

    async def opened(self, ws):
        self.ticket = parse_qs(ws.request.path.split("?", 1)[1])["ticket"][0]

    async def on_message(self, ws, raw):
        self.acks.append(json.loads(raw))

    async def bot_message(self, staff_id: str, text: str, group: bool = False, nick: str = "Ding User"):
        data = {
            "conversationId": "cid1",
            "conversationType": "2" if group else "1",
            "senderStaffId": staff_id,
            "senderNick": nick,
            "msgtype": "text",
            "text": {"content": f" {text}"},
            "sessionWebhook": f"https://oapi.dingtalk.com/session/{'grp' if group else staff_id}",
            "robotCode": "ding-id",
        }
        frame = {
            "specVersion": "1.0",
            "type": "CALLBACK",
            "headers": {"topic": "/v1.0/im/bot/messages/get", "messageId": f"m{next(self._ids)}"},
            "data": json.dumps(data),
        }
        await self.ws.send(json.dumps(frame))

    async def ping(self):
        frame = {"type": "SYSTEM", "headers": {"topic": "ping", "messageId": "p1"}, "data": json.dumps({"opaque": 1})}
        await self.ws.send(json.dumps(frame))


# ---- WeCom -----------------------------------------------------------------------------------


class FakeWeCom:
    def __init__(self):
        self.app = FastAPI()
        self.sent: list[dict] = []
        self.group: list[dict] = []

        @self.app.get("/cgi-bin/gettoken")
        async def token(corpid: str, corpsecret: str):
            if corpsecret != "wc-secret":
                return {"errcode": 40001, "errmsg": "invalid credential"}
            return {"errcode": 0, "access_token": "wc-token", "expires_in": 7200}

        @self.app.post("/cgi-bin/message/send")
        async def send(access_token: str, request: Request):
            assert access_token == "wc-token"
            self.sent.append(await request.json())
            return {"errcode": 0, "errmsg": "ok"}

        @self.app.post("/cgi-bin/webhook/send")
        async def webhook(key: str, request: Request):
            body = await request.json()
            body["key"] = key
            self.group.append(body)
            return {"errcode": 0, "errmsg": "ok"}

    def texts(self, user_id: str) -> list[str]:
        return [m["text"]["content"] for m in self.sent if m["touser"] == user_id]


# ---- QQ ----------------------------------------------------------------------------------------


class FakeQQ(_WsServer):
    def __init__(self):
        super().__init__()
        self.app = FastAPI()
        self.sent: list[dict] = []
        self.identify: dict | None = None
        self.heartbeats = 0
        self._seq = itertools.count(1)

        @self.app.post("/app/getAppAccessToken")
        async def token(request: Request):
            body = await request.json()
            assert body == {"appId": "qq-app", "clientSecret": "qq-secret"}
            return {"access_token": "qq-token", "expires_in": "7200"}

        @self.app.get("/gateway")
        async def gateway(request: Request):
            assert request.headers["authorization"] == "QQBot qq-token"
            return {"url": f"ws://127.0.0.1:{await self.start()}/"}

        @self.app.post("/v2/users/{openid}/messages")
        async def c2c(openid: str, request: Request):
            body = await request.json()
            body["to"] = f"user:{openid}"
            self.sent.append(body)
            return {"id": "r1", "timestamp": 0}

        @self.app.post("/v2/groups/{group_openid}/messages")
        async def group(group_openid: str, request: Request):
            body = await request.json()
            body["to"] = f"group:{group_openid}"
            self.sent.append(body)
            return {"id": "r2", "timestamp": 0}

    async def opened(self, ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}))

    async def on_message(self, ws, raw):
        payload = json.loads(raw)
        if payload["op"] == 2:
            self.identify = payload["d"]
            ready = {"version": 1, "session_id": "s1", "user": {"username": "SlotQQ"}, "shard": [0, 1]}
            await ws.send(json.dumps({"op": 0, "s": next(self._seq), "t": "READY", "d": ready}))
        elif payload["op"] == 1:
            self.heartbeats += 1

    async def c2c(self, openid: str, text: str, msg_id: str = "m1"):
        d = {"id": msg_id, "author": {"user_openid": openid}, "content": text, "timestamp": "0"}
        await self.ws.send(json.dumps({"op": 0, "s": next(self._seq), "t": "C2C_MESSAGE_CREATE", "d": d}))

    async def group_at(self, group_openid: str, member: str, text: str, msg_id: str = "g1"):
        d = {"id": msg_id, "author": {"member_openid": member}, "content": f" {text}", "group_openid": group_openid}
        await self.ws.send(json.dumps({"op": 0, "s": next(self._seq), "t": "GROUP_AT_MESSAGE_CREATE", "d": d}))

    def texts(self, to: str) -> list[str]:
        return [m["content"] for m in self.sent if m["to"] == to]


# ---- Feishu group robot webhook -------------------------------------------------------------------


class FakeFeishuHook:
    def __init__(self):
        self.app = FastAPI()
        self.posts: list[dict] = []

        @self.app.post("/open-apis/bot/v2/hook/{token}")
        async def hook(token: str, request: Request):
            body = await request.json()
            body["token"] = token
            self.posts.append(body)
            return {"code": 0, "msg": "success"}


class FakeWebhooks:
    """Routes the three webhook hosts to their fakes (one transport serves all group webhooks)."""

    def __init__(self, feishu: FakeFeishuHook, dingtalk: FakeDingTalk, wecom: FakeWeCom):
        self.targets = {
            "open.feishu.cn": feishu.app,
            "oapi.dingtalk.com": dingtalk.app,
            "qyapi.weixin.qq.com": wecom.app,
        }

    async def __call__(self, scope, receive, send):
        host = dict(scope["headers"]).get(b"host", b"").decode().split(":")[0]
        await self.targets[host](scope, receive, send)
