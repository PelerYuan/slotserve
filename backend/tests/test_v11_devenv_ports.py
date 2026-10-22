"""Ports of dev environments: added at runtime, direct host ports through forwarder containers (fixed per project,
released with the booking), limits, the admin overview, and the separate origin of the login-protected proxy."""

import asyncio
import json
import socket
from datetime import timedelta

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_v09_devenv import _setup, _ticks, ed25519_key

from slotserve.db import SessionLocal, utcnow
from slotserve.gateway import devenv_web_app
from slotserve.gateway import devenv_web_auth as web_auth
from slotserve.orm import DevPort, DevSession
from slotserve.services.devenv import network_name, project_network_name
from slotserve.services.devenv_ports import parse_listening

FWD_IMAGE = "alpine/socat:1.8.1.1"
# /proc/net/tcp: 8888 on all addresses, 6006 on loopback only, 22 (SSH), 5000 on all (not listed yet), one connection
PROC_NET_TCP = b"""  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:22B8 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1
   1: 0100007F:1776 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1
   2: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3 1
   3: 00000000:1388 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 4 1
   4: 0200A8C0:22B8 0300A8C0:D431 01 00000000:00000000 00:00000000 00000000     0        0 5 1
"""


def test_parse_listening():
    assert parse_listening(PROC_NET_TCP.decode()) == {8888: "all", 6006: "local", 22: "all", 5000: "all"}
    v6 = "0: 00000000000000000000000001000000:0BB8 00000000000000000000000000000000:0000 0A 0"
    assert parse_listening(v6) == {3000: "local"}


async def _running(app, client, template_id: int, **extra) -> dict:
    now = utcnow()
    body = {
        "start_at": now.isoformat(),
        "end_at": (now + timedelta(hours=1)).isoformat(),
        **({"devenv_template_id": template_id} if template_id else {}),
        **extra,
    }
    r = await client.post("/api/reservations", json=body)
    assert r.status_code == 201, r.text
    await _ticks(app)
    sessions = [s for s in (await client.get("/api/devenv/sessions")).json() if s["state"] == "running"]
    assert sessions, (await client.get("/api/devenv/sessions")).json()
    return {"booking": r.json()[0], "session": sessions[0]}


async def _env(app, make_client, web_ports=None, image="me/web:1"):
    admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    app.state.devenv.in_container = False
    app.state.fake_docker.images.update({image, FWD_IMAGE})
    body = {"name": "Web", "image": image, "web_ports": web_ports or []}
    t = (await bob.post("/api/devenv/templates", json=body)).json()
    return admin, bob, t


async def test_add_direct_and_login_ports_at_runtime(app, make_client):
    docker = app.state.fake_docker
    docker.listening = PROC_NET_TCP
    _admin, bob, t = await _env(app, make_client, [{"name": "Lab", "port": 8888, "kind": "jupyter"}])
    run = await _running(app, bob, t["id"])
    sid = run["session"]["id"]
    [env] = [c for c in docker.containers.values() if c["Running"]]

    # declared ports are listed, behind the login by default; what listens is probed inside the container
    data = (await bob.get(f"/api/devenv/sessions/{sid}/ports")).json()
    assert data["enabled"] and data["can_add"] and data["can_direct"] and data["range"] == "2300-2399"
    [lab] = data["ports"]
    assert lab["declared"] and lab["mode"] == "login" and lab["status"] == "login" and lab["id"] is None
    assert lab["url"] == f"/devenv/{sid}/port/8888/" and lab["direct_url"] is None and lab["listening"] == "all"
    assert data["detected"] == [5000]  # listening but not listed (SSH's 22 and listed ports are left out)
    assert any("/proc/net/tcp" in " ".join(cmd) for _cid, cmd in docker.exec_calls)

    # add a port directly: a host port from the range and a forwarder on the environment's network
    r = await bob.put(f"/api/devenv/sessions/{sid}/ports/6006", json={"name": "TensorBoard", "mode": "direct"})
    assert r.status_code == 200, r.text
    tb = next(p for p in r.json()["ports"] if p["port"] == 6006)
    assert tb["host_port"] == 2300 and tb["direct_url"] == "http://test:2300/" and tb["status"] == "starting"
    assert tb["listening"] == "local" and not tb["declared"]
    await _ticks(app, 1)
    fwd = docker.forwarders()[f"slotserve-fwd-{tb['id']}"]
    config = fwd["Config"]
    assert fwd["Running"] and config["Image"] == FWD_IMAGE and config["Entrypoint"] == ["socat"]
    assert config["Cmd"] == ["TCP-LISTEN:18080,fork,reuseaddr", f"TCP:slotserve-dev-{sid}:6006"]
    assert config["HostConfig"]["NetworkMode"] == network_name(sid)
    assert config["HostConfig"]["PortBindings"] == {"18080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2300"}]}
    assert config["HostConfig"]["CapDrop"] == ["ALL"] and config["User"] == "65534:65534"
    net = next(n for n in docker.networks.values() if n["Name"] == network_name(sid))
    assert fwd["Id"] in net["Containers"] and env["Running"]  # the environment kept running
    tb = next(p for p in (await bob.get(f"/api/devenv/sessions/{sid}/ports")).json()["ports"] if p["port"] == 6006)
    assert tb["status"] == "active"
    await _ticks(app, 1)
    assert len(docker.forwarders()) == 1 and len([c for c in docker.created if "fwd" in c[0]]) == 1  # left alone

    # a login port that is not declared; the declared one switched to direct
    r = await bob.put(f"/api/devenv/sessions/{sid}/ports/7860", json={})
    assert next(p for p in r.json()["ports"] if p["port"] == 7860)["mode"] == "login"
    r = await bob.put(f"/api/devenv/sessions/{sid}/ports/8888", json={"mode": "direct"})
    lab = next(p for p in r.json()["ports"] if p["port"] == 8888)
    assert lab["declared"] and lab["host_port"] == 2301 and lab["name"] == "Lab"
    await _ticks(app, 1)
    assert len(docker.forwarders()) == 2

    # back to the login: the host port is released and its forwarder goes
    r = await bob.put(f"/api/devenv/sessions/{sid}/ports/8888", json={"mode": "login"})
    assert next(p for p in r.json()["ports"] if p["port"] == 8888)["host_port"] is None
    await _ticks(app, 1)
    assert set(docker.forwarders()) == {f"slotserve-fwd-{tb['id']}"}

    # bad requests
    assert (await bob.put(f"/api/devenv/sessions/{sid}/ports/22", json={})).status_code == 400
    assert (await bob.put(f"/api/devenv/sessions/{sid}/ports/70000", json={})).status_code == 400
    eve = await make_client("eve")
    assert (await eve.put(f"/api/devenv/sessions/{sid}/ports/9000", json={})).status_code == 404
    assert (await eve.get(f"/api/devenv/sessions/{sid}/ports")).status_code == 404

    # removing a port removes its forwarder; the environment keeps running
    r = await bob.delete(f"/api/devenv/sessions/{sid}/ports/6006")
    assert r.status_code == 200 and 6006 not in [p["port"] for p in r.json()["ports"]]
    assert not docker.forwarders() and env["Running"]
    assert (await bob.delete(f"/api/devenv/sessions/{sid}/ports/6006")).status_code == 404

    # the booking ends: forwarders and ports go with it, the network can be removed
    await bob.put(f"/api/devenv/sessions/{sid}/ports/6006", json={"mode": "direct"})
    await _ticks(app, 1)
    assert docker.forwarders()
    await bob.delete(f"/api/reservations/{run['booking']['id']}")
    await _ticks(app, 2)
    assert not docker.forwarders() and not docker.networks and not docker.containers
    async with SessionLocal() as db:
        assert (await db.execute(DevPort.__table__.select())).first() is None


async def test_limits_and_switches(app, make_client):
    docker = app.state.fake_docker
    admin, bob, t = await _env(app, make_client)
    sid = (await _running(app, bob, t["id"]))["session"]["id"]
    url = f"/api/devenv/sessions/{sid}/ports"

    await admin.put("/api/admin/settings", json={"devenv_public_port_limit": 1})
    assert (await bob.put(f"{url}/8000", json={"mode": "direct"})).status_code == 200
    r = await bob.put(f"{url}/8001", json={"mode": "direct"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_public_port_limit"
    assert (await bob.get(url)).json()["can_direct"]  # has one already
    # an admin may add one more for them
    assert (await admin.put(f"{url}/8001", json={"mode": "direct"})).status_code == 200

    # 0: only admins
    await admin.put("/api/admin/settings", json={"devenv_public_port_limit": 0})
    r = await bob.put(f"{url}/8002", json={"mode": "direct"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "devenv_public_ports_admin_only"
    assert (await bob.put(f"{url}/8002", json={"mode": "login"})).status_code == 200

    # switched off: nothing new, and the forwarders stop (the ports stay assigned)
    await _ticks(app, 1)
    assert len(docker.forwarders()) == 2
    await admin.put("/api/admin/settings", json={"devenv_public_ports_enabled": False})
    r = await admin.put(f"{url}/8003", json={"mode": "direct"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_public_ports_disabled"
    await _ticks(app, 1)
    assert not docker.forwarders()
    assert {p["status"] for p in (await bob.get(url)).json()["ports"] if p["mode"] == "direct"} == {"off"}
    await admin.put("/api/admin/settings", json={"devenv_public_ports_enabled": True})
    await _ticks(app, 1)
    assert len(docker.forwarders()) == 2

    # a full range
    await admin.put("/api/admin/settings", json={"devenv_public_ports": "2300-2301", "devenv_public_port_limit": 9})
    r = await bob.put(f"{url}/8004", json={"mode": "direct"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_public_ports_full"
    bad = await admin.put("/api/admin/settings", json={"devenv_public_ports": "2400"})
    assert bad.status_code == 400


async def test_host_port_taken_and_forwarder_image_pulled(app, make_client):
    docker = app.state.fake_docker
    _admin, bob, t = await _env(app, make_client)
    docker.images.discard(FWD_IMAGE)
    sid = (await _running(app, bob, t["id"]))["session"]["id"]
    docker.busy_ports.add("2300")  # something else on the host holds the first port
    r = await bob.put(f"/api/devenv/sessions/{sid}/ports/8000", json={"mode": "direct"})
    assert next(p for p in r.json()["ports"] if p["port"] == 8000)["host_port"] == 2300
    await _ticks(app, 1)  # the forwarder image is pulled first
    for _ in range(50):
        if FWD_IMAGE in docker.images:
            break
        await asyncio.sleep(0.02)
    assert FWD_IMAGE in docker.pulls
    await _ticks(app, 2)
    # never used yet: it moves to the next free number
    [fwd] = docker.forwarders().values()
    assert fwd["Running"] and fwd["Config"]["HostConfig"]["PortBindings"]["18080/tcp"][0]["HostPort"] == "2301"
    port = next(p for p in (await bob.get(f"/api/devenv/sessions/{sid}/ports")).json()["ports"] if p["port"] == 8000)
    assert port["host_port"] == 2301 and port["status"] == "active"


async def test_project_keeps_its_host_port(app, make_client):
    docker = app.state.fake_docker
    admin, bob, t = await _env(app, make_client, [{"name": "App", "port": 7860, "kind": "http", "mode": "direct"}])
    first = await _running(app, bob, t["id"], devenv_project_name="Demo")
    sid = first["session"]["id"]
    [project] = (await bob.get("/api/devenv/projects")).json()
    # the template declares the port as direct: it gets a host port when the environment starts
    await _ticks(app, 1)
    [app_port] = (await bob.get(f"/api/devenv/sessions/{sid}/ports")).json()["ports"]
    assert app_port["mode"] == "direct" and app_port["declared"] and app_port["host_port"] == 2300
    assert app_port["status"] == "active"
    [fwd] = docker.forwarders().values()
    assert fwd["Config"]["Cmd"][1] == f"TCP:slotserve-proj-{project['id']}:7860"
    assert fwd["Config"]["HostConfig"]["NetworkMode"] == project_network_name(project["id"])
    added = await bob.put(f"/api/devenv/sessions/{sid}/ports/9000", json={"mode": "direct", "name": "API"})
    assert next(p for p in added.json()["ports"] if p["port"] == 9000)["host_port"] == 2301
    await _ticks(app, 1)
    assert len(docker.forwarders()) == 2

    # paused: the forwarders go, the numbers stay with the project
    await bob.delete(f"/api/reservations/{first['booking']['id']}")
    await _ticks(app, 2)
    assert not docker.forwarders()
    mine = (await bob.get("/api/devenv/public-ports")).json()
    assert [(p["host_port"], p["container_port"], p["project_name"], p["status"]) for p in mine] == [
        (2300, 7860, "Demo", "paused"),
        (2301, 9000, "Demo", "paused"),
    ]
    # nobody else gets those numbers meanwhile
    async with SessionLocal() as db:
        assert await app.state.devenv.ports.free_host_port(db) == 2302

    # resumed: the same numbers come back
    second = await _running(app, bob, 0, devenv_project_id=project["id"])
    await _ticks(app, 1)
    sid2 = second["session"]["id"]
    ports = (await bob.get(f"/api/devenv/sessions/{sid2}/ports")).json()["ports"]
    assert [(p["port"], p["host_port"], p["status"]) for p in ports] == [(7860, 2300, "active"), (9000, 2301, "active")]
    assert len(docker.forwarders()) == 2

    # an admin reclaims one: it stays listed behind the login; its number is free again
    everyone = (await admin.get("/api/devenv/public-ports", params={"everyone": True})).json()
    assert {p["owner"] for p in everyone} == {"bob"}
    eve = await make_client("eve")
    assert (await eve.get("/api/devenv/public-ports", params={"everyone": True})).json() == []
    reclaim = next(p for p in everyone if p["host_port"] == 2301)
    assert (await eve.delete(f"/api/devenv/public-ports/{reclaim['id']}")).status_code == 404
    assert (await admin.delete(f"/api/devenv/public-ports/{reclaim['id']}")).status_code == 204
    ports = (await bob.get(f"/api/devenv/sessions/{sid2}/ports")).json()["ports"]
    assert next(p for p in ports if p["port"] == 9000)["mode"] == "login" and len(docker.forwarders()) == 1
    audit = (await admin.get("/api/admin/audit")).json()
    assert any(a["action"] == "devenv.port_reclaim" for a in (audit["items"] if isinstance(audit, dict) else audit))

    # deleting the project releases its ports
    await bob.delete(f"/api/reservations/{second['booking']['id']}")
    await _ticks(app, 2)
    assert (await bob.delete(f"/api/devenv/projects/{project['id']}")).status_code == 204
    assert (await bob.get("/api/devenv/public-ports")).json() == []
    assert not docker.forwarders() and not docker.networks


async def test_saved_image_keeps_ports_and_restart_cleanup(app, make_client):
    docker = app.state.fake_docker
    _admin, bob, t = await _env(app, make_client, [{"name": "Lab", "port": 8888, "kind": "jupyter"}])
    sid = (await _running(app, bob, t["id"]))["session"]["id"]
    await bob.put(f"/api/devenv/sessions/{sid}/ports/6006", json={"name": "TB", "mode": "direct"})
    await bob.put(f"/api/devenv/sessions/{sid}/ports/8888", json={"name": "Notebook"})
    assert (await bob.post(f"/api/devenv/sessions/{sid}/save", json={"name": "With ports"})).status_code == 202
    for _ in range(50):
        saved = [x for x in (await bob.get("/api/devenv/templates")).json() if x["kind"] == "saved"]
        if saved:
            break
        await asyncio.sleep(0.05)
    assert saved[0]["web_ports"] == [
        {"name": "Notebook", "port": 8888, "kind": "jupyter", "mode": "login"},
        {"name": "TB", "port": 6006, "kind": "http", "mode": "direct"},
    ]

    # after a SlotServe restart leftover forwarders are removed and the wanted ones started again
    await _ticks(app, 1)
    stale = docker.add_container("slotserve-fwd-999", {"slotserve.devenv": "forwarder", "slotserve.name": "x"})
    app.state.devenv._cleaned = False
    await _ticks(app, 1)
    assert stale not in docker.containers and len(docker.forwarders()) == 1


async def test_shared_environment_only_declared_ports(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    docker.images.add(FWD_IMAGE)
    body = {
        "name": "Shared",
        "image": "me/shared:1",
        "web_ports": [{"name": "Board", "port": 3000, "mode": "direct"}],
        "shared": True,
    }
    tpl = (await admin.post("/api/devenv/templates", json=body)).json()
    docker.images.add("me/shared:1")
    lane = (await admin.get("/api/lanes")).json()[0]
    patch = {"name": lane["name"], "idle_mode": "devenv", "idle_template_id": tpl["id"]}
    assert (await admin.patch(f"/api/admin/lanes/{lane['id']}", json=patch)).status_code == 200
    scheduler = app.state.lanes.get(lane["id"]).scheduler
    for _ in range(200):
        if scheduler.target is not None and scheduler.target.reason == "idle-devenv":
            break
        await asyncio.sleep(0.05)
    await _ticks(app)
    shared = next(s for s in (await bob.get("/api/devenv/sessions")).json() if s["kind"] == "shared")
    assert shared["state"] == "running"
    data = (await bob.get(f"/api/devenv/sessions/{shared['id']}/ports")).json()
    assert not data["can_add"]
    [board] = data["ports"]
    assert board["mode"] == "direct" and board["host_port"] == 2300 and board["status"] == "active"
    r = await admin.put(f"/api/devenv/sessions/{shared['id']}/ports/8080", json={})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_shared_ports"
    assert (await bob.put(f"/api/devenv/sessions/{shared['id']}/ports/3000", json={})).status_code == 403
    [fwd] = docker.forwarders().values()
    assert fwd["Config"]["Cmd"][1] == f"TCP:slotserve-idle-{lane['id']}:3000"
    async with SessionLocal() as db:
        row = (await db.execute(DevPort.__table__.select())).first()
        assert row.owner_id is None and row.session_id == shared["id"]


# ---- the separate origin of the login-protected proxy -----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _echo(request: Request):
    return JSONResponse({"path": request.url.path, "cookie": request.headers.get("cookie")})


UPSTREAM = Starlette(routes=[Route("/{path:path}", _echo, methods=["GET", "POST"])])


async def _serve(asgi_app, port: int):
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    return server, task


async def test_web_interfaces_on_their_own_origin(app, make_client):
    settings = app.state.settings
    _admin, bob, t = await _env(app, make_client, [{"name": "App", "port": 7860}])
    sid = (await _running(app, bob, t["id"]))["session"]["id"]
    up_port = _free_port()
    upstream, up_task = await _serve(UPSTREAM, up_port)
    app.state.devenv.port_target = lambda s, p: f"http://127.0.0.1:{up_port}" if s.state == "running" else None
    settings.devenv_web_port = 8001
    web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.web_app), base_url="http://test:8001")
    try:
        path = f"/devenv/{sid}/port/7860/x?y=1"
        # SlotServe's own port only signs people in and hands them over
        anonymous = await make_client()
        r = await anonymous.get(path, headers={"accept": "text/html"})
        assert r.status_code == 302 and r.headers["location"].startswith("/login?next=")
        eve = await make_client("eve")
        assert (await eve.get(path)).status_code == 404
        r = await bob.get(path)
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("http://test:8001/_slotserve/enter?token=")
        assert (await bob.post(path)).json()["detail"]["code"] == "devenv_web_moved"

        # the listener never accepts SlotServe's session cookie
        main_cookie = bob.cookies.get("slotserve_session")
        r = await web.get(path, headers={"cookie": f"slotserve_session={main_cookie}", "accept": "text/html"})
        assert (
            r.status_code == 302
            and r.headers["location"] == f"http://test:{settings.port}/devenv/{sid}/port/7860/x?y=1"
        )

        # the token becomes a cookie for this environment's path only; it works once
        enter = location.removeprefix("http://test:8001")
        r = await web.get(enter)
        assert r.status_code == 302 and r.headers["location"] == path
        set_cookie = r.headers["set-cookie"]
        assert set_cookie.startswith(f"slotserve_web_{sid}=") and f"Path=/devenv/{sid}/" in set_cookie
        assert "HttpOnly" in set_cookie
        assert (await web.get(enter)).status_code == 400
        r = await web.get(path)
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "/x" and r.json()["cookie"] is None  # SlotServe's cookies never reach the app

        # a page of another environment cannot use it
        headers = {"sec-fetch-site": "same-origin", "referer": "http://test:8001/devenv/999/port/80/"}
        assert (await web.get(path, headers=headers)).status_code == 403
        headers["referer"] = f"http://test:8001/devenv/{sid}/port/80/"
        assert (await web.get(path, headers=headers)).status_code == 200

        # a cookie for one environment does not open another, nor after the person loses access
        value = web.cookies.get(f"slotserve_web_{sid}")
        r = await web.get(f"/devenv/{sid + 1}/port/7860/", headers={"cookie": f"slotserve_web_{sid + 1}={value}"})
        assert r.status_code == 401
        forged = web_auth.cookie_value(settings, 999, sid)
        fresh = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.web_app), base_url="http://test:8001")
        assert (await fresh.get(path, headers={"cookie": f"slotserve_web_{sid}={forged}"})).status_code == 401
        await fresh.aclose()
        async with SessionLocal() as db:
            (await db.get(DevSession, sid)).state = "failed"
            await db.commit()
        assert (await web.get(path)).status_code == 503

        # pages on the listener's origin cannot change anything in SlotServe
        r = await bob.post(f"/api/devenv/sessions/{sid}/restart", headers={"origin": "http://test:8001"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "cross_origin_refused"
        r = await bob.post(f"/api/devenv/sessions/{sid}/restart", headers={"origin": "http://test"})
        assert r.status_code == 202
        settings.devenv_web_url = "https://envs.example:8443"
        r = await bob.post("/api/devenv/sessions/1/restart", headers={"origin": "https://envs.example:8443"})
        assert r.status_code == 403
        assert (await bob.get(path)).headers["location"].startswith("https://envs.example:8443/_slotserve/enter?")
        assert (await web.get("/")).status_code == 302 and (await web.get("/api/devenv")).status_code == 404
    finally:
        await web.aclose()
        upstream.should_exit = True
        await up_task


async def test_web_listener_runs_in_process(app):
    port = _free_port()
    running = await devenv_web_app.start(app.state.web_app, "127.0.0.1", port)
    assert running is not None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/devenv/1/port/80/", headers={"accept": "application/json"})
            assert r.status_code == 401
            r = await c.get(f"http://127.0.0.1:{port}/_slotserve/enter?token=nope")
            assert r.status_code == 400
        # the port is taken: SlotServe logs it and carries on
        assert await devenv_web_app.start(app.state.web_app, "127.0.0.1", port) is None
    finally:
        await devenv_web_app.stop(running)
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{port}/")


def test_handover_tokens():
    class S:
        secret_key = "k"

    h = web_auth.Handovers()
    kw = {"group": "user:1", "as_admin": False}
    token = h.issue(S, 1, 5, "/devenv/5/port/80/a", **kw)
    data = h.redeem(S, token)
    assert data["u"] == 1 and data["g"] == "user:1" and data["a"] is False and h.redeem(S, token) is None
    assert h.redeem(S, h.issue(S, 1, 5, "/devenv/6/port/80/", **kw)) is None  # the path names another environment
    assert h.redeem(S, h.issue(S, 1, 5, "https://evil/", **kw)) is None
    assert h.redeem(S, "garbage") is None
    assert json.loads(json.dumps(web_auth.cookie_user(S, f"slotserve_web_5={web_auth.cookie_value(S, 1, 5)}", 5))) == 1
