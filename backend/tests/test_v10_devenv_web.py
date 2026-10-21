"""Web interfaces of dev environments: declared ports, container setup, networks, access and the reverse proxy
(HTTP + WebSocket) at /devenv/<session>/port/<port>/."""

import asyncio
import json
import socket
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevSession
from slotserve.services.devenv import (
    can_access,
    fill_placeholders,
    network_name,
    placeholder_values,
    project_network_name,
)


def _env(config: dict) -> dict[str, str]:
    return dict(e.split("=", 1) for e in config["Env"])


async def _templates(client) -> dict[str, dict]:
    return {t["builtin_key"] or t["name"]: t for t in (await client.get("/api/devenv/templates")).json()}


async def _web(sid: int) -> tuple[DevSession, dict]:
    async with SessionLocal() as db:
        row = await db.get(DevSession, sid)
        return row, json.loads(row.web)


def test_placeholders_and_access_rule():
    ports = [{"port": 8888}, {"port": 6006}]
    values = placeholder_values(7, ports, "abc")
    text = "x --base={base_path} --tb={base_path_6006} -p {port} ${HOME} {token}{other}"
    assert fill_placeholders(text, values) == (
        "x --base=/devenv/7/port/8888/ --tb=/devenv/7/port/6006/ -p 8888 ${HOME} abc{other}"
    )
    owner = SimpleNamespace(id=1, is_admin=False)
    admin = SimpleNamespace(id=2, is_admin=True)
    other = SimpleNamespace(id=3, is_admin=False)
    booked = SimpleNamespace(reservation_id=10, user_id=1)
    shared = SimpleNamespace(reservation_id=None, user_id=1)
    assert can_access(booked, owner) and can_access(booked, admin) and not can_access(booked, other)
    assert can_access(shared, other)


async def test_web_templates_containers_and_networks(app, make_client):
    docker = app.state.fake_docker
    manager = app.state.devenv
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    templates = await _templates(bob)
    assert templates["jupyter-pytorch"]["web_ports"] == [
        {"name": "JupyterLab", "port": 8888, "kind": "jupyter", "mode": "login"}
    ]
    assert templates["desktop"]["keep_entrypoint"] and templates["desktop"]["web_ports"][0]["kind"] == "desktop"
    assert templates["vscode"]["web_ports"][0]["port"] == 8080 and templates["pytorch"]["web_ports"] == []

    # declared ports: validated, unique; a port without a kind is plain HTTP; an empty list clears them
    def body(ports):
        return {"name": "x", "image": "a/b:1", "web_ports": ports}

    assert (await bob.post("/api/devenv/templates", json=body([{"port": 80, "kind": "ftp"}]))).status_code == 422
    assert (await bob.post("/api/devenv/templates", json=body([{"port": 0}]))).status_code == 422
    r = await bob.post("/api/devenv/templates", json=body([{"port": 80}, {"port": 80, "name": "again"}]))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_web_port_duplicate"
    mine = (await bob.post("/api/devenv/templates", json=body([{"port": 7860}]))).json()
    assert (
        mine["web_ports"] == [{"name": "7860", "port": 7860, "kind": "http", "mode": "login"}]
        and not mine["keep_entrypoint"]
    )
    patched = (await bob.patch(f"/api/devenv/templates/{mine['id']}", json={"web_ports": []})).json()
    assert patched["web_ports"] == []

    # the desktop keeps its image's entrypoint (an init system); SlotServe runs on the host here
    manager.in_container = False
    booking = (await _book(bob, templates["desktop"]["id"])).json()[0]
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running", session
    sid = session["id"]
    assert session["web"] == [{"name": "Desktop", "port": 3000, "kind": "desktop", "url": f"/devenv/{sid}/port/3000/"}]
    [container] = docker.containers.values()
    config = container["Config"]
    assert "Entrypoint" not in config and "Cmd" not in config and "User" not in config
    assert config["HostConfig"]["Init"] is False
    assert config["HostConfig"]["NetworkMode"] == network_name(sid)
    bindings = config["HostConfig"]["PortBindings"]
    assert bindings["22/tcp"][0]["HostIp"] == "0.0.0.0"
    assert bindings["3000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": ""}]  # never public
    row, web = await _web(sid)
    env = _env(config)
    assert env["SUBFOLDER"] == f"/devenv/{sid}/port/3000/" and env["PASSWORD"] == web["token"]
    assert len(web["token"]) > 20 and env["CUSTOM_USER"] == "slotserve" and env["SLOTSERVE_WEB_TOKEN"] == web["token"]
    assert web["ports"][0]["strip"] is False
    published = web["published"]["3000"]
    assert published.startswith("127.0.0.1:") and published != "127.0.0.1:"
    # one function resolves (session, port): declared ports via 127.0.0.1, any other on the container's address
    assert manager.port_target(row, 3000) == f"http://{published}"
    assert manager.port_target(row, 5000) == f"http://{web['ip']}:5000" and web["ip"].startswith("172.30.")
    [ssh_exec] = docker.execs.values()
    assert ssh_exec["Container"] == container["Id"]
    assert ssh_exec["Config"]["Cmd"] == ["/bin/sh", "/opt/slotserve/ssh.sh"]
    assert ssh_exec["Started"] is True and ssh_exec["Config"]["User"] == "root"
    assert b"dropbear" in docker.archives[container["Id"]]["opt/slotserve/ssh.sh"]
    [net] = docker.networks.values()
    assert net["Name"] == network_name(sid) and net["Labels"]["slotserve.session"] == str(sid)

    # the booking ends: container and network are removed
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    assert not docker.containers and not docker.networks
    assert (await bob.get("/api/devenv/sessions")).json()[0]["web"] == []
    assert await _web_sid_cleared(sid)

    # JupyterLab with SlotServe in a container: the start script stays, the app gets its base path and token,
    # nothing is published and SlotServe joins the environment's network (so any port is reachable by name)
    manager.in_container, manager.self_container = True, "slotserve-self"
    await _book(bob, templates["jupyter-pytorch"]["id"])
    await _ticks(app)
    session = next(s for s in (await bob.get("/api/devenv/sessions")).json() if s["state"] == "running")
    sid = session["id"]
    [container] = docker.containers.values()
    config = container["Config"]
    assert config["Entrypoint"] == ["/bin/sh", "/opt/slotserve/start.sh"] and config["User"] == "root"
    assert config["HostConfig"]["Init"] is True
    assert list(config["HostConfig"]["PortBindings"]) == ["22/tcp"] and "8888/tcp" in config["ExposedPorts"]
    row, web = await _web(sid)
    command = config["Cmd"][2]
    assert f"--ServerApp.base_url=/devenv/{sid}/port/8888/" in command
    assert f"--IdentityProvider.token={web['token']}" in command
    assert _env(config)["JUPYTER_TOKEN"] == web["token"]
    assert len(docker.execs) == 1  # no exec this time: SSH comes from the start script
    assert manager.port_target(row, 8888) == f"http://slotserve-dev-{sid}:8888"
    assert manager.port_target(row, 6006) == f"http://slotserve-dev-{sid}:6006"
    net = next(iter(docker.networks.values()))
    assert net["Containers"] == {container["Id"], "slotserve-self"}

    # after a restart: leftover networks go, running environments' networks are joined again
    stale = docker.networks["n9999"] = {
        "Id": "n9999",
        "Name": network_name(999),
        "Labels": {"slotserve.devenv": "1", "slotserve.session": "999"},
        "Containers": {"slotserve-self"},
    }
    net["Containers"].discard("slotserve-self")  # SlotServe was recreated
    manager._cleaned = False
    await _ticks(app, 1)
    assert stale["Id"] not in docker.networks and "slotserve-self" in net["Containers"]

    # a failure also removes the network
    container["Running"], container["ExitCode"] = False, 1
    await _ticks(app, 1)
    assert not docker.networks and not docker.containers


async def _web_sid_cleared(sid: int) -> bool:
    async with SessionLocal() as db:
        return (await db.get(DevSession, sid)).web is None


async def test_several_ports_and_base_paths(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    app.state.devenv.in_container = False
    ports = [{"name": "Lab", "port": 8888, "kind": "jupyter"}, {"name": "TensorBoard", "port": 6006}]
    t = (
        await bob.post(
            "/api/devenv/templates",
            json={
                "name": "Two",
                "image": "me/two:1",
                "command": "lab --base={base_path} & tensorboard --path_prefix={base_path_6006}",
                "env": {"APP_URL": "{base_path_8888}"},
                "web_ports": ports,
            },
        )
    ).json()
    docker.images.add("me/two:1")
    await _book(bob, t["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    sid = session["id"]
    assert [(w["name"], w["url"]) for w in session["web"]] == [
        ("Lab", f"/devenv/{sid}/port/8888/"),
        ("TensorBoard", f"/devenv/{sid}/port/6006/"),
    ]
    [container] = docker.containers.values()
    config = container["Config"]
    assert (
        config["Cmd"][2] == f"lab --base=/devenv/{sid}/port/8888/ & tensorboard --path_prefix=/devenv/{sid}/port/6006/"
    )
    assert _env(config)["APP_URL"] == f"/devenv/{sid}/port/8888/"
    assert set(config["HostConfig"]["PortBindings"]) == {"22/tcp", "8888/tcp", "6006/tcp"}
    _row, web = await _web(sid)
    assert [p["strip"] for p in web["ports"]] == [False, False] and set(web["published"]) == {"8888", "6006"}


# ---- the proxy -------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _echo(request: Request):
    if request.url.path.endswith("/redirect"):
        return RedirectResponse("/elsewhere?x=1", status_code=302)
    if request.url.path.endswith("/absolute"):
        return RedirectResponse(f"http://{request.headers['host']}/abs", status_code=302)
    if request.url.path.endswith("/stream"):

        async def chunks():
            for i in range(3):
                yield f"chunk{i};".encode()
                await asyncio.sleep(0.01)

        return StreamingResponse(chunks(), media_type="text/plain")
    body = await request.body()
    response = JSONResponse(
        {
            "method": request.method,
            "path": request.scope["raw_path"].decode(),
            "query": request.url.query,
            "body_size": len(body),
            "headers": {k: v for k, v in request.headers.items()},
        }
    )
    response.set_cookie("app", "1", path="/")
    response.set_cookie("slotserve_session", "evil", path="/")
    return response


async def _ws_echo(ws: WebSocket):
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol=offered[0] if offered else None)
    await ws.send_json(
        {
            "path": ws.scope["raw_path"].decode(),
            "auth": ws.headers.get("authorization"),
            "origin": ws.headers.get("origin"),
        }
    )
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("text") == "bye":
            await ws.close(code=4000)
            return
        if msg.get("text") is not None:
            await ws.send_text("echo:" + msg["text"])
        else:
            await ws.send_bytes(b"echo:" + msg["bytes"])


UPSTREAM = Starlette(
    routes=[
        WebSocketRoute("/{path:path}", _ws_echo),
        Route("/{path:path}", _echo, methods=["GET", "POST", "PUT", "DELETE", "HEAD"]),
    ]
)


async def _serve(asgi_app, port: int):
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    return server, task


@pytest.fixture
async def servers(app):
    up_port, ss_port = _free_port(), _free_port()
    upstream, up_task = await _serve(UPSTREAM, up_port)
    slotserve, ss_task = await _serve(app, ss_port)
    yield up_port, ss_port
    for server in (upstream, slotserve):
        server.should_exit = True
    await asyncio.gather(up_task, ss_task)


async def _running_web_session(app, make_client, template_body: dict):
    admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    app.state.devenv.in_container = False
    t = (await bob.post("/api/devenv/templates", json={"name": "Web", "image": "me/web:1", **template_body})).json()
    app.state.fake_docker.images.add("me/web:1")
    await _book(bob, t["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running", session
    return admin, bob, session


def _upstream_at(app, port: int) -> list[int]:
    """Points every session port at the test upstream; returns the ports asked for."""
    asked: list[int] = []

    def resolve(s, p):
        asked.append(p)
        return f"http://127.0.0.1:{port}" if s.state == "running" else None

    app.state.devenv.port_target = resolve
    return asked


async def test_proxy_access(app, make_client):
    admin, bob, session = await _running_web_session(app, make_client, {"web_ports": [{"port": 8888}]})
    url = session["web"][0]["url"]
    _upstream_at(app, 9)  # nothing listens there

    anonymous = await make_client()
    r = await anonymous.get(url, headers={"accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == f"/login?next={url}"
    assert (await anonymous.get(url + "api/x")).status_code == 401
    eve = await make_client("eve")
    assert (await eve.get(url)).status_code == 404
    assert (await bob.get(url + "api/x")).status_code == 502  # allowed, but the app does not answer
    assert (await admin.get(url + "api/x")).status_code == 502
    assert (await bob.get(url.rstrip("/") + "?a=1")).headers["location"] == url + "?a=1"
    # the SPA does not serve these paths
    assert (await bob.get("/devenv/999/port/80/x")).status_code == 404

    # not running any more: nothing to open
    async with SessionLocal() as db:
        (await db.get(DevSession, session["id"])).state = "failed"
        await db.commit()
    assert (await bob.get(url + "api/x")).status_code == 503


async def test_proxy_http_and_websocket(app, make_client, servers):
    up_port, ss_port = servers
    template = {
        "web_ports": [{"name": "Lab", "port": 8888, "kind": "jupyter"}],
        "command": "jupyter lab --ServerApp.base_url={base_path}",
    }
    _admin, bob, session = await _running_web_session(app, make_client, template)
    asked = _upstream_at(app, up_port)
    sid, prefix = session["id"], session["web"][0]["url"]
    _row, web = await _web(sid)
    token = web["token"]
    cookie = bob.cookies.get("slotserve_session")
    base = f"http://127.0.0.1:{ss_port}"

    async with httpx.AsyncClient(base_url=base, headers={"cookie": f"slotserve_session={cookie}; theme=dark"}) as c:
        # the app knows its base path: it gets the full path, the token, not SlotServe's cookie
        r = await c.get(prefix + "api/contents/a%20b?type=notebook", headers={"origin": base})
        assert r.status_code == 200, r.text
        seen = r.json()
        assert seen["path"] == f"/devenv/{sid}/port/8888/api/contents/a%20b" and seen["query"] == "type=notebook"
        assert seen["headers"]["authorization"] == f"token {token}"
        assert seen["headers"]["cookie"] == "theme=dark"
        assert seen["headers"]["host"] == f"127.0.0.1:{up_port}"
        assert seen["headers"]["origin"] == f"http://127.0.0.1:{up_port}"
        assert seen["headers"]["x-forwarded-prefix"] == f"/devenv/{sid}/port/8888"
        assert asked[-1] == 8888
        cookies = r.headers.get_list("set-cookie")
        assert any(x.startswith("app=1") and f"Path=/devenv/{sid}/port/8888" in x for x in cookies)
        assert not any(x.startswith("slotserve_session") for x in cookies)

        r = await c.post(prefix + "upload", content=b"x" * 300_000)
        assert r.json()["body_size"] == 300_000 and r.json()["method"] == "POST"
        r = await c.get(prefix + "stream")
        assert r.text == "chunk0;chunk1;chunk2;"
        r = await c.get(prefix + "redirect")
        assert r.status_code == 302 and r.headers["location"] == f"/devenv/{sid}/port/8888/elsewhere?x=1"
        r = await c.get(prefix + "absolute")
        assert r.headers["location"] == f"/devenv/{sid}/port/8888/abs"

        # a port the template does not declare: a plain app that gets the prefix stripped and no token
        other = f"/devenv/{sid}/port/6006/"
        seen = (await c.get(other + "static/x.js")).json()
        assert seen["path"] == "/static/x.js" and "authorization" not in seen["headers"] and asked[-1] == 6006
        r = await c.get(other + "redirect")
        assert r.headers["location"] == f"/devenv/{sid}/port/6006/elsewhere?x=1"
        assert (await c.get(other)).json()["path"] == "/"

    # WebSocket: the subprotocol is negotiated with the app, messages go both ways, the close code comes back
    ws_url = f"ws://127.0.0.1:{ss_port}{prefix}api/kernels/1/channels?session_id=s1"
    async with websockets.connect(
        ws_url,
        additional_headers={"cookie": f"slotserve_session={cookie}"},
        origin=base,
        subprotocols=["v1.kernel.websocket.jupyter.org"],
    ) as ws:
        assert ws.subprotocol == "v1.kernel.websocket.jupyter.org"
        hello = json.loads(await ws.recv())
        assert hello["path"] == f"/devenv/{sid}/port/8888/api/kernels/1/channels" and hello["auth"] == f"token {token}"
        assert hello["origin"] == f"http://127.0.0.1:{up_port}"
        await ws.send("hi")
        assert await ws.recv() == "echo:hi"
        await ws.send(b"\x00\x01")
        assert await ws.recv() == b"echo:\x00\x01"
        await ws.send("bye")
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4000

    # without signing in the handshake is refused
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(ws_url):
            pass

    # an undeclared port over WebSocket: stripped, no token
    async with websockets.connect(
        f"ws://127.0.0.1:{ss_port}/devenv/{sid}/port/6006/",
        additional_headers={"cookie": f"slotserve_session={cookie}"},
    ) as ws:
        hello = json.loads(await ws.recv())
        assert hello["path"] == "/" and hello["auth"] is None


async def test_proxy_head_and_other_methods(app, make_client, servers):
    up_port, ss_port = servers
    _admin, bob, session = await _running_web_session(app, make_client, {"web_ports": [{"port": 7860}]})
    _upstream_at(app, up_port)
    url = session["web"][0]["url"]
    cookie = bob.cookies.get("slotserve_session")
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{ss_port}", headers={"cookie": f"slotserve_session={cookie}"}
    ) as c:
        r = await c.request("HEAD", url + "x")
        assert r.status_code == 200
        r = await c.delete(url + "x")
        assert r.json()["method"] == "DELETE" and r.json()["path"] == "/x"


async def test_kept_environment_keeps_its_network_and_web_paths(app, make_client):
    """A kept environment's container is created once with a session's base path and token: its network stays while
    it is paused, and the address it was created with keeps working in later bookings."""
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    app.state.devenv.in_container = False
    body = {
        "name": "Lab",
        "image": "me/lab:1",
        "command": "lab --base={base_path}",
        "web_ports": [{"port": 8888, "kind": "jupyter"}],
    }
    t = (await bob.post("/api/devenv/templates", json=body)).json()
    docker.images.add("me/lab:1")

    async def book(**extra):
        now = utcnow()
        times = {"start_at": now.isoformat(), "end_at": (now + timedelta(hours=1)).isoformat()}
        r = await bob.post("/api/reservations", json={**times, **extra})
        assert r.status_code == 201, r.text
        return r.json()[0]

    first = await book(devenv_template_id=t["id"], devenv_project_name="Lab")
    await _ticks(app)
    [s1] = (await bob.get("/api/devenv/sessions")).json()
    url = s1["web"][0]["url"]
    assert s1["state"] == "running" and url == f"/devenv/{s1['id']}/port/8888/"
    [project] = (await bob.get("/api/devenv/projects")).json()
    [container] = docker.containers.values()
    [net] = docker.networks.values()
    assert net["Name"] == project_network_name(project["id"]) == container["Config"]["HostConfig"]["NetworkMode"]
    _row, web1 = await _web(s1["id"])

    await bob.delete(f"/api/reservations/{first['id']}")
    await _ticks(app, 2)
    assert container["Id"] in docker.containers and not container["Running"] and net["Id"] in docker.networks

    second = await book(devenv_project_id=project["id"])
    await _ticks(app)
    s2 = next(s for s in (await bob.get("/api/devenv/sessions")).json() if s["state"] == "running")
    assert s2["id"] != s1["id"] and s2["web"][0]["url"] == url and container["Running"]
    _row, web2 = await _web(s2["id"])
    assert web2["token"] == web1["token"] and web2["base_session"] == s1["id"] and web2["published"]["8888"]
    # the address of the booking that created the container reaches the running one, for its owner only
    asked = _upstream_at(app, 9)
    assert (await bob.get(url + "api/x")).status_code == 502 and asked == [8888]
    assert (await (await make_client("eve")).get(url + "api/x")).status_code == 404

    await bob.delete(f"/api/reservations/{second['id']}")
    await _ticks(app, 2)
    assert (await bob.delete(f"/api/devenv/projects/{project['id']}")).status_code == 204
    assert not docker.containers and not docker.networks
