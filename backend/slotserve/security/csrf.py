"""CsrfGuard: changing requests to SlotServe must come from SlotServe's own pages.

Session cookies are ``SameSite=lax``, and browsers count every port of a host as the same site, so a page on a
dev environment's direct port (``http://host:2301``) or the web port (``:8001``) - or any page that sends
``Origin: null`` - could otherwise make a signed-in person's browser POST to SlotServe. For POST, PUT, PATCH and
DELETE (and WebSocket handshakes) this middleware requires:

* ``Sec-Fetch-Site`` is not ``cross-site`` or ``same-site`` (browsers set it; other clients do not send it);
* an ``Origin``, when present, is SlotServe's own: the origin of ``SLOTSERVE_PUBLIC_URL`` or the host the request
  was sent to (``Host`` / ``X-Forwarded-Host``); ``null`` is always refused;
* without ``Origin``, a request carrying the session cookie must carry ``X-SlotServe: 1`` (the frontend's API
  client adds it to every request; a page on another origin cannot add headers without a CORS preflight,
  which SlotServe never grants). Not for dev environment apps proxied on SlotServe's own port
  (``/devenv/<id>/port/...`` with ``SLOTSERVE_DEVENV_WEB_PORT=0``), which cannot add the header; browsers
  mark their cross-site requests with ``Sec-Fetch-Site`` and ``Origin`` anyway.

Clients that do not use the session cookie are not affected: the OpenAI-compatible ``/v1`` API (API keys), chat
platform callbacks (signed), ``/metrics`` (bearer token, GET) and scripts sending ``Authorization: Bearer``.
"""

import json
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

SESSION_COOKIE = "slotserve_session"
HEADER = "x-slotserve"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REFUSED_SITES = frozenset({"cross-site", "same-site"})
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _origin_parts(value: str) -> tuple[str, int] | None:
    """(host, port) of an origin such as ``https://gpu-box:8443``; None when it cannot be parsed."""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
            return None
        return parts.hostname.lower().rstrip("."), parts.port or DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None


def _host_matches(host_header: str, origin: tuple[str, int]) -> bool:
    """Whether ``Origin`` names the host a request was sent to (any scheme: TLS may end at a proxy)."""
    host_header = host_header.strip().lower()
    if not host_header:
        return False
    try:
        parts = urlsplit(f"//{host_header}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host or host.rstrip(".") != origin[0]:
        return False
    if port is None:  # Host without a port: the browser used the scheme's default port
        return origin[1] in (80, 443)
    return port == origin[1]


def _has_cookie(cookie_header: str, name: str) -> bool:
    return any(part.strip().split("=", 1)[0].strip() == name for part in cookie_header.split(";"))


def check(
    *,
    method: str,
    headers: dict[str, str],
    public_url: str = "",
    session_cookie: str = SESSION_COOKIE,
    require_header: bool = True,
) -> str | None:
    """Why a changing request must be refused, or None when it may pass. ``headers`` have lower-case names."""
    if headers.get("sec-fetch-site", "").strip().lower() in REFUSED_SITES:
        return "sec_fetch_site"
    origin = headers.get("origin")
    if origin is not None:
        if origin.strip().lower() in ("null", ""):
            return "origin_null"
        parts = _origin_parts(origin)
        if parts is None:
            return "origin_invalid"
        if public_url and _origin_parts(public_url) == parts:
            return None
        hosts = [headers.get("host", "")]
        if forwarded := headers.get("x-forwarded-host"):
            hosts.append(forwarded.split(",")[0])
        if any(_host_matches(h, parts) for h in hosts):
            return None
        return "origin_foreign"
    if method == "WEBSOCKET" or not require_header:
        return None  # browsers always send Origin with WebSocket handshakes
    if _has_cookie(headers.get("cookie", ""), session_cookie) and not headers.get(HEADER, "").strip():
        if headers.get("authorization", "").lower().startswith("bearer "):
            return None  # a script with an API credential; no browser page can add this header
        return "header_missing"
    return None


class CsrfGuard:
    """ASGI middleware (outermost, before the session middleware) applying ``check`` to SlotServe's own app."""

    def __init__(
        self,
        app,
        settings,
        exempt_paths: Iterable[str] = (),
        exempt_prefixes: Iterable[str] = (),
        no_header_prefixes: Iterable[str] = (),
        session_cookie: str = SESSION_COOKIE,
    ):
        self.app = app
        self.settings = settings
        self.exempt_paths = frozenset(exempt_paths)
        self.exempt_prefixes = tuple(exempt_prefixes)
        self.no_header_prefixes = tuple(no_header_prefixes)
        self.session_cookie = session_cookie

    def exempt(self, path: str) -> bool:
        return path in self.exempt_paths or path.startswith(self.exempt_prefixes)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper() if scope["type"] == "http" else "WEBSOCKET"
        path = scope.get("path", "")
        if (method in UNSAFE_METHODS or method == "WEBSOCKET") and not self.exempt(path):
            headers: dict[str, str] = {}
            for k, v in scope.get("headers", []):
                name = k.decode("latin-1").lower()
                value = v.decode("latin-1")
                headers[name] = f"{headers[name]}; {value}" if name == "cookie" and name in headers else value
            reason = check(
                method=method,
                headers=headers,
                public_url=getattr(self.settings, "public_url", "") or "",
                session_cookie=self.session_cookie,
                require_header=not path.startswith(self.no_header_prefixes),
            )
            if reason is not None:
                log.warning(
                    "refused %s %s: %s (origin=%r, sec-fetch-site=%r)",
                    method,
                    path,
                    reason,
                    headers.get("origin"),
                    headers.get("sec-fetch-site"),
                )
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                code = "cross_origin_refused" if self._from_web_port(headers.get("origin")) else "csrf_refused"
                body = json.dumps({"detail": {"code": code, "params": {"reason": reason}}}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)

    def _from_web_port(self, origin: str | None) -> bool:
        """A page of a dev environment's web interface (``devenv_web_port``): reported with its own message."""
        port = getattr(self.settings, "devenv_web_port", 0)
        parts = _origin_parts(origin) if origin else None
        if not port or parts is None:
            return False
        web_url = getattr(self.settings, "devenv_web_url", "") or ""
        return parts[1] == port or (bool(web_url) and _origin_parts(web_url) == parts)
