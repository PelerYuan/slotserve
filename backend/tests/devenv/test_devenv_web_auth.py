"""R1-08: code-server runs with --auth password; only SlotServe's proxy signs people in (with code-server's session
cookie), and only for requests it trusts. Opening an app without its own login as a direct port needs a
confirmation."""

import hashlib

from sqlalchemy import select
from test_v09_devenv import _book, _setup, _ticks

from slotserve.containers.contributors.web import code_server_cookie
from slotserve.db import SessionLocal
from slotserve.gateway.devenv_web import Target, _forward_headers
from slotserve.orm import User

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


async def test_preview_is_redacted(app, make_client):
    """POST /api/devenv/preview: what an environment would get, without its secrets and without creating anything."""
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    await bob.put("/api/devenv/password", json={"password": "bob's environment password"})
    smb = {
        "name": "Lib",
        "kind": "smb",
        "purpose": "shared",
        "server": "nas",
        "remote_path": "share",
        "username": "svc",
        "password": "smb-secret-pw",
        "mount_path": "/team",
    }
    assert (await admin.post("/api/admin/storage", json=smb)).status_code == 201
    templates = {t["builtin_key"]: t for t in (await bob.get("/api/devenv/templates")).json()}
    created = len(docker.created)

    r = await bob.post("/api/devenv/preview", json={"template_id": templates["jupyter-pytorch"]["id"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["ok"] and p["kind"] == "booking" and p["gpus"] == ["0"] and p["memory"] > 0 and p["pids_limit"] == 4096
    assert p["web"] == [{"name": "JupyterLab", "port": 8888, "kind": "jupyter"}]
    assert p["ssh"] == {"keys": 0, "password": True, "accounts": 0, "needs_credentials": False}
    assert p["security"]["no_new_privileges"] and "NET_RAW" in p["security"]["cap_drop"]
    assert {"target": "/team", "type": "volume"}.items() <= next(m for m in p["mounts"] if m["target"] == "/team").items()
    assert "opt/slotserve/root.hash" in p["files"] and "opt/slotserve/ssh_host_key" in p["files"]
    text = r.text
    async with SessionLocal() as db:
        bob_hash = (await db.execute(select(User.devenv_password_hash).where(User.username == "bob"))).scalar_one()
    assert bob_hash not in text and "smb-secret-pw" not in text and "$6$" not in text
    env = dict(e.split("=", 1) for e in p["body"]["Env"])
    assert env["SLOTSERVE_WEB_TOKEN"] == "***" and env["JUPYTER_TOKEN"] == "***"
    assert "token=***" in p["body"]["Cmd"][2]  # the token inside the command line, too
    assert len(docker.created) == created  # nothing was created
    assert (await bob.get("/api/devenv/sessions")).json() == []

    # without a GPU; a template that needs credentials says so instead of failing
    r = (await bob.post("/api/devenv/preview", json={"template_id": templates["pytorch"]["id"], "gpu": False})).json()
    assert r["ok"] and r["kind"] == "cpu" and r["gpus"] == [] and r["memory"] == 16 * 1024**3
    await admin.put("/api/admin/settings", json={"devenv_password_login": False})
    r = (await bob.post("/api/devenv/preview", json={"template_id": templates["pytorch"]["id"]})).json()
    assert r == {"ok": False, "error": {"code": "ssh_key_required", "params": {}}}
    assert (await bob.post("/api/devenv/preview", json={"template_id": 999})).status_code == 404
