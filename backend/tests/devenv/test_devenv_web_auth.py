"""R1-08: code-server runs with --auth password; only SlotServe's proxy signs people in (with code-server's session
cookie), and only for requests it trusts. Opening an app without its own login as a direct port needs a
confirmation."""

import hashlib

from test_v09_devenv import _book, _setup, _ticks

from slotserve.containers.contributors.web import code_server_cookie
from slotserve.gateway.devenv_web import Target, _forward_headers

TOKEN = "tok-123"


def _cookie(t: Target, sent: str | None) -> str | None:
    items = [("cookie", sent)] if sent else []
    return dict(_forward_headers(items, {"cookie"}, t, "http", "1.2.3.4")).get("cookie")


def test_code_server_cookie_matches_its_hashed_password():
    name, value = code_server_cookie(TOKEN)
    assert name == "code-server-session" and value == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_proxy_signs_in_to_code_server_only_when_trusted():
    trusted = Target(5, 8080, "vscode", True, TOKEN, "http://c:8080", trusted=True)
    name, value = code_server_cookie(TOKEN)
    assert _cookie(trusted, None) == f"{name}={value}"
    # the browser's own code-server cookie is replaced, other app cookies stay
    assert _cookie(trusted, f"a=1; {name}=forged") == f"a=1; {name}={value}"
    untrusted = Target(5, 8080, "vscode", True, TOKEN, "http://c:8080", trusted=False)
    assert _cookie(untrusted, "a=1") is None
    other = Target(5, 8888, "jupyter", False, TOKEN, "http://c:8888", trusted=True)
    assert _cookie(other, None) is None


async def test_vscode_template_and_direct_port_confirmation(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    vscode = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "vscode")
    assert "--auth password" in vscode["command"] and vscode["env"] == {"HASHED_PASSWORD": "{token_sha256}"}
    desktop = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "desktop")
    assert desktop["compat"] is True and vscode["compat"] is False
    await _book(bob, vscode["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    [container] = [c for c in docker.containers.values() if c["Running"]]
    env = dict(e.split("=", 1) for e in container["Config"]["Env"])
    assert env["HASHED_PASSWORD"] == hashlib.sha256(env["SLOTSERVE_WEB_TOKEN"].encode()).hexdigest()
    assert "--auth none" not in " ".join(container["Config"]["Cmd"])

    url = f"/api/devenv/sessions/{session['id']}/ports/8080"
    r = await bob.put(url, json={"mode": "direct"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_port_unauthenticated"
    assert r.json()["detail"]["params"] == {"kind": "vscode"}
    r = await bob.put(url, json={"mode": "direct", "confirm_unauthenticated": True})
    assert r.status_code == 200, r.text
    # a plain HTTP port needs no confirmation
    r = await bob.put(f"/api/devenv/sessions/{session['id']}/ports/6006", json={"mode": "direct"})
    assert r.status_code == 200, r.text


async def test_non_admins_cannot_switch_on_compatibility_mode(app, make_client):
    admin, bob = await _setup(app, make_client)
    r = await bob.post("/api/devenv/templates", json={"name": "Mine", "image": "example/x:1", "compat": True})
    assert r.status_code == 403
    mine = (await bob.post("/api/devenv/templates", json={"name": "Mine", "image": "example/x:1"})).json()
    assert (await bob.patch(f"/api/devenv/templates/{mine['id']}", json={"compat": True})).status_code == 403
    r = await admin.patch(f"/api/devenv/templates/{mine['id']}", json={"compat": True})
    assert r.status_code == 200 and r.json()["compat"] is True
