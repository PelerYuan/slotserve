"""R1-02: every changing route of SlotServe's own app refuses requests from other origins.

The route list comes from the app itself, so routes added later are covered automatically.
"""

import re

import pytest
from security_helpers import client, register_admin, running_app

from slotserve.api.channels import WECOM_CALLBACK
from slotserve.gateway.devenv_web_auth import WebOriginGuard
from slotserve.security.csrf import CsrfGuard, check
from slotserve.security.sessions import ServerSessionMiddleware

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
EXEMPT = {"/v1/{path:path}", WECOM_CALLBACK}
# dev environment apps proxied on SlotServe's port (DEVENV_WEB_PORT=0) cannot add X-SlotServe
NO_HEADER_PREFIX = "/devenv/"
REFUSED_CODES = ("csrf_refused", "cross_origin_refused")
COOKIE = {"cookie": "slotserve_session=anything"}  # CsrfGuard only looks at whether the cookie is there

ATTACKS = {
    "origin null": {**COOKIE, "origin": "null", "content-type": "text/plain"},
    "direct port": {**COOKIE, "origin": "http://test:2301", "content-type": "text/plain"},
    "web port": {**COOKIE, "origin": "http://test:8001"},
    "other site": {**COOKIE, "origin": "https://evil.example"},
    "no origin, no header": {**COOKIE},
    "sec-fetch-site same-site": {**COOKIE, "sec-fetch-site": "same-site", "x-slotserve": "1"},
    "sec-fetch-site cross-site": {**COOKIE, "sec-fetch-site": "cross-site", "x-slotserve": "1"},
    "origin null with header": {**COOKIE, "origin": "null", "x-slotserve": "1"},
}
ALLOWED = {
    "own origin": {**COOKIE, "origin": "http://test", "sec-fetch-site": "same-origin"},
    "no origin, header": {**COOKIE, "x-slotserve": "1"},
}


def write_routes(app) -> list[tuple[str, str]]:
    try:  # FastAPI >= 0.13x includes routers lazily
        from fastapi.routing import iter_route_contexts

        routes = [(rc.path, getattr(rc.original_route, "methods", None)) for rc in iter_route_contexts(app.routes)]
    except ImportError:
        routes = [(r.path, getattr(r, "methods", None)) for r in app.routes]
    return sorted({(m, path) for path, methods in routes if methods for m in methods if m in UNSAFE})


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
async def app(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as application:
        yield application


async def test_route_list_is_complete(app):
    routes = write_routes(app)
    paths = {p for _, p in routes}
    # a sample of known routes, so a broken enumeration cannot pass silently
    for known in ("/api/admin/backups", "/api/auth/register", "/api/models/{model_id}/default", WECOM_CALLBACK):
        assert known in paths
    assert len(routes) > 60
    assert paths >= EXEMPT


async def test_every_write_route_refuses_foreign_requests(app):
    refused = []
    async with client(app, csrf_header=False) as c:
        for method, path in write_routes(app):
            if path in EXEMPT:
                continue
            for name, headers in ATTACKS.items():
                if name == "no origin, no header" and path.startswith(NO_HEADER_PREFIX):
                    continue
                r = await c.request(method, concrete(path), headers=headers, content=b"x")
                if r.status_code != 403 or r.json()["detail"]["code"] not in REFUSED_CODES:
                    refused.append(f"{method} {path} [{name}] -> {r.status_code} {r.text[:80]}")
    assert not refused, "\n".join(refused)


async def test_every_write_route_accepts_own_pages(app):
    wrong = []
    async with client(app, csrf_header=False) as c:
        for method, path in write_routes(app):
            for name, headers in ALLOWED.items():
                r = await c.request(method, concrete(path), headers=headers)
                body = r.text if r.headers.get("content-type", "").startswith("application/json") else ""
                if "csrf_refused" in body:
                    wrong.append(f"{method} {path} [{name}]")
    assert not wrong, "\n".join(wrong)


async def test_exempt_routes_keep_working_for_their_clients(app):
    async with client(app, csrf_header=False) as c:
        # OpenAI-compatible API: API keys only (no cookie, no Origin needed; an Origin does not matter)
        r = await c.post("/v1/chat/completions", headers={"origin": "null"}, json={})
        assert r.status_code == 401 and "csrf" not in r.text
        r = await c.post("/v1/chat/completions", headers={"authorization": "Bearer sk-ss-nope"}, json={})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
        # chat platform callback (signed by the platform)
        r = await c.post(WECOM_CALLBACK, content=b"<xml/>")
        assert "csrf" not in r.text


async def test_clients_without_the_session_cookie_are_not_affected(app):
    """Scripts, chat platform webhooks, node agents: no cookie, no Origin, no header."""
    async with client(app, csrf_header=False) as c:
        r = await c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
        r = await c.post("/api/admin/backups", headers={"authorization": "Bearer something"})
        assert r.status_code == 401  # reached the app (and its own authentication)


async def test_real_session_flow(app):
    """A signed-in admin: the frontend's requests pass, a cross-origin page's identical request does not."""
    async with client(app) as admin:
        await register_admin(app, admin)
        r = await admin.post("/api/admin/backups")
        assert r.status_code == 201, r.text
        r = await admin.post("/api/admin/backups", headers={"origin": "null", "x-slotserve": ""})
        assert r.status_code == 403
        del admin.headers["x-slotserve"]
        assert (await admin.post("/api/admin/backups")).status_code == 403
        # a same-origin page (the SPA) always sends its Origin
        r = await admin.post("/api/admin/backups", headers={"origin": "http://test"})
        assert r.status_code == 201
        assert (await admin.get("/api/admin/backups")).status_code == 200  # reads are not affected


async def test_public_url_and_forwarded_host(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_PUBLIC_URL="https://gpu.example.org/") as app:
        async with client(app, csrf_header=False) as c:
            h = {**COOKIE, "origin": "https://gpu.example.org"}
            assert "csrf" not in (await c.post("/api/auth/logout", headers=h)).text
            h = {**COOKIE, "origin": "https://proxy.example.org", "x-forwarded-host": "proxy.example.org"}
            assert "csrf" not in (await c.post("/api/auth/logout", headers=h)).text
            h = {**COOKIE, "origin": "http://gpu.example.org:2301"}
            assert (await c.post("/api/auth/logout", headers=h)).status_code == 403


async def test_pages_on_the_web_port_get_their_own_message(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_DEVENV_WEB_PORT="8001") as app:
        async with client(app) as c:
            r = await c.post("/api/admin/backups", headers={"origin": "http://test:8001"})
            assert r.status_code == 403 and r.json()["detail"]["code"] == "cross_origin_refused"
            r = await c.post("/api/admin/backups", headers={"origin": "http://test:2301"})
            assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_refused"


def test_middleware_order(app):
    """CsrfGuard runs first (outermost), then WebOriginGuard, then the session middleware."""
    assert [m.cls for m in app.user_middleware] == [CsrfGuard, WebOriginGuard, ServerSessionMiddleware]


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"origin": "http://gpu-box:8000", "host": "gpu-box:8000"}, None),
        ({"origin": "https://gpu-box", "host": "gpu-box"}, None),
        ({"origin": "http://gpu-box", "host": "gpu-box:80"}, None),
        ({"origin": "http://GPU-box:8000", "host": "gpu-box:8000"}, None),
        ({"origin": "http://gpu-box:8001", "host": "gpu-box:8000"}, "origin_foreign"),
        ({"origin": "http://gpu-box:8000", "host": "gpu-box"}, "origin_foreign"),
        ({"origin": "http://gpu-box.evil:8000", "host": "gpu-box:8000"}, "origin_foreign"),
        ({"origin": "null", "host": "gpu-box"}, "origin_null"),
        ({"origin": "", "host": "gpu-box"}, "origin_null"),
        ({"origin": "file://", "host": "gpu-box"}, "origin_invalid"),
        ({"host": "gpu-box", "cookie": "a=1; slotserve_session=x"}, "header_missing"),
        ({"host": "gpu-box", "cookie": "slotserve_session_other=x"}, None),
        ({"host": "gpu-box", "cookie": "slotserve_session=x", "authorization": "Bearer k"}, None),
        ({"host": "gpu-box", "sec-fetch-site": "Same-Site"}, "sec_fetch_site"),
        ({"host": "gpu-box", "sec-fetch-site": "same-origin", "cookie": "slotserve_session=x"}, "header_missing"),
        ({"host": "gpu-box", "sec-fetch-site": "none"}, None),
    ],
)
def test_check(headers, reason):
    assert check(method="POST", headers=headers) == reason


def test_check_without_header_requirement():
    cookie = {"host": "gpu-box", "cookie": "slotserve_session=x"}
    assert check(method="POST", headers=cookie, require_header=False) is None
    assert check(method="POST", headers={**cookie, "sec-fetch-site": "same-site"}, require_header=False)
    assert check(method="POST", headers={**cookie, "origin": "null"}, require_header=False)


async def test_websocket_handshakes_from_other_origins_are_closed():
    sent = []
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    async def send(message):
        sent.append(message)

    guard = CsrfGuard(inner, settings=None)
    scope = {
        "type": "websocket",
        "path": "/devenv/1/port/8888/api/kernels/x/channels",
        "headers": [(b"host", b"gpu-box:8000"), (b"origin", b"http://gpu-box:2301")],
    }
    await guard(scope, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}] and not reached
    scope["headers"] = [(b"host", b"gpu-box:8000"), (b"origin", b"http://gpu-box:8000")]
    await guard(scope, None, send)
    assert reached
