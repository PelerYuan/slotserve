"""Group robot webhooks (Feishu/Lark, DingTalk, WeCom): one-way messages into a group chat.

Feishu and DingTalk can require a signature made with the robot's secret; WeCom webhooks carry their key in the URL.
"""

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from ...db import utcnow

log = logging.getLogger(__name__)

TARGETS = {
    "feishu": ("Feishu / Lark", "feishu_webhook_url", "feishu_webhook_secret"),
    "dingtalk": ("DingTalk", "dingtalk_webhook_url", "dingtalk_webhook_secret"),
    "wecom": ("WeCom", "wecom_webhook_url", None),
}


def feishu_sign(timestamp: int, secret: str) -> str:
    """Feishu custom bot: HMAC-SHA256 keyed with "timestamp\\nsecret" over an empty message."""
    digest = hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def dingtalk_sign(timestamp_ms: int, secret: str) -> str:
    """DingTalk custom robot: HMAC-SHA256 keyed with the secret over "timestamp\\nsecret", base64, URL-encoded."""
    digest = hmac.new(secret.encode(), f"{timestamp_ms}\n{secret}".encode(), hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


class GroupWebhooks:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._client: httpx.AsyncClient | None = None
        self.last_ok_at: dict[str, datetime] = {}
        self.errors: dict[str, str | None] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(15, connect=10))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def url(self, target: str) -> str:
        return getattr(self.settings, TARGETS[target][1], "") or ""

    def secret(self, target: str) -> str:
        key = TARGETS[target][2]
        return (getattr(self.settings, key, "") or "") if key else ""

    def configured_ids(self) -> list[str]:
        return [t for t in TARGETS if self.url(t)]

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t,
                "label": label,
                "configured": bool(self.url(t)),
                "signed": bool(self.secret(t)),
                "last_ok_at": self.last_ok_at.get(t),
                "error": self.errors.get(t) if self.url(t) else None,
            }
            for t, (label, _, _) in TARGETS.items()
        ]

    def request(self, target: str, text: str) -> tuple[str, dict]:
        url, secret = self.url(target), self.secret(target)
        if target == "feishu":
            body: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
            if secret:
                ts = int(time.time())
                body.update(timestamp=str(ts), sign=feishu_sign(ts, secret))
            return url, body
        if target == "dingtalk" and secret:
            ts = int(time.time() * 1000)
            url = f"{url}{'&' if '?' in url else '?'}timestamp={ts}&sign={dingtalk_sign(ts, secret)}"
        return url, {"msgtype": "text", "text": {"content": text}}

    async def send(self, target: str, text: str) -> bool:
        """``False`` when the platform could not be reached (retry later); a refusal is logged and dropped."""
        if not self.url(target):
            return True
        url, body = self.request(target, text)
        try:
            resp = await self._http().post(url, json=body)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.errors[target] = str(exc)[:300] or type(exc).__name__
            log.warning("%s webhook failed: %s", target, exc)
            return False
        code = data.get("code", data.get("errcode", data.get("StatusCode", 0)))
        if resp.status_code >= 400 or code not in (0, None):
            self.errors[target] = str(data.get("msg") or data.get("errmsg") or resp.status_code)[:300]
            log.warning("%s webhook refused the message: %s", target, self.errors[target])
            return True
        self.errors[target] = None
        self.last_ok_at[target] = utcnow()
        return True

    async def test(self, target: str, text: str) -> str | None:
        """Sends ``text`` now; returns the error, or ``None`` when the platform accepted it."""
        self.errors[target] = None
        ok = await self.send(target, text)
        return self.errors.get(target) or (None if ok else "unreachable")
