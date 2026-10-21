"""Reverse proxy to the ports of dev environments: ``/devenv/<session>/port/<container port>/...`` (HTTP and
WebSocket).

Any port of a running environment can be opened; the ports a template declares carry a kind (desktop, JupyterLab,
VS Code, http) that decides whether the app gets the full path and which token is added. Upstream addresses come
from ``DevEnvManager.port_target(session, port)`` only.

Only people signed in to SlotServe who may use the environment get through. SlotServe's own session cookie is
never passed on, and where the app supports it the proxy adds the environment's random token
(JupyterLab: ``Authorization: token ...``, the desktop: HTTP basic auth), so other containers or people on the host
cannot use the app without going through SlotServe.
"""

import asyncio
import base64
import contextlib
import logging
import re
from dataclasses import dataclass
from html import escape
from urllib.parse import quote, urlsplit

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

from ..db import SessionLocal
from ..deps import SESSION_USER_KEY
from ..orm import DevSession, User
from ..services.devenv import can_access, port_entry, session_web, web_base

log = logging.getLogger(__name__)
router = APIRouter(tags=["devenv"], include_in_schema=False)

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
SESSION_COOKIE = "slotserve_session"
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# request headers that are recomputed or must not reach the app
DROP_REQUEST = HOP_BY_HOP | {"host", "cookie", "authorization", "origin", "referer", "content-length"}
WS_DROP = DROP_REQUEST | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
    "sec-websocket-accept",
}
COOKIE_PATH = re.compile(r";\s*path=[^;]*", re.I)


class Denied(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code


@dataclass
class Target:
    session_id: int
    port: int
    kind: str  # desktop | jupyter | vscode | http
    strip: bool  # the app does not know its base path: send it the part after the prefix
    token: str
    base: str  # http://host:port

    @property
    def prefix(self) -> str:
        return web_base(self.session_id, self.port).rstrip("/")

    @property
    def netloc(self) -> str:
        return urlsplit(self.base).netloc


async def _target(app, scope_session: dict, session_id: int, port: int) -> Target:
    """Where a request for one of a session's ports goes, after checking who is asking."""
    uid = scope_session.get(SESSION_USER_KEY)
    if uid is None:
        raise Denied(401, "not_authenticated")
    async with SessionLocal() as db:
        user = await db.get(User, uid)
        if user is None or user.disabled_at is not None:
            raise Denied(401, "not_authenticated")
        s = await db.get(DevSession, session_id)
        if s is None or not can_access(s, user):
            raise Denied(404, "not_found")
    web = session_web(s)
    base = app.state.devenv.port_target(s, port) if s.state == "running" else None
    if not base:
        raise Denied(503, "devenv_web_unavailable")
    entry = port_entry(web, port)
    return Target(session_id, port, entry["kind"], bool(entry.get("strip")), (web or {}).get("token") or "", base)


def _upstream_path(raw_path: str, t: Target) -> str:
    """Apps told their base path get the full path; the others get the part after it."""
    if t.strip:
        rest = raw_path[len(t.prefix) :] if raw_path.startswith(t.prefix) else raw_path
        return rest or "/"
    return raw_path


def _auth_header(t: Target) -> str | None:
    if not t.token:
        return None
    if t.kind == "jupyter":
        return f"token {t.token}"
    if t.kind == "desktop":
        return "Basic " + base64.b64encode(f"slotserve:{t.token}".encode()).decode()
    return None


def _cookies_without_ours(header: str | None) -> str | None:
    if not header:
        return None
    kept = [c.strip() for c in header.split(";") if c.strip() and c.split("=", 1)[0].strip() != SESSION_COOKIE]
    return "; ".join(kept) or None


def _forward_headers(items, drop: set[str], t: Target, scheme: str, client: str | None) -> list[tuple[str, str]]:
    """Headers for the app. It sees itself addressed directly (Host, and Origin/Referer when the browser sent
    them, name the upstream), so the origin checks of Jupyter and code-server pass; SlotServe already checked."""
    items = list(items)
    headers: list[tuple[str, str]] = [(k, v) for k, v in items if k.lower() not in drop]
    cookie = _cookies_without_ours("; ".join(v for k, v in items if k.lower() == "cookie"))
    if cookie:
        headers.append(("cookie", cookie))
    if auth := _auth_header(t):
        headers.append(("authorization", auth))
    for k, v in items:
        if k.lower() == "referer":
            headers.append(("referer", urlsplit(v)._replace(scheme="http", netloc=t.netloc).geturl()))
    headers += [
        ("x-forwarded-proto", scheme),
        ("x-forwarded-prefix", t.prefix),
        ("x-forwarded-for", client or ""),
    ]
    return headers


def _rewrite_location(value: str, t: Target) -> str:
    """Redirects to the app's own address or to paths outside the prefix are kept under the prefix."""
    parts = urlsplit(value)
    if parts.netloc and parts.netloc != t.netloc:
        return value  # somewhere else entirely
    path = parts.path
    if not parts.netloc and not path.startswith("/"):
        return value  # relative
    if not path.startswith(t.prefix + "/") and path != t.prefix:
        path = t.prefix + path
    return path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")


def _rewrite_cookie(value: str, t: Target) -> str | None:
    """Cookies stay inside the port's path; nobody gets to set SlotServe's own."""
    if value.split("=", 1)[0].strip() == SESSION_COOKIE:
        return None
    m = COOKIE_PATH.search(value)
    path = m.group(0).split("=", 1)[1].strip() if m else "/"
    if not (path == t.prefix or path.startswith(t.prefix + "/")):
        path = t.prefix + (path if path.startswith("/") else "/" + path)
    new = f"; Path={path}"
    return COOKIE_PATH.sub(new, value, count=1) if m else value + new


def _denied_http(request: Request, exc: Denied) -> Response:
    if exc.status == 401 and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=302)
    if exc.status == 503 and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(_page("This environment is not running."), status_code=503)
    return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)


def _page(text: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>SlotServe</title><body style='font-family:system-ui,sans-serif;padding:40px;color:#444'>"
        f"<p>{escape(text)}</p><p><a href='/devenv'>Dev environments</a></p>"
    )


def _raw_path(scope: dict, fallback: str) -> str:
    return scope.get("raw_path", b"").decode("latin-1").split("?", 1)[0] or fallback


@router.api_route("/devenv/{session_id:int}/port/{port:int}", methods=METHODS)
async def port_root(session_id: int, port: int, request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(web_base(session_id, port) + query, status_code=307)


@router.api_route("/devenv/{session_id:int}/port/{port:int}/{path:path}", methods=METHODS)
async def port_http(session_id: int, port: int, path: str, request: Request):
    try:
        t = await _target(request.app, request.session, session_id, port)
    except Denied as exc:
        return _denied_http(request, exc)
    url = t.base + _upstream_path(_raw_path(request.scope, request.url.path), t)
    if request.url.query:
        url += "?" + request.url.query
    client_host = request.client.host if request.client else None
    headers = _forward_headers(request.headers.items(), DROP_REQUEST, t, request.url.scheme, client_host)
    if request.headers.get("origin"):
        headers.append(("origin", f"http://{t.netloc}"))
    if "accept-encoding" not in request.headers:
        headers.append(("accept-encoding", "identity"))  # the body is passed on as it comes
    has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
    if "content-length" in request.headers:
        headers.append(("content-length", request.headers["content-length"]))
    client: httpx.AsyncClient = request.app.state.devenv.web_client()
    upstream_request = client.build_request(
        request.method, url, headers=headers, content=request.stream() if has_body else None
    )
    try:
        resp = await client.send(upstream_request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        log.info("dev environment %s port %s unreachable: %s", session_id, port, exc)
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                _page(f"Nothing answers on port {port} yet. It may still be starting; reload in a moment."),
                status_code=502,
            )
        return JSONResponse({"detail": {"code": "devenv_web_unreachable"}}, status_code=502)

    out: list[tuple[bytes, bytes]] = []
    for key, value in resp.headers.multi_items():
        k = key.lower()
        if k in HOP_BY_HOP:
            continue
        if k == "location":
            value = _rewrite_location(value, t)
        elif k == "set-cookie":
            value = _rewrite_cookie(value, t)
            if value is None:
                continue
        out.append((k.encode("latin-1"), value.encode("latin-1")))
    response = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    response.raw_headers = out
    return response


@router.websocket("/devenv/{session_id:int}/port/{port:int}/{path:path}")
async def port_socket(websocket: WebSocket, session_id: int, port: int, path: str):
    try:
        t = await _target(websocket.app, websocket.session, session_id, port)
    except Denied:
        await websocket.close(code=1008)
        return
    url = f"ws://{t.netloc}{_upstream_path(_raw_path(websocket.scope, websocket.url.path), t)}"
    if websocket.url.query:
        url += "?" + websocket.url.query
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    client_host = websocket.client.host if websocket.client else None
    headers = _forward_headers(websocket.headers.items(), WS_DROP, t, scheme, client_host)
    try:
        upstream = await websockets.connect(
            url,
            additional_headers=headers,
            origin=f"http://{t.netloc}",
            subprotocols=websocket.scope.get("subprotocols") or None,
            max_size=None,
            open_timeout=15,
            ping_interval=None,
            compression=None,
            user_agent_header=None,
        )
    except (OSError, websockets.InvalidHandshake, TimeoutError) as exc:
        log.info("dev environment %s websocket to %s failed: %s", session_id, url, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream():
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def upstream_to_client():
        async for msg in upstream:
            if isinstance(msg, str):
                await websocket.send_text(msg)
            else:
                await websocket.send_bytes(msg)

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):  # either side went away
                await task
    finally:
        await upstream.close()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            code = upstream.close_code if upstream.close_code and upstream.close_code < 5000 else 1000
            await websocket.close(code=code if code not in (1005, 1006) else 1000)
