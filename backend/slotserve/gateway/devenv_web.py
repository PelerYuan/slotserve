"""Reverse proxy to the ports of dev environments: ``/devenv/<session>/port/<container port>/...`` (HTTP and
WebSocket).

Any port of a running environment can be opened; the ports a template declares carry a kind (desktop, JupyterLab,
VS Code, http) that decides whether the app gets the full path and which token is added. Upstream addresses come
from ``DevEnvManager.port_upstream(session, port)`` only (an environment on another machine: a loopback port of the
control plane relayed to the container through that machine's agent).

These routes are served by the web listener on its own port (``devenv_web_port``, see ``devenv_web_auth.py`` and
``devenv_web_app.py``), so pages in environments are a different origin than SlotServe; there people are recognized
by a per-environment cookie handed over from SlotServe. On SlotServe's own port the same paths only sign people in
and redirect there (or, with ``devenv_web_port`` 0, are served as before).

Only people signed in to SlotServe who may use the environment get through. SlotServe's own cookies are
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
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

from ..db import SessionLocal
from ..deps import SESSION_USER_KEY
from ..orm import DevSession, User
from ..services.devenv import can_access, port_entry, session_web, web_base
from . import devenv_web_auth as web_auth

log = logging.getLogger(__name__)
router = APIRouter(tags=["devenv"], include_in_schema=False)

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
SESSION_COOKIE = web_auth.SESSION_COOKIE
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


def _listener(conn) -> bool:
    """Whether this is the web listener (its own origin) rather than SlotServe's own app."""
    return bool(getattr(conn.app.state, "web_listener", False))


def _separate(conn) -> bool:
    """SlotServe's own app, while web interfaces are served on the web listener: only sign in and redirect."""
    return not _listener(conn) and bool(conn.app.state.settings.devenv_web_port)


def _uid(conn, session_id: int) -> int | None:
    if _listener(conn):
        return web_auth.cookie_user(conn.app.state.settings, conn.cookies, session_id)
    return conn.session.get(SESSION_USER_KEY)


async def _session_for(db, session_id: int, uid: int | None) -> DevSession:
    """The session a path names, if the person may use it (a kept environment's container keeps the paths of the
    booking that created it, so they reach the running session)."""
    if uid is None:
        raise Denied(401, "not_authenticated")
    user = await db.get(User, uid)
    if user is None or user.disabled_at is not None:
        raise Denied(401, "not_authenticated")
    s = await db.get(DevSession, session_id)
    if s is not None and s.state != "running" and s.project_id:
        current = (
            await db.execute(
                select(DevSession).where(DevSession.project_id == s.project_id, DevSession.state == "running")
            )
        ).scalar()
        if current is not None and (session_web(current) or {}).get("base_session") == s.id:
            s = current
    if s is None or not can_access(s, user):
        raise Denied(404, "not_found")
    return s


def _other_environment(conn, session_id: int) -> bool:
    """A page of one environment asking for another one's (they share the web listener's origin)."""
    if conn.headers.get("sec-fetch-site") != "same-origin":
        return False
    m = re.match(r"^/devenv/(\d+)/", urlsplit(conn.headers.get("referer", "")).path)
    return m is not None and int(m.group(1)) != session_id


async def _target(app, uid: int | None, session_id: int, port: int) -> Target:
    """Where a request for one of a session's ports goes, after checking who is asking."""
    async with SessionLocal() as db:
        s = await _session_for(db, session_id, uid)
    web = session_web(s)
    base = await app.state.devenv.port_upstream(s, port) if s.state == "running" else None
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
    kept = [c.strip() for c in header.split(";") if c.strip() and not web_auth.is_ours(c.split("=", 1)[0])]
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
    if web_auth.is_ours(value.split("=", 1)[0]):
        return None
    m = COOKIE_PATH.search(value)
    path = m.group(0).split("=", 1)[1].strip() if m else "/"
    if not (path == t.prefix or path.startswith(t.prefix + "/")):
        path = t.prefix + (path if path.startswith("/") else "/" + path)
    new = f"; Path={path}"
    return COOKIE_PATH.sub(new, value, count=1) if m else value + new


def _denied_http(request: Request, exc: Denied) -> Response:
    html = "text/html" in request.headers.get("accept", "")
    if exc.status == 401 and request.method == "GET" and html:
        if _listener(request):
            # sign in on SlotServe, which hands the person back with a token
            query = f"?{request.url.query}" if request.url.query else ""
            back = web_auth.main_origin(request.app.state.settings, request.url) + quote(request.url.path, safe="/")
            return RedirectResponse(back + query, status_code=302)
        return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=302)
    if exc.status == 503 and html:
        return HTMLResponse(_page("This environment is not running.", _home(request)), status_code=503)
    return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)


def _home(conn) -> str:
    if _listener(conn):
        return web_auth.main_origin(conn.app.state.settings, conn.url) + "/devenv"
    return "/devenv"


def _page(text: str, home: str = "/devenv") -> str:
    return (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>SlotServe</title><body style='font-family:system-ui,sans-serif;padding:40px;color:#444'>"
        f"<p>{escape(text)}</p><p><a href='{escape(home)}'>Dev environments</a></p>"
    )


def _raw_path(scope: dict, fallback: str) -> str:
    return scope.get("raw_path", b"").decode("latin-1").split("?", 1)[0] or fallback


@router.api_route("/devenv/{session_id:int}/port/{port:int}", methods=METHODS)
async def port_root(session_id: int, port: int, request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(web_base(session_id, port) + query, status_code=307)


async def _handover(request: Request, session_id: int) -> Response:
    """SlotServe's own app: checks the person and sends them to the web listener with a one-time token."""
    settings = request.app.state.settings
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": {"code": "devenv_web_moved"}}, status_code=404)
    try:
        async with SessionLocal() as db:
            await _session_for(db, session_id, request.session.get(SESSION_USER_KEY))
    except Denied as exc:
        return _denied_http(request, exc)
    target = _raw_path(request.scope, request.url.path) + (f"?{request.url.query}" if request.url.query else "")
    token = web_auth.handovers.issue(settings, request.session[SESSION_USER_KEY], session_id, target)
    url = web_auth.web_origin(settings, request.url) + web_auth.ENTER_PATH + "?token=" + quote(token, safe="")
    return RedirectResponse(url, status_code=302, headers={"cache-control": "no-store"})


@router.api_route("/devenv/{session_id:int}/port/{port:int}/{path:path}", methods=METHODS)
async def port_http(session_id: int, port: int, path: str, request: Request):
    if _separate(request):
        return await _handover(request, session_id)
    if _listener(request) and _other_environment(request, session_id):
        return JSONResponse({"detail": {"code": "devenv_web_cross_environment"}}, status_code=403)
    try:
        t = await _target(request.app, _uid(request, session_id), session_id, port)
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
                _page(
                    f"Nothing answers on port {port} yet. It may still be starting; reload in a moment.", _home(request)
                ),
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
    if _separate(websocket) or (_listener(websocket) and _other_environment(websocket, session_id)):
        await websocket.close(code=1008)
        return
    try:
        t = await _target(websocket.app, _uid(websocket, session_id), session_id, port)
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


# ---- the web listener's own routes (devenv_web_app.py) ---------------------------------------------------------

enter_router = APIRouter(include_in_schema=False)


@enter_router.get(web_auth.ENTER_PATH)
async def enter(request: Request, token: str = ""):
    """Turns a one-time token from SlotServe into the environment's cookie, then opens the page."""
    settings = request.app.state.settings
    data = web_auth.handovers.redeem(settings, token)
    if data is None:
        return HTMLResponse(
            _page("This link has expired. Open the environment again.", _home(request)), status_code=400
        )
    sid = int(data["s"])
    response = RedirectResponse(data["n"], status_code=302, headers={"cache-control": "no-store"})
    response.set_cookie(
        web_auth.cookie_name(sid),
        web_auth.cookie_value(settings, int(data["u"]), sid),
        max_age=web_auth.COOKIE_SECONDS,
        path=web_auth.cookie_path(sid),
        httponly=True,
        samesite="lax",
        secure=bool(settings.cookie_secure),
    )
    return response


@enter_router.get("/")
async def web_root(request: Request):
    return RedirectResponse(_home(request), status_code=302)
