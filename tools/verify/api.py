"""A small client for SlotServe's public HTTP API, as a signed-in person (session cookie)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx


class ApiFailure(Exception):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path}: HTTP {status} {body[:300]}")
        self.status = status
        self.body = body

    @property
    def code(self) -> str:
        try:
            detail = httpx.Response(200, content=self.body).json().get("detail")
            return detail.get("code", "") if isinstance(detail, dict) else str(detail)
        except ValueError:
            return ""


class Api:
    """Sends ``X-SlotServe: 1`` like SlotServe's own frontend (CsrfGuard); enters sudo mode when asked."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None, verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.verify_tls = verify_tls
        self.http = self.client(headers={"X-SlotServe": "1"})
        self.password = ""
        self.user: dict = {}

    def client(self, headers: dict | None = None, follow: bool = False) -> httpx.AsyncClient:
        """Another client to the same server (sharing nothing unless cookies are passed)."""
        kw: dict[str, Any] = (
            {"transport": self.transport} if self.transport is not None else {"verify": self.verify_tls}
        )
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(120, connect=10),
            follow_redirects=follow,
            **kw,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def login(self, username: str, password: str) -> dict:
        self.password = password
        self.user = await self.call("POST", "/api/auth/login", json={"username": username, "password": password})
        return self.user

    async def call(self, method: str, path: str, expect: tuple[int, ...] = (200, 201, 202, 204), **kw) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code == 403 and "sudo_required" in r.text and self.password:
            await self.http.post("/api/auth/sudo", json={"password": self.password})
            r = await self.http.request(method, path, **kw)
        if r.status_code not in expect:
            raise ApiFailure(method, path, r.status_code, r.text)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    async def get(self, path: str, **kw) -> Any:
        return await self.call("GET", path, **kw)


async def wait_for(
    what: str, probe: Callable[[], Awaitable[Any]], timeout: float, interval: float = 2.0, log=None
) -> Any:
    """Calls ``probe`` until it returns something truthy; raises TimeoutError naming ``what``.

    ``probe`` may raise ``RuntimeError`` to stop early (a failed state)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while True:
        last = await probe()
        if last:
            return last
        if loop.time() >= deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")
        await asyncio.sleep(interval)
