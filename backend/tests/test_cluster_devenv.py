# ruff: noqa: F811  (the cluster fixtures are imported from test_cluster)
"""Dev environments on another machine: its Docker is driven through the relay (the agent proxies the Docker API),
SSH, direct ports and web interfaces are served by the control plane and relayed to the container.

The node's Docker is a second FakeDocker served over TCP; "containers" are stood in for by servers on the loopback
interface (the agent is told where a container port is).
"""

import asyncio
import json
from datetime import timedelta

import pytest
from docker_mock import FakeDocker
from fastapi import FastAPI
from sqlalchemy import select
from test_cluster import _add_node, _free_port, _serve, _stop, _until, live, start_agent  # noqa: F401
from test_v09_devenv import _setup, ed25519_key

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevProject, DevSession, UsageSample
from slotserve.services import storage as storage_svc


@pytest.fixture
async def node_docker():
    fake = FakeDocker()
    fake.ncpu = 16
    port = _free_port()
    server, task = await _serve(fake.app, port)
    fake.host = f"tcp://127.0.0.1:{port}"
    yield fake
    await _stop(server, task)


async def _echo(greeting: bytes):
    async def handle(reader, writer):
        writer.write(greeting)
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _is(value: bool) -> bool:
    return value


async def _remote(app, make_client, start_agent, node_docker, ports: dict[int, int]):
    """Dev environments enabled; a node whose Docker is ``node_docker`` with lane "B" on its GPU 0. ``ports``:
    container port -> local port the agent connects to instead (stand-ins for the container's services)."""
    admin, bob = await _setup(app, make_client)
    settings = app.state.settings
    settings.devenv_relay_host = "127.0.0.1"
    first = _free_port()
    settings.devenv_ssh_ports = f"{first}-{first}"
    public = _free_port()
    settings.devenv_public_ports = f"{public}-{public}"
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await start_agent(created["token"], docker_host=node_docker.host)

    async def resolve(target: dict):
        return "127.0.0.1", ports[int(target["port"])]

    agent.relay.resolve_container = resolve
    r = await admin.post("/api/admin/lanes", json={"name": "B", "gpus": [0], "node_id": node_id})
    assert r.status_code == 201, r.text
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    return admin, bob, agent, node_id, r.json()


async def _book(client, lane_id: int, hours: float = 1, **devenv):
    start = utcnow()
    r = await client.post(
        "/api/reservations",
        json={
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=hours)).isoformat(),
            "lane_id": lane_id,
            **devenv,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()[0]


async def _ssh_banner(port: int) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
    try:
        banner = await asyncio.wait_for(reader.readline(), 10)
        writer.write(b"ping\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.readline(), 10) == b"ping\n"
        return banner
    finally:
        writer.close()


async def _session(client, **match):
    sessions = (await client.get("/api/devenv/sessions")).json()
    return next((s for s in sessions if all(s.get(k) == v for k, v in match.items())), None)


async def _until_ticks(app, predicate, what: str, n: int = 60):
    for _ in range(n):
        await app.state.devenv.tick()
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} never happened")


async def test_remote_booking_ssh_web_and_direct_ports(app, make_client, start_agent, node_docker):
    ssh, ssh_port = await _echo(b"SSH-2.0-dropbear\r\n")
    direct, direct_port = await _echo(b"hello from 9000\n")
    web = FastAPI()

    @web.get("/{path:path}")
    async def page(path: str):
        return {"app": "jupyter", "path": path}

    web_port = _free_port()
    web_server, web_task = await _serve(web, web_port)
    try:
        _admin, bob, _agent, node_id, lane = await _remote(
            app, make_client, start_agent, node_docker, {22: ssh_port, 8888: web_port, 9000: direct_port}
        )
        local = app.state.fake_docker
        jupyter = next(
            t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "jupyter-pytorch"
        )
        booking = await _book(bob, lane["id"], devenv_template_id=jupyter["id"])

        # The image is pulled on the node (with progress), the container created there with the node's GPU.
        await _until_ticks(app, lambda: _session(bob, state="running"), "remote environment running")
        assert node_docker.pulls == [jupyter["image"]] and local.pulls == [] and not local.containers
        [container] = node_docker.containers.values()
        config = container["Config"]
        assert config["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] == ["0"]
        assert config["Labels"]["slotserve.node"] == str(node_id)
        # nothing is published beyond the node's loopback interface; SSH files were uploaded through the relay
        bindings = config["HostConfig"]["PortBindings"]
        assert bindings["22/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": ""}]
        assert all(b[0]["HostIp"] == "127.0.0.1" for b in bindings.values())
        files = node_docker.archives[container["Id"]]
        assert "ssh-ed25519" in files["opt/slotserve/ssh/authorized_keys"].decode()
        assert files["opt/slotserve/dropbearmulti"] == b"\x7fELFfake"
        assert any(
            n["Name"] == f"slotserve-devenv-{booking['id']}" or n["Name"].startswith("slotserve-devenv-")
            for n in node_docker.networks.values()
        )

        # SSH: the control plane listens on its own SSH port and relays to the container's port 22.
        session = await _session(bob, state="running")
        assert session["node_id"] == node_id and session["node_name"] == "gpu-b"
        ssh_listen = int(app.state.settings.devenv_ssh_ports.split("-")[0])
        assert session["ssh_port"] == ssh_listen and session["ssh_command"].startswith(f"ssh -p {ssh_listen} root@")
        assert await _ssh_banner(ssh_listen) == b"SSH-2.0-dropbear\r\n"

        # Web interface: the proxy reaches it through a relayed loopback port.
        r = await bob.get(f"/devenv/{session['id']}/port/8888/lab/tree")
        assert r.status_code == 200, r.text
        assert r.json()["app"] == "jupyter" and "lab/tree" in r.json()["path"]
        executor = app.state.nodes.get(node_id)
        assert executor.relay_stats()["opened"] >= 3

        # A direct port: the control plane listens on a public port, no forwarder container anywhere.
        r = await bob.put(f"/api/devenv/sessions/{session['id']}/ports/9000", json={"mode": "direct", "name": "api"})
        assert r.status_code == 200, r.text
        public = next(p for p in r.json()["ports"] if p["port"] == 9000)["host_port"]
        await app.state.devenv.tick()
        ports = (await bob.get(f"/api/devenv/sessions/{session['id']}/ports")).json()["ports"]
        assert next(p for p in ports if p["port"] == 9000)["status"] == "active"
        assert not node_docker.forwarders() and not local.forwarders()
        reader, writer = await asyncio.open_connection("127.0.0.1", public)
        assert await asyncio.wait_for(reader.readline(), 10) == b"hello from 9000\n"
        writer.close()

        # The node goes away: nothing fails, the environment waits for it.
        await app.state.cluster.disconnect(node_id, 1001, "test")
        await _until(lambda: _is(not executor.online), what="offline")
        await app.state.devenv.tick()
        assert (await _session(bob, id=session["id"]))["state"] == "running"
        await _until(lambda: _is(executor.online), what="reconnect")

        # The booking ends: the container goes away on the node, and so do the listeners.
        assert (await bob.delete(f"/api/reservations/{booking['id']}")).status_code in (200, 204)
        await _until_ticks(app, lambda: _is(not node_docker.containers), "container removed")
        assert (await _session(bob, id=session["id"]))["state"] == "stopped"
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", ssh_listen), 5)
        assert not app.state.devenv.relays.ssh and not app.state.devenv.relays.direct
    finally:
        ssh.close()
        direct.close()
        await _stop(web_server, web_task)


async def test_remote_project_pause_resume_and_move(app, make_client, start_agent, node_docker):
    ssh, ssh_port = await _echo(b"SSH-2.0-dropbear\r\n")
    try:
        admin, bob, _agent, node_id, lane = await _remote(app, make_client, start_agent, node_docker, {22: ssh_port})
        local = app.state.fake_docker
        torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
        project = (await bob.post("/api/devenv/projects", json={"name": "Thesis", "template_id": torch["id"]})).json()
        booking = await _book(bob, lane["id"], devenv_project_id=project["id"])
        await _until_ticks(app, lambda: _session(bob, state="running"), "project running on the node")
        [container] = node_docker.containers.values()
        async with SessionLocal() as db:
            row = await db.get(DevProject, project["id"])
            assert row.container_node_id == node_id and row.container_id == container["Id"]

        # Pause: the booking ends, the container is stopped and kept on the node.
        await bob.delete(f"/api/reservations/{booking['id']}")
        await _until_ticks(app, lambda: _is(not container["Running"]), "paused")
        assert container["Id"] in node_docker.containers
        assert (await bob.get("/api/devenv/projects")).json()[0]["state"] == "paused"

        # Resume: the same container starts again on the node; SSH is relayed again.
        created = len(node_docker.created)
        booking = await _book(bob, lane["id"], devenv_project_id=project["id"])
        await _until_ticks(app, lambda: _is(container["Running"]), "resumed")
        assert len(node_docker.created) == created
        session = await _session(bob, state="running")
        assert await _ssh_banner(session["ssh_port"]) == b"SSH-2.0-dropbear\r\n"

        # The lane's GPUs change: the container is snapshotted and recreated (on the node).
        await bob.delete(f"/api/reservations/{booking['id']}")
        await _until_ticks(app, lambda: _is(not container["Running"]), "paused again")
        r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json={"name": "B", "gpus": [1]})
        assert r.status_code == 200, r.text
        booking = await _book(bob, lane["id"], devenv_project_id=project["id"])
        await _until_ticks(app, lambda: _is(len(node_docker.created) == created + 1), "recreated")
        assert node_docker.commits and node_docker.commits[0][1].startswith(f"slotserve/project-{project['id']}:")
        [new] = [c for c in node_docker.containers.values() if c["Running"]]
        assert new["Config"]["HostConfig"]["DeviceRequests"][0]["DeviceIDs"] == ["1"]
        assert new["Config"]["Image"] == node_docker.commits[0][1]
        await bob.delete(f"/api/reservations/{booking['id']}")
        await _until_ticks(app, lambda: _is(not new["Running"]), "paused on the node")

        # Booked on the server's own lane: its /workspace and kept folders are Docker volumes on the node, so it
        # cannot move (it would start empty) ...
        local_lane = next(x for x in (await admin.get("/api/lanes")).json() if x["name"] != "B")
        start = utcnow()
        window = {"start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()}
        r = await bob.post(
            "/api/reservations", json={**window, "lane_id": local_lane["id"], "devenv_project_id": project["id"]}
        )
        assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_project_data_elsewhere"
        assert r.json()["detail"]["params"]["node"] == "gpu-b"
        r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"], "node_id": None})
        assert r.status_code == 201  # without a machine named: it stays where it is
        await _until_ticks(app, lambda: _session(bob, state="running", kind="cpu"), "CPU-only on the node")
        cpu_session = await _session(bob, state="running", kind="cpu")
        assert cpu_session["node_id"] == node_id
        assert (await bob.post(f"/api/devenv/cpu/sessions/{cpu_session['id']}/stop")).status_code == 202
        local_id = app.state.nodes.local.node_id
        r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"], "node_id": local_id})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_project_data_elsewhere"

        # ... until its data is on storage both machines use (here NFS): then it moves, data and all.
        for name, purpose in (("homes", "personal"), ("kept", "projects")):
            r = await admin.post(
                "/api/admin/storage",
                json={"name": name, "kind": "nfs", "purpose": purpose, "server": "nas.lan", "remote_path": f"/{name}"},
            )
            assert r.status_code == 201, r.text
        await _book(bob, local_lane["id"], devenv_project_id=project["id"])
        await _until_ticks(app, lambda: _session(bob, state="running"), "running on the server", n=80)
        [moved] = local.containers.values()
        snapshot = node_docker.commits[-1][1]
        assert moved["Config"]["Image"] == snapshot and local.loads and local.loads[0][0] == snapshot
        assert local.loads[0][1] > 300_000  # the whole archive came through the relay
        assert not node_docker.containers  # the old container is gone from the node
        async with SessionLocal() as db:
            row = await db.get(DevProject, project["id"])
            assert row.container_node_id is None and row.container_id == moved["Id"]
    finally:
        ssh.close()


async def test_remote_cpu_environment_and_usage(app, make_client, start_agent, node_docker):
    ssh, ssh_port = await _echo(b"SSH-2.0-dropbear\r\n")
    try:
        _admin, bob, _agent, node_id, _lane = await _remote(app, make_client, start_agent, node_docker, {22: ssh_port})
        torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
        # the machine picker offers the node for environments without a GPU (CpuEnvs.runs_on)
        machines = (await bob.get("/api/machines")).json()
        assert next(m for m in machines if m["name"] == "gpu-b")["capacity"]["cpu_envs"] is True
        r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"], "node_id": 999})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "node_invalid"
        r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"], "node_id": node_id})
        assert r.status_code == 201, r.text
        started = r.json()
        assert started["node_id"] == node_id
        await _until_ticks(app, lambda: _session(bob, state="running", kind="cpu"), "CPU environment running")
        [container] = node_docker.containers.values()
        host = container["Config"]["HostConfig"]
        # the node's 16 cores (its own Docker) minus the 8 its lane reserves; no GPU
        assert host["CpusetCpus"] == "8-15" and "DeviceRequests" not in host
        assert "NVIDIA_VISIBLE_DEVICES=void" in container["Config"]["Env"]

        # Usage: docker stats of the node, through the relay, into the minute rows.
        node_docker.cpu_ns[container["Id"]] = 0
        node_docker.mem_used[container["Id"]] = 2**30
        await app.state.devenv.cpu.poll()
        node_docker.cpu_ns[container["Id"]] += 30 * 10**9
        await app.state.devenv.cpu.poll()
        cpu = (await bob.get("/api/devenv/cpu")).json()
        [live] = cpu["sessions"]
        assert live["node_name"] == "gpu-b" and live["live"]["memory"] == 2**30 and live["live"]["cpu"] > 0
        when = (utcnow() + timedelta(minutes=5)).replace(second=0, microsecond=0)  # not one the sampler wrote
        await app.state.usage_sampler.sample(when)
        async with SessionLocal() as db:
            rows = (await db.execute(select(UsageSample).where(UsageSample.resource == "cpu"))).scalars().all()
        row = next(r for r in rows if r.start == when)
        assert row.kind == "cpu_devenv" and row.cpu_core_seconds >= 30

        assert (await bob.post(f"/api/devenv/cpu/sessions/{started['id']}/stop")).status_code == 202
        assert not node_docker.containers
        async with SessionLocal() as db:
            assert (await db.get(DevSession, started["id"])).state == "stopped"
    finally:
        ssh.close()


async def test_shared_idle_environment_on_a_node_and_storage_reach(app, make_client, start_agent, node_docker):
    ssh, ssh_port = await _echo(b"SSH-2.0-dropbear\r\n")
    try:
        admin, bob, _agent, node_id, lane = await _remote(app, make_client, start_agent, node_docker, {22: ssh_port})
        torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
        # A remote lane can host the shared environment of an idle lane now.
        r = await admin.patch(
            f"/api/admin/lanes/{lane['id']}",
            json={"name": "B", "gpus": [0], "idle_mode": "devenv", "idle_template_id": torch["id"]},
        )
        assert r.status_code == 200, r.text
        await _until_ticks(app, lambda: _session(bob, kind="shared", state="running"), "shared environment")
        [container] = node_docker.containers.values()
        assert container["Config"]["Labels"]["slotserve.kind"] == "shared"
        assert "opt/slotserve/accounts" in node_docker.archives[container["Id"]]

        # Storage: a host directory is the server's only, unless it is listed for the node; NFS goes anywhere.
        r = await admin.post(
            "/api/admin/storage",
            json={"name": "data", "kind": "host", "purpose": "shared", "host_path": "/srv/data", "mount_path": "/data"},
        )
        assert r.status_code == 201, r.text
        host_storage = r.json()
        r = await admin.post(
            "/api/admin/storage",
            json={
                "name": "nas",
                "kind": "nfs",
                "purpose": "shared",
                "server": "nas.lan",
                "remote_path": "/export",
                "mount_path": "/nas",
            },
        )
        assert r.status_code == 201, r.text
        async with SessionLocal() as db:
            on_node = await storage_svc.plan_for(db, "k", None, shared=True, node_id=node_id)
            on_server = await storage_svc.plan_for(db, "k", None, shared=True)
        assert [m["Target"] for m in on_node.mounts] == ["/nas"]
        assert sorted(m["Target"] for m in on_server.mounts) == ["/data", "/nas"]
        body = {**host_storage, "node_ids": [node_id]}
        body = {k: body[k] for k in ("name", "kind", "purpose", "host_path", "mount_path", "node_ids")}
        r = await admin.put(f"/api/admin/storage/{host_storage['id']}", json=body)
        assert r.status_code == 200 and r.json()["node_ids"] == [node_id], r.text
        r = await admin.put(f"/api/admin/storage/{host_storage['id']}", json={**body, "node_ids": [12345]})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "storage_nodes_invalid"
        async with SessionLocal() as db:
            on_node = await storage_svc.plan_for(db, "k", None, shared=True, node_id=node_id)
        assert sorted(m["Target"] for m in on_node.mounts) == ["/data", "/nas"]
        assert json.dumps(on_node.mounts)  # plain data
    finally:
        ssh.close()
