"""R1-03: on the shared web origin a page of one dev environment cannot act in another (security/web_origin.py,
gateway/devenv_web.py). The browser-level proof is e2e/test_devenv_web_origin.py."""

import json
import logging

import httpx
import pytest
from sqlalchemy import select
from test_v10_devenv_web import UPSTREAM, _free_port, _serve

from slotserve.db import SessionLocal
from slotserve.gateway import devenv_web_auth as web_auth
from slotserve.orm import AuditLog, DevSession, User
from slotserve.security.web_origin import classify, environment_of, response_headers, trust_group

WEB = "http://test:8001"


# ---- the rules -------------------------------------------------------------------------------------------------


def _h(**kw) -> dict[str, str]:
    return {k.replace("_", "-"): v for k, v in kw.items()}


def test_rules():
    b = 2
    own = f"{WEB}/devenv/2/port/8888/lab"
    other = f"{WEB}/devenv/1/port/7860/"
    fetch = {"sec_fetch_site": "same-origin", "sec_fetch_mode": "cors", "sec_fetch_dest": "empty"}
    # a page on the origin: only with a Referer of the same environment
    assert classify("POST", _h(**fetch, referer=own), b) == classify("GET", _h(**fetch, referer=own), b)
    assert classify("POST", _h(**fetch, referer=own), b).trusted
    v = classify("GET", _h(**fetch), b)
    assert not v.allow and v.reason == "referer_missing"  # no-referrer is refused, not waved through
    assert classify("POST", _h(**fetch, referer=other), b).reason == "other_environment"
    assert classify("GET", _h(**fetch, referer=f"{WEB}/_slotserve/enter"), b).reason == "referer_foreign"
    nav = {"sec_fetch_site": "same-origin", "sec_fetch_mode": "navigate", "sec_fetch_dest": "document"}
    v = classify("GET", _h(**nav, referer=other), b)
    assert not v.allow and v.navigation  # window.open / links from another environment
    frame = {**nav, "sec_fetch_dest": "iframe"}
    assert not classify("GET", _h(**frame, referer=other), b).allow
    assert classify("GET", _h(**frame, referer=own), b).trusted  # the app's own frames
    # the person's own page loads (address bar, bookmark, reload)
    typed = {"sec_fetch_site": "none", "sec_fetch_mode": "navigate", "sec_fetch_dest": "document"}
    assert classify("GET", _h(**typed), b).trusted and classify("GET", _h(**typed), b).reason == "user_navigation"
    assert not classify("POST", _h(**typed), b).allow
    # other sites and other ports of the host (SlotServe itself, direct ports): top-level GET only
    for site in ("same-site", "cross-site"):
        assert classify("GET", _h(**{**typed, "sec_fetch_site": site}), b).trusted
        assert not classify("POST", _h(**{**typed, "sec_fetch_site": site}), b).allow  # form post
        no_cors = {"sec_fetch_site": site, "sec_fetch_mode": "no-cors", "sec_fetch_dest": "empty"}
        assert not classify("POST", _h(**no_cors), b).allow
        assert not classify("GET", _h(**{**frame, "sec_fetch_site": site}), b).allow
    # without Fetch Metadata: through, but untrusted (no token, no app cookies)
    v = classify("POST", {}, b)
    assert v.allow and not v.trusted
    # WebSockets: no Referer, no Sec-Fetch-*; the Origin must be the web origin
    host = {"host": "test:8001"}
    assert classify("GET", {**host, "origin": WEB}, b, websocket=True).trusted
    assert not classify("GET", {**host, "origin": "http://test:2301"}, b, websocket=True).allow
    assert not classify("GET", {**host, "origin": "null"}, b, websocket=True).allow
    assert classify(
        "GET", {**host, "origin": "https://envs.example"}, b, websocket=True, web_url="https://envs.example"
    )
    v = classify("GET", host, b, websocket=True)
    assert v.allow and not v.trusted
    # compatibility mode relaxes the cross-environment rule only
    assert classify("GET", _h(**fetch), b, compat=True).trusted
    assert classify("GET", _h(**fetch, referer=other), b, compat=True).trusted
    assert not classify("POST", _h(sec_fetch_site="same-site", sec_fetch_mode="no-cors"), b, compat=True).allow
    # SlotServe's own pages on the same origin (devenv_web_port 0) may link to an environment
    assert classify("GET", _h(**nav, referer="http://test/devenv"), b, own_pages=True).allow
    assert not classify("GET", _h(**fetch, referer="http://test/devenv"), b, own_pages=True).allow

    assert environment_of(f"{WEB}/devenv/12/port/80/x") == 12 and environment_of("http://x/devenv/a/") is None


def test_trust_groups_and_headers():
    class S:
        def __init__(self, id, kind, user_id, reservation_id=None):
            self.id, self.kind, self.user_id, self.reservation_id = id, kind, user_id, reservation_id

    assert trust_group(S(1, "booking", 7, 3)) == trust_group(S(2, "cpu", 7)) == "user:7"
    assert trust_group(S(3, "shared", None)) == "env:3"
    assert trust_group(S(4, None, None)) == "env:4"
    out = dict(response_headers([("referrer-policy", "no-referrer"), ("content-type", "text/html")]))
    assert out["referrer-policy"] == "same-origin" and out["cross-origin-opener-policy"] == "same-origin"
    assert out["content-security-policy"] == "frame-ancestors 'self'"
    denied = dict(response_headers([("content-security-policy", "x")], denied=True))
    assert denied["content-security-policy"] == "sandbox; frame-ancestors 'none'"
    assert denied["x-frame-options"] == "DENY"


def test_cookies():
    class S:
        secret_key = "k"

    good = web_auth.cookie_value(S, 1, 5)
    # a page can add same-named cookies for other paths; only signed ones count and they cannot push the real out
    assert web_auth.cookie_user(S, f"slotserve_web_5=junk; slotserve_web_5={good}", 5) == 1
    assert web_auth.cookie_user(S, f"slotserve_web_6={good}", 6) is None
    scope = web_auth.scope_value(S, 1, "user:1")
    assert web_auth.scope_groups(S, f"slotserve_web_scope=x; slotserve_web_scope={scope}", 1) == {"user:1"}
    assert web_auth.scope_groups(S, f"slotserve_web_scope={scope}", 2) == set()
    admin = web_auth.cookie_value(S, 1, 5, as_admin=True)
    assert web_auth.cookie_user(S, f"slotserve_web_5={admin}", 5) == 1
    assert web_auth.cookie_seconds(True) == 3600 and web_auth.cookie_seconds(False) == 12 * 3600
    note = web_auth.return_note(S, 5, "/devenv/5/port/80/x")
    assert web_auth.check_return_note(S, note, 5, "/devenv/5/port/80/x")
    assert not web_auth.check_return_note(S, note, 5, "/devenv/5/port/80/y")
    assert not web_auth.check_return_note(S, "forged", 5, "/devenv/5/port/80/x")


# ---- the listener --------------------------------------------------------------------------------------------


async def _session(**kw) -> int:
    web = {"token": f"tok-{kw.get('user_id')}-{kw['kind']}", "ports": [{"port": 8888, "kind": "jupyter"}]}
    async with SessionLocal() as db:
        s = DevSession(image="x", state="running", web=json.dumps(web), **kw)
        db.add(s)
        await db.commit()
        return s.id


async def _uid(name: str) -> int:
    async with SessionLocal() as db:
        return (await db.execute(select(User.id).where(User.username == name))).scalar_one()


@pytest.fixture
async def world(app, make_client):
    """root (admin), bob with his environment B, eve with hers (E), and a lane's shared environment A."""
    admin, bob, eve = await make_client("root"), await make_client("bob"), await make_client("eve")

    async def idle():  # the rows below have no containers; keep the manager away from them
        return None

    app.state.devenv.tick = idle
    b = await _session(kind="cpu", user_id=await _uid("bob"))
    e = await _session(kind="cpu", user_id=await _uid("eve"))
    a = await _session(kind="shared", user_id=None)
    port = _free_port()
    server, task = await _serve(UPSTREAM, port)
    app.state.devenv.port_target = lambda s, p: f"http://127.0.0.1:{port}" if s.state == "running" else None
    app.state.settings.devenv_web_port = 8001
    web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.web_app), base_url=WEB)
    yield {"admin": admin, "bob": bob, "eve": eve, "web": web, "a": a, "b": b, "e": e}
    await web.aclose()
    server.should_exit = True
    await task


async def _enter(main: httpx.AsyncClient, web: httpx.AsyncClient, sid: int, **headers) -> httpx.Response:
    r = await main.get(f"/devenv/{sid}/port/8888/lab", headers=headers)
    assert r.status_code == 302, r.text
    r = await web.get(r.headers["location"].removeprefix(WEB))
    assert r.status_code == 302
    return r


def _page_of(sid: int) -> dict[str, str]:
    return {
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": f"{WEB}/devenv/{sid}/port/8888/lab",
    }


async def test_cross_environment_requests_are_refused(world, app, caplog):
    bob, web, a, b = world["bob"], world["web"], world["a"], world["b"]
    await _enter(bob, web, b)
    api = f"/devenv/{b}/port/8888/api/kernels"
    r = await web.post(api, headers=_page_of(b))
    assert r.status_code == 200 and r.json()["headers"]["authorization"] == f"token tok-{await _uid('bob')}-cpu"
    assert r.headers["referrer-policy"] == "same-origin" and r.headers["cross-origin-opener-policy"] == "same-origin"

    caplog.set_level(logging.WARNING, logger="slotserve.gateway.devenv_web")
    # B's cookie, same-origin, cors, no Referer: refused (it used to pass)
    r = await web.post(api, headers={**_page_of(b), "referer": ""})
    assert r.status_code == 403 and r.json()["detail"]["params"]["reason"] == "referer_missing"
    assert r.headers["content-security-policy"] == "sandbox; frame-ancestors 'none'"
    # A's Referer: refused
    r = await web.post(api, headers=_page_of(a))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "devenv_web_cross_environment"
    # a no-cors POST from another port of the host (a direct port): refused, nothing reaches the app
    r = await web.post(api, headers={"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors"})
    assert r.status_code == 403
    # another environment's page opening B in a window or frame gets a sandboxed page, not B
    nav = {**_page_of(a), "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "accept": "text/html"}
    r = await web.get(f"/devenv/{b}/port/8888/lab", headers=nav)
    assert r.status_code == 403 and "sandbox" in r.headers["content-security-policy"]
    assert "Open in SlotServe" in r.text and f"/devenv/{b}/port/8888/lab" in r.text
    assert "refused" in caplog.text and "referer_missing" in caplog.text
    # the person's own navigation passes with the token
    typed = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    r = await web.get(f"/devenv/{b}/port/8888/lab", headers=typed)
    assert r.status_code == 200 and r.json()["headers"]["authorization"].startswith("token ")
    # untrusted (no Fetch Metadata): through, but without the token or the app's own cookies
    jar = "; ".join(f"{c.name}={c.value}" for c in web.cookies.jar)
    r = await web.post(api, headers={"cookie": f"app=1; {jar}"})
    seen = r.json()["headers"]
    assert r.status_code == 200 and "authorization" not in seen and "app=1" not in seen.get("cookie", "")
    # the proxy's own HTTP client keeps no cookies the apps set (they would reach every environment on the host)
    assert not list(app.state.devenv.web_client().cookies.jar)


async def test_one_trust_group_at_a_time(world):
    bob, web, a, b = world["bob"], world["web"], world["a"], world["b"]
    await _enter(bob, web, b)
    api = f"/devenv/{b}/port/8888/api/x"
    assert (await web.get(api, headers=_page_of(b))).status_code == 200
    # bob opens the lane's shared environment A: the browser now uses A on the shared origin
    r = await _enter(bob, web, a)
    assert "slotserve_web_scope=" in "".join(r.headers.get_list("set-cookie"))
    assert (await web.get(f"/devenv/{a}/port/8888/x", headers=_page_of(a))).status_code == 200
    # even a request that looks like B's own page (a Referer can be faked with history.replaceState) is refused
    r = await web.get(api, headers=_page_of(b))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "devenv_web_other_scope"
    # bob reloads his B tab himself: B is in use again, A stops working
    typed = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    r = await web.get(f"/devenv/{b}/port/8888/lab", headers=typed)
    assert r.status_code == 200 and "slotserve_web_scope=" in r.headers["set-cookie"]
    assert (await web.get(api, headers=_page_of(b))).status_code == 200
    assert (await web.get(f"/devenv/{a}/port/8888/x", headers=_page_of(a))).status_code == 403
    # compatibility mode lifts both cross-environment rules
    r = await world["admin"].put("/api/admin/settings", json={"devenv_web_compat": True})
    assert r.status_code == 200, r.text
    assert (await web.get(f"/devenv/{a}/port/8888/x", headers=_page_of(a))).status_code == 200
    assert (await web.get(api, headers={**_page_of(b), "referer": ""})).status_code == 200
    r = await web.post(api, headers={"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors"})
    assert r.status_code == 403  # but never cross-site sub-requests


async def test_admin_opens_someone_elses_environment(world, app):
    admin, web, e, a = world["admin"], world["web"], world["e"], world["a"]
    path = f"/devenv/{e}/port/8888/lab?x=1"
    # opening it asks first (a page on SlotServe's origin), nothing is audited yet
    r = await admin.get(path, headers={"sec-fetch-site": "same-origin", "accept-language": "zh-CN"})
    assert r.status_code == 200 and "<form method=post" in r.text and "eve" in r.text
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "管理员" in r.text  # zh
    assert not await _audits("devenv.open_as_admin")
    # the form posts back (CsrfGuard: SlotServe's own Origin)
    r = await admin.post(path, content="confirm=1", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303 and r.headers["location"].startswith(f"{WEB}/_slotserve/enter?token=")
    [row] = await _audits("devenv.open_as_admin")
    assert row.target == f"#{e}" and json.loads(row.detail) == {"owner": "eve", "port": 8888}
    r = await web.get(r.headers["location"].removeprefix(WEB))
    assert r.headers["location"] == path
    cookies = r.headers.get_list("set-cookie")
    assert all("Max-Age=3600" in c for c in cookies) and len(cookies) == 2  # an hour, not 12
    assert (await web.get(f"/devenv/{e}/port/8888/x", headers=_page_of(e))).status_code == 200
    # a form post from elsewhere cannot confirm it
    r = await admin.post(
        path, content="confirm=1", headers={"content-type": "application/x-www-form-urlencoded", "origin": WEB}
    )
    assert r.status_code == 403
    # the API for SlotServe's pages: 409 until confirmed, then a one-time link
    r = await admin.post(f"/api/devenv/sessions/{e}/web-open", json={"port": 8888, "path": "lab"})
    assert r.status_code == 409 and r.json()["detail"] == {
        "code": "devenv_open_as_admin_confirm",
        "params": {"owner": "eve"},
    }
    r = await admin.post(
        f"/api/devenv/sessions/{e}/web-open", json={"port": 8888, "path": "lab", "confirm_as_admin": True}
    )
    assert r.status_code == 200 and r.json()["as_admin"] is True and r.json()["expires_in"] == 60
    assert r.json()["url"].startswith(f"{WEB}/_slotserve/enter?token=")
    assert len(await _audits("devenv.open_as_admin")) == 2
    # a shared environment is not someone else's: no confirmation, no audit
    r = await admin.post(f"/api/devenv/sessions/{a}/web-open", json={"port": 8888})
    assert r.status_code == 200 and r.json()["as_admin"] is False
    assert (await admin.get(f"/devenv/{a}/port/8888/", headers={"sec-fetch-site": "same-origin"})).status_code == 302
    # the owner opens hers directly; others cannot at all; bad paths are refused
    eve = world["eve"]
    r = await eve.post(f"/api/devenv/sessions/{e}/web-open", json={"port": 8888, "path": "/lab/tree"})
    assert r.status_code == 200 and r.json()["as_admin"] is False
    assert (await world["bob"].post(f"/api/devenv/sessions/{e}/web-open", json={"port": 8888})).status_code == 404
    bad = await eve.post(f"/api/devenv/sessions/{e}/web-open", json={"port": 8888, "path": "a\\b"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "devenv_web_path_invalid"
    assert len(await _audits("devenv.open_as_admin")) == 2


async def test_handover_from_elsewhere_asks_first(world, app):
    bob, web, b = world["bob"], world["web"], world["b"]
    path = f"/devenv/{b}/port/8888/lab"
    # a link on another site or port of the host (for example a page in another environment): asks first
    r = await bob.get(path, headers={"sec-fetch-site": "same-site"})
    assert r.status_code == 200 and "<form method=post" in r.text
    # from SlotServe's own pages, or typed: straight through
    assert (await bob.get(path, headers={"sec-fetch-site": "same-origin"})).status_code == 302
    # the listener sends a person who typed the address (and has no cookie yet) back with a note: no question
    r = await web.get(
        path,
        headers={"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "accept": "text/html"},
    )
    assert r.status_code == 302 and web_auth.RETURN_PARAM in r.headers["location"]
    back = r.headers["location"].removeprefix(f"http://test:{app.state.settings.port}")
    r = await bob.get(back, headers={"sec-fetch-site": "same-site"})
    assert r.status_code == 302 and r.headers["location"].startswith(f"{WEB}/_slotserve/enter")
    r = await web.get(r.headers["location"].removeprefix(WEB))
    assert r.headers["location"] == path  # the note is not passed on to the app


async def _audits(action: str) -> list[AuditLog]:
    async with SessionLocal() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())
