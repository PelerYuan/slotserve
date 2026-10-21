"""WeCom (企业微信) self-built app.

Sending uses the server API and works from anywhere WeCom's API is reachable (WeCom may require the server's
address under the app's trusted IPs). Receiving commands needs WeCom to reach SlotServe's callback URL
``/api/channels/wecom/callback``; messages there are signed and AES encrypted with the app's Token and
EncodingAESKey (WeCom's "message encryption" scheme, implemented below).
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import struct
import time
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .booking import Buttons, Reply
from .channel import Channel, ChannelError

log = logging.getLogger(__name__)

TOKEN_ERRORS = {40014, 42001, 41001}  # invalid / expired / missing access token


class WeComCryptoError(Exception):
    pass


class WeComCrypto:
    """WeCom callback encryption: SHA1 signature over the sorted strings, AES-256-CBC with PKCS#7 (block 32)."""

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        try:
            self.key = base64.b64decode(encoding_aes_key + "=")
        except ValueError as exc:
            raise WeComCryptoError("invalid EncodingAESKey") from exc
        if len(self.key) != 32:
            raise WeComCryptoError("invalid EncodingAESKey")
        self.token = token
        self.receive_id = receive_id

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        return hashlib.sha1("".join(sorted([self.token, timestamp, nonce, encrypted])).encode()).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> bool:
        return hmac.compare_digest(self.signature(timestamp, nonce, encrypted), msg_signature or "")

    def decrypt(self, encrypted: str) -> str:
        try:
            cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            raw = cipher.update(base64.b64decode(encrypted)) + cipher.finalize()
            pad = raw[-1]
            if not 1 <= pad <= 32:
                raise ValueError("bad padding")
            raw = raw[:-pad]
            length = struct.unpack(">I", raw[16:20])[0]
            message, receive_id = raw[20 : 20 + length], raw[20 + length :]
        except (ValueError, IndexError, struct.error) as exc:
            raise WeComCryptoError("cannot decrypt") from exc
        if receive_id.decode(errors="replace") != self.receive_id:
            raise WeComCryptoError("message is for another corp")
        return message.decode()

    def encrypt(self, message: str, random16: bytes | None = None) -> str:
        body = message.encode()
        raw = (random16 or os.urandom(16)) + struct.pack(">I", len(body)) + body + self.receive_id.encode()
        pad = 32 - len(raw) % 32
        raw += bytes([pad]) * pad
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        return base64.b64encode(cipher.update(raw) + cipher.finalize()).decode()


def xml_fields(body: bytes | str) -> dict[str, str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise WeComCryptoError("invalid XML") from exc
    return {child.tag: (child.text or "") for child in root}


class WeComChannel(Channel):
    id = "wecom"
    label = "WeCom"
    receive_mode = "callback"
    credential_keys = ("wecom_corp_id", "wecom_agent_id", "wecom_secret")

    def __init__(self, app, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(app, transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._seen: deque[str] = deque(maxlen=500)
        self._tasks: set[asyncio.Task] = set()

    @property
    def receives(self) -> bool:
        return bool(self.settings.wecom_token and self.settings.wecom_aes_key)

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["callback_path"] = "/api/channels/wecom/callback"
        out["receives"] = self.receives
        return out

    def reload(self) -> None:
        self._token = None
        super().reload()

    @property
    def base(self) -> str:
        return self.settings.wecom_api_base.rstrip("/")

    async def token(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token_expires:
            return self._token
        resp = await self.http().get(
            f"{self.base}/cgi-bin/gettoken",
            params={"corpid": self.settings.wecom_corp_id, "corpsecret": self.settings.wecom_secret},
        )
        data = resp.json()
        if data.get("errcode", 0) != 0 or not data.get("access_token"):
            raise ChannelError(f"gettoken: {data.get('errmsg') or resp.status_code}")
        self._token = data["access_token"]
        self._token_expires = time.monotonic() + int(data.get("expires_in", 7200)) - 300
        return self._token

    async def connect(self) -> None:
        await self.token(refresh=True)
        self.mark_connected(f"agent {self.settings.wecom_agent_id}")
        await asyncio.Event().wait()  # nothing to keep open: messages arrive at the callback URL

    async def api(self, path: str, body: dict) -> dict:
        for attempt in (0, 1):
            token = await self.token(refresh=attempt == 1)
            resp = await self.http().post(f"{self.base}{path}", params={"access_token": token}, json=body)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            data = resp.json()
            if data.get("errcode") not in TOKEN_ERRORS:
                return data
        return data

    async def send(self, address, text: str, buttons: Buttons | None = None, locale: str = "en") -> bool:
        if not self.enabled:
            return True
        body = {
            "touser": str(address),
            "msgtype": "text",
            "agentid": int(self.settings.wecom_agent_id) if str(self.settings.wecom_agent_id).isdigit() else 0,
            "text": {"content": self.render(address, text, buttons, locale)},
        }
        try:
            data = await self.api("/cgi-bin/message/send", body)
        except (httpx.HTTPError, ValueError, ChannelError) as exc:
            log.warning("wecom send failed: %s", exc)
            self.error = str(exc)[:300]
            return False
        if data.get("errcode", 0) != 0:
            log.warning("wecom refused message: %s", data.get("errmsg"))
            self.error = str(data.get("errmsg"))[:300]
        return True

    # ---- callback URL ----------------------------------------------------------

    def crypto(self) -> WeComCrypto:
        if not (self.enabled and self.receives):
            raise WeComCryptoError("receiving is not set up")
        return WeComCrypto(self.settings.wecom_token, self.settings.wecom_aes_key, self.settings.wecom_corp_id)

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """WeCom's check when the callback URL is saved: answer with the decrypted echostr."""
        crypto = self.crypto()
        if not crypto.verify(msg_signature, timestamp, nonce, echostr):
            raise WeComCryptoError("bad signature")
        return crypto.decrypt(echostr)

    def receive(self, msg_signature: str, timestamp: str, nonce: str, body: bytes) -> dict[str, str]:
        crypto = self.crypto()
        encrypted = xml_fields(body).get("Encrypt", "")
        if not encrypted or not crypto.verify(msg_signature, timestamp, nonce, encrypted):
            raise WeComCryptoError("bad signature")
        return xml_fields(crypto.decrypt(encrypted))

    def accept(self, message: dict[str, str]) -> None:
        """Handles a decrypted message in the background; the callback answers WeCom at once."""
        key = message.get("MsgId") or f"{message.get('FromUserName')}:{message.get('CreateTime')}"
        if key in self._seen:
            return  # WeCom retries when it did not get an answer in time
        self._seen.append(key)
        if message.get("MsgType") != "text" or not message.get("FromUserName"):
            return
        task = asyncio.get_running_loop().create_task(self._handle(message["FromUserName"], message.get("Content", "")))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, user_id: str, content: str) -> None:
        try:
            answer = await self.incoming(user_id, content, name=user_id)
            if isinstance(answer, Reply):
                await self.send(user_id, answer.text, answer.buttons, await self.locale_for(user_id))
            elif answer:
                await self.send(user_id, answer)
        except Exception:
            log.exception("wecom message failed")
