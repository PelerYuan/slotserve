"""Reverse proxy to the ports of dev environments: ``/devenv/<session>/port/<container port>/...`` (HTTP and
WebSocket).

Any port of a running environment can be opened; the ports a template declares carry a kind (desktop, JupyterLab,
VS Code, http) that decides whether the app gets the full path and which token is added. Upstream addresses come
from ``DevEnvManager.port_target(session, port)`` only.

These routes are served by the web listener on its own port (``devenv_web_port``, see ``devenv_web_auth.py`` and
``devenv_web_app.py``), so pages in environments are a different origin than SlotServe; there people are recognized
by a per-environment cookie handed over from SlotServe. On SlotServe's own port the same paths only sign people in
and redirect there (or, with ``devenv_web_port`` 0, are served as before).

Only people signed in to SlotServe who may use the environment get through. SlotServe's own cookies are
never passed on, and where the app supports it the proxy adds the environment's random token
(JupyterLab: ``Authorization: token ...``, the desktop: HTTP basic auth), so other containers or people on the host
cannot use the app without going through SlotServe.

On the web listener all environments share one origin, so every request is first judged by
``security/web_origin.py`` (R1-03): requests a page of another environment could have made are refused, and only
trusted ones get the token and the app's own cookies. An admin opening someone else's environment confirms it
first (a page on SlotServe's origin, or ``POST /api/devenv/sessions/<id>/web-open``) and is audited.
"""

import asyncio
import base64
import contextlib
import logging
import re
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import websockets
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

from ..db import SessionLocal
from ..deps import SESSION_USER_KEY, current_user
from ..errors import ApiError
from ..orm import DevSession, User
from ..security import web_origin
from ..services.audit import record
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
    trusted: bool = False  # the token and the app's own cookies are passed on (security/web_origin.py)
    switch_scope: str = ""  # the browser starts using this trust group here (a navigation it made itself)
    as_admin: bool = False

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


def _headers(conn) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in conn.headers.items():
        k = k.lower()
        out[k] = f"{out[k]}; {v}" if k == "cookie" and k in out else v
    return out


def _compat(conn) -> bool:
    return bool(getattr(conn.app.state.settings, "devenv_web_compat", False))


def _uid(conn, session_id: int) -> int | None:
    if _listener(conn):
        return web_auth.cookie_user(conn.app.state.settings, _headers(conn).get("cookie"), session_id)
    return conn.session.get(SESSION_USER_KEY)


def _as_admin(s: DevSession, user: User) -> bool:
    """Whether opening this environment uses admin rights: someone else's, not a shared one."""
    group = web_origin.trust_group(s)
    return bool(user.is_admin) and group.startswith("user:") and group != f"user:{user.id}"


async def _session_and_user(db, session_id: int, uid: int | None) -> tuple[DevSession, User]:
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
    return s, user


def _verdict(conn, session_id: int, *, websocket: bool = False) -> web_origin.Verdict:
    settings = conn.app.state.settings
    return web_origin.classify(
        "GET" if websocket else conn.method,
        _headers(conn),
        session_id,
        websocket=websocket,
        compat=_compat(conn),
        own_pages=not _listener(conn),
        web_url=(settings.devenv_web_url or "") if _listener(conn) else "",
    )


def _log_refused(conn, session_id: int, reason: str) -> None:
    h = conn.headers
    referer = urlsplit(h.get("referer", "")).path if h.get("referer") else None
    log.warning(
        "dev environment %s: refused %s %s: %s (sec-fetch-site=%r mode=%r dest=%r referer=%r origin=%r)",
        session_id,
        getattr(conn, "method", "WEBSOCKET"),
        conn.url.path,
        reason,
        h.get("sec-fetch-site"),
        h.get("sec-fetch-mode"),
        h.get("sec-fetch-dest"),
        referer,
        h.get("origin"),
    )


async def _target(conn, session_id: int, port: int, verdict: web_origin.Verdict) -> Target:
    """Where a request for one of a session's ports goes, after checking who is asking."""
    app = conn.app
    async with SessionLocal() as db:
        s, user = await _session_and_user(db, session_id, _uid(conn, session_id))
    switch = ""
    if _listener(conn) and not _compat(conn):
        group = web_origin.trust_group(s)
        if group not in web_auth.scope_groups(app.state.settings, _headers(conn).get("cookie"), user.id):
            if verdict.reason != "user_navigation":
                raise Denied(403, "devenv_web_other_scope")
            switch = group  # typed, bookmarked or reloaded by the person: this environment becomes the one in use
    web = session_web(s)
    base = app.state.devenv.port_target(s, port) if s.state == "running" else None
    if not base:
        raise Denied(503, "devenv_web_unavailable")
    entry = port_entry(web, port)
    return Target(
        session_id,
        port,
        entry["kind"],
        bool(entry.get("strip")),
        (web or {}).get("token") or "",
        base,
        trusted=verdict.trusted,
        switch_scope=switch,
        as_admin=_as_admin(s, user),
    )


def _upstream_path(raw_path: str, t: Target) -> str:
    """Apps told their base path get the full path; the others get the part after it."""
    if t.strip:
        rest = raw_path[len(t.prefix) :] if raw_path.startswith(t.prefix) else raw_path
        return rest or "/"
    return raw_path


def _auth_header(t: Target) -> str | None:
    if not t.token or not t.trusted:
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
    if cookie and t.trusted:  # the app's own login cookies count only for requests SlotServe trusts
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


def _html(conn) -> bool:
    return "text/html" in conn.headers.get("accept", "")


def session_id_of(conn) -> int:
    return int(conn.path_params["session_id"])


def _denied_http(request: Request, exc: Denied, verdict: web_origin.Verdict | None = None) -> Response:
    html = _html(request)
    if exc.status == 401 and request.method == "GET" and html:
        if _listener(request):
            # sign in on SlotServe, which hands the person back with a token
            user_made = verdict is not None and verdict.reason == "user_navigation"
            return RedirectResponse(_reopen_url(request, session_id_of(request), user_made), status_code=302)
        return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=302)
    if exc.status == 403 and exc.code == "devenv_web_other_scope":
        _log_refused(request, session_id_of(request), "other_scope")
        return _refused(request, "other_scope", navigation=verdict is not None and verdict.navigation)
    if exc.status == 503 and html:
        return HTMLResponse(_page("This environment is not running.", _home(request)), status_code=503)
    return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)


def _reopen_url(conn, session_id: int, user_made: bool = False) -> str:
    """The same address on SlotServe's origin, which signs the person in (again) and hands them over. A page load
    the person made themselves (``Sec-Fetch-Site: none``) carries a short-lived note, so SlotServe does not ask
    them to confirm it."""
    settings = conn.app.state.settings
    path = quote(conn.url.path, safe="/")
    params = parse_qs(conn.url.query, keep_blank_values=True)
    params.pop(web_auth.RETURN_PARAM, None)
    if user_made:
        params[web_auth.RETURN_PARAM] = [web_auth.return_note(settings, session_id, conn.url.path)]
    query = urlencode(params, doseq=True)
    return web_auth.main_origin(settings, conn.url) + path + (f"?{query}" if query else "")


REFUSED_TEXT = {
    "other_scope": "This browser is using another person's (or a shared) dev environment on this address now. "
    "Open this environment again through SlotServe.",
}
REFUSED_DEFAULT = "A page of another dev environment tried to open this one, which is not allowed."


def _refused(conn, reason: str, *, navigation: bool) -> Response:
    """A refusal: a sandboxed page for page loads (it links to SlotServe, never to the environment), JSON else."""
    if navigation:
        text = REFUSED_TEXT.get(reason, REFUSED_DEFAULT)
        link = _reopen_url(conn, session_id_of(conn)) if _listener(conn) else "/devenv"
        body = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>SlotServe</title><body style='font-family:system-ui,sans-serif;padding:40px;color:#444'>"
            f"<p>{escape(text)}</p><p><a href='{escape(link)}'>Open in SlotServe</a></p>"
        )
        response: Response = HTMLResponse(body, status_code=403)
    else:
        code = "devenv_web_other_scope" if reason == "other_scope" else "devenv_web_cross_environment"
        response = JSONResponse({"detail": {"code": code, "params": {"reason": reason}}}, status_code=403)
    headers = web_origin.response_headers(
        [(k.decode("latin-1"), v.decode("latin-1")) for k, v in response.raw_headers], denied=True
    )
    response.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    return response


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


# ---- SlotServe's own origin: signing in to the web listener ----------------------------------------------------

CONFIRM_TEXT = {
    "en": {
        "title": "Open this dev environment?",
        "admin": "It belongs to {owner}. You are opening it with your admin rights; this is recorded in the audit "
        "log. Its pages can act as you inside this environment for an hour.",
        "elsewhere": "This link was opened from another page, not from SlotServe. Only continue if you meant to "
        "open it.",
        "open": "Open",
        "cancel": "Cancel",
    },
    "zh": {
        "title": "打开这个开发环境？",
        "admin": "它属于 {owner}。你正在以管理员身份打开"
        "它，此操作会记入审计日志。一小时内，"
        "它的页面可以在这个环境中以你的身份"
        "操作。",
        "elsewhere": "这个链接是从其他页面而不是 SlotServe 打开的。只有确实要打开时才继续。",
        "open": "打开",
        "cancel": "取消",
    },
}


def _confirm_page(request: Request, s: DevSession, owner: str, port: int, as_admin: bool) -> Response:
    lang = "zh" if request.headers.get("accept-language", "").lower().startswith("zh") else "en"
    text = CONFIRM_TEXT[lang]
    action = _raw_path(request.scope, request.url.path) + (f"?{request.url.query}" if request.url.query else "")
    note = text["admin"].format(owner=owner) if as_admin else text["elsewhere"]
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>SlotServe</title><body style='font-family:system-ui,sans-serif;padding:40px;color:#333'>"
        f"<h2>{escape(text['title'])}</h2><p>#{s.id} · port {port}</p><p>{escape(note)}</p>"
        f"<form method=post action='{escape(action)}'><input type=hidden name=confirm value=1>"
        f"<button type=submit autofocus>{escape(text['open'])}</button> "
        f"<a href='/devenv'>{escape(text['cancel'])}</a></form>"
    )
    return HTMLResponse(
        body,
        status_code=200,
        headers={
            "cache-control": "no-store",
            "cross-origin-opener-policy": "same-origin",  # a page that opened this window loses its handle on it
            "content-security-policy": "frame-ancestors 'none'",
            "x-frame-options": "DENY",
        },
    )


async def _audit_open_as_admin(db, user: User, s: DevSession, port: int) -> None:
    owner = await db.get(User, s.user_id) if s.user_id else None
    record(db, user, "devenv.open_as_admin", f"#{s.id}", owner=owner.username if owner else None, port=port)
    await db.commit()


def _enter_url(request: Request, user_id: int, session_id: int, target: str, s: DevSession, as_admin: bool) -> str:
    settings = request.app.state.settings
    token = web_auth.handovers.issue(
        settings, user_id, session_id, target, group=web_origin.trust_group(s), as_admin=as_admin
    )
    return web_auth.web_origin(settings, request.url) + web_auth.ENTER_PATH + "?token=" + quote(token, safe="")


async def _handover(request: Request, session_id: int, port: int) -> Response:
    """SlotServe's own app: checks the person and sends them to the web listener with a one-time token. It asks
    first when an admin opens someone else's environment, or when the link did not come from SlotServe itself
    (so a page elsewhere, for example in another environment, cannot open one in the person's name unnoticed)."""
    settings = request.app.state.settings
    confirmed = False
    if request.method == "POST":
        form = parse_qs((await request.body()).decode("latin-1"))
        confirmed = form.get("confirm") == ["1"]
    if request.method not in ("GET", "HEAD") and not confirmed:
        return JSONResponse({"detail": {"code": "devenv_web_moved"}}, status_code=404)
    uid = request.session.get(SESSION_USER_KEY)
    params = parse_qs(request.url.query, keep_blank_values=True)
    note = (params.pop(web_auth.RETURN_PARAM, None) or [""])[0]
    try:
        async with SessionLocal() as db:
            s, user = await _session_and_user(db, session_id, uid)
            as_admin = _as_admin(s, user)
            site = request.headers.get("sec-fetch-site", "").lower()
            from_slotserve = site in ("", "none", "same-origin") or web_auth.check_return_note(
                settings, note, session_id, request.url.path
            )
            if not confirmed and (as_admin or not from_slotserve):
                if request.method == "HEAD":
                    return Response(status_code=200)
                owner = await db.get(User, s.user_id) if s.user_id else None
                return _confirm_page(request, s, owner.username if owner else "?", port, as_admin)
            if as_admin:
                await _audit_open_as_admin(db, user, s, port)
    except Denied as exc:
        return _denied_http(request, exc)
    query = urlencode(params, doseq=True)
    target = _raw_path(request.scope, request.url.path) + (f"?{query}" if query else "")
    url = _enter_url(request, uid, session_id, target, s, as_admin)
    return RedirectResponse(url, status_code=303 if confirmed else 302, headers={"cache-control": "no-store"})


class WebOpen(BaseModel):
    port: int = Field(ge=1, le=65535)
    path: str = Field("", max_length=2000)  # below the port's base path, e.g. "lab/tree/x.ipynb"
    confirm_as_admin: bool = False


main_router = APIRouter(tags=["devenv"])


@main_router.post("/api/devenv/sessions/{session_id}/web-open")
async def web_open(session_id: int, body: WebOpen, request: Request, user: User = Depends(current_user)):
    """A link that opens one of an environment's web interfaces (for SlotServe's own pages): on the web
    listener a one-time sign-in link valid for a minute. An admin opening someone else's environment must send
    ``confirm_as_admin`` (409 ``devenv_open_as_admin_confirm`` otherwise); that is audited."""
    target = web_base(session_id, body.port) + body.path.lstrip("/")
    if "?" in body.path or "#" in body.path or web_auth.NEXT_RE.match(target) is None:
        raise ApiError(400, "devenv_web_path_invalid")
    try:
        async with SessionLocal() as db:
            s, user = await _session_and_user(db, session_id, user.id)
            as_admin = _as_admin(s, user)
            if as_admin and not body.confirm_as_admin:
                owner = await db.get(User, s.user_id) if s.user_id else None
                raise ApiError(409, "devenv_open_as_admin_confirm", owner=owner.username if owner else None)
            if as_admin:
                await _audit_open_as_admin(db, user, s, body.port)
    except Denied as exc:
        raise ApiError(exc.status, exc.code) from None
    if not request.app.state.settings.devenv_web_port:
        return {"url": target, "as_admin": as_admin, "expires_in": None}
    url = _enter_url(request, user.id, session_id, target, s, as_admin)
    return {"url": url, "as_admin": as_admin, "expires_in": web_auth.HANDOVER_SECONDS}


# ---- the proxy ----------------------------------------------------------------------------------------------------


@router.api_route("/devenv/{session_id:int}/port/{port:int}/{path:path}", methods=METHODS)
async def port_http(session_id: int, port: int, path: str, request: Request):
    if _separate(request):
        return await _handover(request, session_id, port)
    verdict = _verdict(request, session_id)
    if not verdict.allow:
        _log_refused(request, session_id, verdict.reason)
        return _refused(request, verdict.reason, navigation=verdict.navigation)
    try:
        t = await _target(request, session_id, port, verdict)
    except Denied as exc:
        return _denied_http(request, exc, verdict)
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
        if _html(request):
            return HTMLResponse(
                _page(
                    f"Nothing answers on port {port} yet. It may still be starting; reload in a moment.", _home(request)
                ),
                status_code=502,
            )
        return JSONResponse({"detail": {"code": "devenv_web_unreachable"}}, status_code=502)

    out: list[tuple[str, str]] = []
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
        out.append((k, value))
    out = web_origin.response_headers(out)
    if t.switch_scope:
        out.append(("set-cookie", _scope_cookie(request, t.switch_scope, t.as_admin)))
    response = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    response.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out]
    return response


def _scope_cookie(conn, group: str, as_admin: bool) -> str:
    settings = conn.app.state.settings
    probe = Response()
    probe.set_cookie(
        web_auth.SCOPE_COOKIE,
        web_auth.scope_value(settings, _uid(conn, session_id_of(conn)), group, as_admin=as_admin),
        max_age=web_auth.cookie_seconds(as_admin),
        path="/",
        httponly=True,
        samesite="lax",
        secure=bool(settings.cookie_secure),
    )
    return probe.headers["set-cookie"]


@router.websocket("/devenv/{session_id:int}/port/{port:int}/{path:path}")
async def port_socket(websocket: WebSocket, session_id: int, port: int, path: str):
    if _separate(websocket):
        await websocket.close(code=1008)
        return
    verdict = _verdict(websocket, session_id, websocket=True)
    if not verdict.allow:
        _log_refused(websocket, session_id, verdict.reason)
        await websocket.close(code=1008)
        return
    try:
        t = await _target(websocket, session_id, port, verdict)
    except Denied as exc:
        if exc.code == "devenv_web_other_scope":
            _log_refused(websocket, session_id, "other_scope")
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
    sid, uid, as_admin = int(data["s"]), int(data["u"]), bool(data.get("a"))
    response = RedirectResponse(data["n"], status_code=302, headers={"cache-control": "no-store"})
    cookie = {
        "max_age": web_auth.cookie_seconds(as_admin),
        "httponly": True,
        "samesite": "lax",
        "secure": bool(settings.cookie_secure),
    }
    response.set_cookie(
        web_auth.cookie_name(sid),
        web_auth.cookie_value(settings, uid, sid, as_admin=as_admin),
        path=web_auth.cookie_path(sid),
        **cookie,
    )
    # the browser now uses this environment's trust group on the shared origin (security/web_origin.py)
    response.set_cookie(
        web_auth.SCOPE_COOKIE, web_auth.scope_value(settings, uid, data["g"], as_admin=as_admin), path="/", **cookie
    )
    return response


@enter_router.get("/")
async def web_root(request: Request):
    return RedirectResponse(_home(request), status_code=302)
