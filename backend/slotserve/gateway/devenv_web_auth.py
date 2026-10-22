"""The separate origin of dev environment web interfaces.

Apps in dev environments are written by whoever uses them. Served on SlotServe's own origin, a page in someone's
environment could call SlotServe's API with the rights of whoever opens it. So the login-protected proxy runs on its
own port (``devenv_web_port``, a second listener in the same process, see ``devenv_web_app.py``): the browser treats
it as a different origin, and it never accepts SlotServe's session cookie.

Signing in there is a handover: SlotServe (where the person is signed in) checks access and redirects to the web
listener with a one-time token valid for a minute; the listener turns it into an HttpOnly cookie that is scoped to
that one environment's path (``slotserve_web_<session>``) and checked against the database on every request.

Cookie names differ per environment, so they stay apart when environments get origins of their own (M2: a port
pool; cookies do not tell ports apart). While all environments share one origin, a second cookie (``scope``,
path ``/``) names the one trust group the browser may use there (``security/web_origin.py``); entering an
environment of another group replaces it. An admin opening someone else's environment gets a cookie valid for an
hour (``ADMIN_COOKIE_SECONDS``) after confirming it (audited as ``devenv.open_as_admin``).
"""

import re
import secrets
import time
from urllib.parse import urlsplit

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "slotserve_session"
COOKIE_PREFIX = "slotserve_web_"
SCOPE_COOKIE = "slotserve_web_scope"
HANDOVER_SECONDS = 60
COOKIE_SECONDS = 12 * 3600
ADMIN_COOKIE_SECONDS = 3600
ENTER_PATH = "/_slotserve/enter"
NEXT_RE = re.compile(r"^/devenv/(\d+)/port/\d+(/[^\\]*)?$")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def is_ours(cookie_name: str) -> bool:
    """SlotServe's cookies: never passed to an app, never set by one."""
    name = cookie_name.strip()
    return name == SESSION_COOKIE or name.startswith(COOKIE_PREFIX)


def cookie_name(session_id: int) -> str:
    return f"{COOKIE_PREFIX}{session_id}"


def cookie_path(session_id: int) -> str:
    return f"/devenv/{session_id}/"


def _signer(settings, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=f"slotserve.devenv-web.{purpose}")


class Handovers:
    """One-time tokens from SlotServe to the web listener (both run in the same process)."""

    def __init__(self):
        self._used: dict[str, float] = {}

    def issue(self, settings, user_id: int, session_id: int, next_path: str, *, group: str, as_admin: bool) -> str:
        return _signer(settings, "handover").dumps(
            {
                "u": user_id,
                "s": session_id,
                "n": next_path,
                "g": group,
                "a": bool(as_admin),
                "j": secrets.token_urlsafe(12),
            }
        )

    def redeem(self, settings, token: str) -> dict | None:
        try:
            data = _signer(settings, "handover").loads(token, max_age=HANDOVER_SECONDS)
        except (BadSignature, ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("j"), str) or data["j"] in self._used:
            return None
        m = NEXT_RE.match(str(data.get("n", "")))
        if m is None or int(m.group(1)) != data.get("s") or not isinstance(data.get("g"), str):
            return None
        now = time.monotonic()
        self._used = {k: v for k, v in self._used.items() if v > now}
        self._used[data["j"]] = now + HANDOVER_SECONDS + 5
        return data


handovers = Handovers()

RETURN_PARAM = "_slotserve_return"


def return_note(settings, session_id: int, path: str) -> str:
    """Sent along when the web listener sends a person back to SlotServe after a page load they made themselves
    (Sec-Fetch-Site: none): SlotServe then hands them over without asking (pages cannot obtain one)."""
    return _signer(settings, "return").dumps({"s": session_id, "p": path})


def check_return_note(settings, note: str, session_id: int, path: str) -> bool:
    if not note:
        return False
    try:
        data = _signer(settings, "return").loads(note, max_age=HANDOVER_SECONDS)
    except (BadSignature, ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("s") == session_id and data.get("p") == path


def cookie_seconds(as_admin: bool) -> int:
    return ADMIN_COOKIE_SECONDS if as_admin else COOKIE_SECONDS


def cookie_value(settings, user_id: int, session_id: int, *, as_admin: bool = False) -> str:
    return _signer(settings, "cookie").dumps({"u": user_id, "s": session_id, "t": cookie_seconds(as_admin)})


def cookie_values(header: str | None, name: str) -> list[str]:
    """Every value the browser sent for a cookie name (a page can add same-named cookies for other paths; only
    signed values count, so they cannot get anyone in, and they cannot push the real one out either)."""
    out = []
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip() == name:
            out.append(value.strip().strip('"'))
    return out


def _load(settings, purpose: str, raw: str, max_age: int) -> dict | None:
    try:
        data, signed_at = _signer(settings, purpose).loads(raw, max_age=max_age, return_timestamp=True)
    except (BadSignature, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    lifetime = data.get("t", COOKIE_SECONDS)
    if not isinstance(lifetime, int) or time.time() - signed_at.timestamp() > lifetime:
        return None
    return data


def cookie_user(settings, cookie_header: str | None, session_id: int) -> int | None:
    for raw in cookie_values(cookie_header, cookie_name(session_id)):
        data = _load(settings, "cookie", raw, COOKIE_SECONDS)
        if data is not None and data.get("s") == session_id and isinstance(data.get("u"), int):
            return data["u"]
    return None


def scope_value(settings, user_id: int, group: str, *, as_admin: bool = False) -> str:
    return _signer(settings, "scope").dumps({"u": user_id, "g": group, "t": cookie_seconds(as_admin)})


def scope_groups(settings, cookie_header: str | None, user_id: int) -> set[str]:
    """The trust group(s) this browser is currently using on the shared origin (normally one)."""
    groups = set()
    for raw in cookie_values(cookie_header, SCOPE_COOKIE):
        data = _load(settings, "scope", raw, COOKIE_SECONDS)
        if data is not None and data.get("u") == user_id and isinstance(data.get("g"), str):
            groups.add(data["g"])
    return groups


def _host(hostname: str | None) -> str:
    host = hostname or "localhost"
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def web_origin(settings, url) -> str:
    """Where browsers reach the web listener: ``devenv_web_url``, or the host they use with its port."""
    if settings.devenv_web_url:
        return settings.devenv_web_url.rstrip("/")
    return f"{url.scheme}://{_host(url.hostname)}:{settings.devenv_web_port}"


def main_origin(settings, url) -> str:
    """Where browsers reach SlotServe itself (to sign in): ``public_url``, or the same host on SlotServe's port."""
    if settings.public_url:
        return settings.public_url.rstrip("/")
    return f"{url.scheme}://{_host(url.hostname)}:{settings.port}"


def _origin_of(value: str) -> tuple[str, str, int] | None:
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname:
        return None
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    return parts.scheme, parts.hostname.lower(), port


def from_web_origin(settings, origin: str, host_header: str) -> bool:
    """Whether a request to SlotServe was made by a page on the web listener's origin."""
    if not settings.devenv_web_port:
        return False
    sender = _origin_of(origin)
    if sender is None:
        return False
    if settings.devenv_web_url and (target := _origin_of(settings.devenv_web_url)) is not None and sender == target:
        return True
    host = (urlsplit(f"//{host_header}").hostname or "").lower()
    return sender[1] == host and sender[2] == settings.devenv_web_port


class WebOriginGuard:
    """ASGI middleware for SlotServe's own app: refuses changing requests sent by pages on the web listener
    (cookies are shared between ports of one host, and browsers count other ports of it as the same site)."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            method = scope.get("method", "GET") if scope["type"] == "http" else "WEBSOCKET"
            origin = headers.get("origin") or headers.get("referer") or ""
            unsafe = method in UNSAFE or method == "WEBSOCKET"
            if unsafe and origin and from_web_origin(self.settings, origin, headers.get("host", "")):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = b'{"detail":{"code":"cross_origin_refused"}}'
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
