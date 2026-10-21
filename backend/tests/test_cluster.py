"""Multiple machines: node agents connected over a real WebSocket to the control plane (uvicorn, random port).

Every agent runs in this process with its own models and data directories and starts the mock model server as
a subprocess, exactly like a single-machine SlotServe does; the gateway reaches it over HTTP.
"""

import asyncio
import contextlib
import json
import socket
import sys
from datetime import timedelta

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from sqlalchemy import select

from conftest import MOCK_SERVER, make_gguf
from slotserve.agent.node import NodeAgent
from slotserve.api import runtime_admin
from slotserve.cluster import protocol
from slotserve.config import Settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import ModelLoadEvent, UsageSample
from slotserve.schemas import GpuOut


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _gpus() -> list[GpuOut]:
    return [
        GpuOut(
            index=i,
            name="Node GPU",
            memory_used_mb=1000 + i,
            memory_total_mb=24_000,
            utilization_pct=70,
            temperature_c=50,
            power_w=200.0,
        )
        for i in range(2)
    ]


async def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    return server, task


async def _stop(server, task) -> None:
    server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


@pytest.fixture
async def live(app):
    """The control plane on a real port, so agents connect over a real WebSocket."""
    port = _free_port()
    server, task = await _serve(app, port)
    yield f"http://127.0.0.1:{port}"
    await _stop(server, task)


@pytest.fixture
async def start_agent(app, live, tmp_path):
    agents: list[tuple[NodeAgent, asyncio.Task]] = []

    async def start(
        token: str,
        name: str = "b",
        wait: bool = True,
        protocol_version: int | None = None,
        docker_host: str = "unix:///nonexistent/docker.sock",
    ) -> NodeAgent:
        root = tmp_path / f"node-{name}"
        settings = Settings(
            _env_file=None,
            docker_host=docker_host,
            data_dir=root / "data",
            models_dir=root / "models",
            llama_server_cmd=f'"{sys.executable}" "{MOCK_SERVER}"',
            llama_port=_free_port(),
            agent_model_host="127.0.0.1",
            stop_timeout_seconds=5,
            load_timeout_seconds=60,
        )
        (root / "models").mkdir(parents=True, exist_ok=True)
        agent = NodeAgent(settings, server=live, token=token, stats=_gpus)
        if protocol_version is not None:
            agent.protocol = protocol_version
        task = asyncio.create_task(agent.run())
        agents.append((agent, task))
        if wait:
            await asyncio.wait_for(agent.connected.wait(), 15)
        return agent

    yield start
    for agent, task in agents:
        await agent.close()  # stops every model server it started
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _until(predicate, timeout: float = 25.0, what: str = "condition"):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(f"{what} never happened (last: {last!r})")


async def _add_node(admin, name: str = "gpu-b") -> dict:
    r = await admin.post("/api/admin/nodes", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def _node(admin, node_id: int) -> dict:
    return next(n for n in (await admin.get("/api/admin/nodes")).json() if n["id"] == node_id)


async def _lane(admin, name: str) -> dict:
    return next(lane for lane in (await admin.get("/api/status")).json()["lanes"] if lane["name"] == name)


def _events(app) -> list[tuple[str, dict]]:
    """Captures notifications the app emits from now on."""
    seen: list[tuple[str, dict]] = []
    original = app.state.notifier.emit

    def emit(event, **data):
        seen.append((event, data))
        original(event, **data)

    app.state.notifier.emit = emit
    return seen


async def _remote_setup(app, make_client, start_agent, model_file: str = "remote.gguf"):
    """A node with one lane on its GPU 0 and a model that only that node has."""
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await start_agent(created["token"])
    make_gguf(agent.settings.models_dir, model_file)
    r = await admin.post("/api/admin/lanes", json={"name": "B", "gpus": [0], "node_id": node_id})
    assert r.status_code == 201, r.text
    lane = r.json()
    scan = (await admin.post("/api/models/scan")).json()
    assert model_file[:-5] in scan["added"]
    models = {m["name"]: m for m in (await admin.get("/api/models")).json()}
    return admin, agent, node_id, lane, models


# ---- registration, heartbeat, revocation --------------------------------------------


async def test_register_inventory_metrics_and_revoke(app, make_client, start_agent):
    admin = await make_client("root")
    member = await make_client("bob")
    assert (await member.get("/api/admin/nodes")).status_code == 403
    [local] = (await admin.get("/api/admin/nodes")).json()
    assert local["is_local"] and local["status"] == "online" and local["lanes"]

    created = await _add_node(admin)
    token = created["token"]
    assert token.startswith(protocol.TOKEN_PREFIX)
    assert "slotserve agent" in created["docker_command"] and token in created["docker_command"]
    assert created["node"]["status"] == "never" and created["node"]["token_prefix"] == token[:8]
    assert (await admin.post("/api/admin/nodes", json={"name": "GPU-B"})).status_code == 409

    agent = await start_agent(token)
    node = await _until(
        lambda: _node_when(admin, created["node"]["id"], lambda n: n["status"] == "online" and n["gpus"]),
        what="node online with metrics",
    )
    assert node["cpu_cores"] and node["hostname"] and node["agent_version"]
    assert node["protocol_version"] == protocol.PROTOCOL_VERSION
    assert [g["index"] for g in node["gpus"]] == [0, 1] and node["gpus"][0]["utilization_pct"] == 70
    assert node["effective_address"] == "127.0.0.1" and node["models_dir"]
    assert agent.node_name == "gpu-b"

    # The token is stored as a hash only and a wrong one is refused.
    intruder = await start_agent("ssn-wrong", name="x", wait=False)
    await _until(lambda: _is(intruder.status == "rejected"), what="wrong token rejected")
    assert intruder.fatal_code == protocol.CLOSE_INVALID_TOKEN

    r = await admin.post(f"/api/admin/nodes/{created['node']['id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    await _until(lambda: _is(agent.status == "rejected"), what="agent told it was revoked")
    assert agent.fatal_code == protocol.CLOSE_REVOKED
    assert (await _node(admin, created["node"]["id"]))["status"] == "revoked"

    # A new token re-admits the machine.
    rotated = (await admin.post(f"/api/admin/nodes/{created['node']['id']}/token")).json()
    assert rotated["token"] != token
    again = await start_agent(rotated["token"], name="b2")
    assert again.node_id == created["node"]["id"]
    await admin.post(f"/api/admin/nodes/{created['node']['id']}/revoke")
    await _until(lambda: _is(again.status == "rejected"), what="second agent revoked")
    assert (await admin.delete(f"/api/admin/nodes/{created['node']['id']}")).status_code == 204
    assert len((await admin.get("/api/admin/nodes")).json()) == 1


async def _is(value: bool) -> bool:
    return value


async def _node_when(admin, node_id: int, predicate):
    node = await _node(admin, node_id)
    return node if predicate(node) else None


async def test_protocol_version_mismatch_is_reported(app, make_client, start_agent):
    admin = await make_client("root")
    created = await _add_node(admin)
    agent = await start_agent(created["token"], wait=False, protocol_version=protocol.PROTOCOL_VERSION + 1)
    await _until(lambda: _is(agent.status == "rejected"), what="mismatch rejected")
    assert agent.fatal_code == protocol.CLOSE_PROTOCOL
    assert "protocol version mismatch" in agent.last_error
    node = await _node(admin, created["node"]["id"])
    assert node["status"] == "mismatch" and "protocol version mismatch" in node["last_error"]
    assert node["protocol_version"] == protocol.PROTOCOL_VERSION + 1


async def test_heartbeat_offline_detection_and_notifications(app, make_client, start_agent):
    app.state.settings.node_offline_seconds = 1.0
    app.state.settings.node_heartbeat_seconds = 0.2  # sent to the agent in the welcome
    seen = _events(app)
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await start_agent(created["token"])
    lane = (await admin.post("/api/admin/lanes", json={"name": "B", "gpus": [0], "node_id": node_id})).json()
    bob = await make_client("bob")
    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "model_id": None,  # the whole GPU
            "lane_id": lane["id"],
            "start_at": (now + timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=2)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text

    # The agent goes silent (no heartbeats) without closing its socket: the watchdog notices.
    agent.retry_seconds = 3.0  # stays away long enough to be reported
    executor = app.state.nodes.get(node_id)
    executor.last_seen -= 60
    await app.state.cluster.check()
    assert not executor.online
    status = await _lane(admin, "B")
    assert status["node_online"] is False and status["runtime"]["state"] == "offline"
    assert (await _node(admin, node_id))["status"] == "offline"

    await asyncio.sleep(1.1)
    await app.state.cluster.check()
    assert ("node_offline", {"node": "gpu-b"}) in seen
    flagged = [d for e, d in seen if e == "booking_node_offline"]
    assert flagged and flagged[0]["reservation_id"] == r.json()[0]["id"] and flagged[0]["node"] == "gpu-b"

    # The agent reconnects by itself; admins hear that it is back.
    await _until(lambda: _is(executor.online), what="reconnect")
    assert ("node_online", {"node": "gpu-b"}) in seen
    assert (await _lane(admin, "B"))["node_online"] is True


# ---- remote model servers end to end ----------------------------------------------------


async def test_remote_model_server_end_to_end(app, make_client, start_agent, monkeypatch):
    monkeypatch.setattr(runtime_admin, "STREAM_SECONDS", 1.5)
    monkeypatch.setattr(runtime_admin, "POLL_SECONDS", 0.1)
    monkeypatch.setenv("MOCK_LOAD_SECONDS", "2")
    admin, agent, node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    remote = models["remote"]
    assert remote["node_ids"] == [node_id] and remote["file_exists"]
    assert lane["node_id"] == node_id and lane["node_name"] == "gpu-b"

    r = await admin.post(f"/api/models/{remote['id']}/default", params={"lane_id": lane["id"]})
    assert r.status_code == 200, r.text

    # Loading progress comes from the node's log.
    progress = set()

    async def ready():
        status = await _lane(admin, "B")
        rt = status["runtime"]
        if rt["state"] == "loading" and rt["log_percent"] is not None:
            progress.add(rt["log_percent"])
        return status if rt["state"] == "ready" else None

    status = await _until(ready, what="remote model ready")
    assert status["runtime"]["model_name"] == "remote"
    assert progress, "no loading progress seen"

    runtime = next(x for x in (await admin.get("/api/admin/runtime")).json() if x["lane_id"] == lane["id"])
    assert runtime["node_name"] == "gpu-b" and runtime["node_id"] == node_id
    assert runtime["base_url"] == f"http://127.0.0.1:{agent.settings.llama_port + lane['id'] - 1}"
    assert "remote.gguf" in runtime["command"] and isinstance(runtime["pid"], int)
    logs = (await admin.get("/api/runtime/logs", params={"lane_id": lane["id"]})).json()["lines"]
    assert any("--alias remote" in line for line in logs)
    download = await admin.get(f"/api/admin/runtime/{lane['id']}/log")
    assert download.status_code == 200 and "server is listening" in download.text
    stream = await admin.get(f"/api/admin/runtime/{lane['id']}/stream")
    lines = [
        line for raw in stream.text.splitlines() if raw.startswith("data: ") for line in json.loads(raw[6:])["lines"]
    ]
    assert any("start:" in line for line in lines) and any("server is listening" in line for line in lines)
    async with SessionLocal() as db:
        events = (await db.execute(select(ModelLoadEvent).where(ModelLoadEvent.lane_id == lane["id"]))).scalars()
        assert any(e.success for e in events)

    # The gateway forwards to the node directly.
    key = {"Authorization": f"Bearer {(await admin.post('/api/keys', json={'name': 'k'})).json()['key']}"}
    r = await admin.post(
        "/v1/chat/completions", headers=key, json={"model": "remote", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"].startswith("[remote]")
    assert [m["id"] for m in (await admin.get("/v1/models", headers=key)).json()["data"]] == ["remote"]

    # Usage samples of the remote lane carry the node's GPU metrics.
    when = (utcnow() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    await app.state.usage_sampler.sample(when)
    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(UsageSample).where(UsageSample.lane_id == lane["id"], UsageSample.start == when)))
            .scalars()
            .all()
        )
    assert [(r.gpu_index, r.kind, r.model_name) for r in rows] == [(0, "idle_model", "remote")]
    assert rows[0].util_seconds == 70 * 60 and rows[0].mem_peak_mb == 1000

    # Unloading stops the server on the node.
    await admin.delete(f"/api/models/{remote['id']}/default", params={"lane_id": lane["id"]})
    await _until(
        lambda: _is_state(admin, "B", "stopped"),
        what="remote model stopped",
    )
    assert agent.runtimes[lane["id"]].pid is None


async def _is_state(admin, lane_name: str, state: str):
    return (await _lane(admin, lane_name))["runtime"]["state"] == state


async def test_failed_remote_load_reports_details(app, make_client, start_agent):
    admin, _agent, _node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    remote = models["remote"]
    await admin.patch(f"/api/models/{remote['id']}", json={"args": "--mock-fail"})
    await admin.post(f"/api/models/{remote['id']}/default", params={"lane_id": lane["id"]})
    await _until(lambda: _is_state(admin, "B", "failed"), what="remote load failed")
    runtime = next(x for x in (await admin.get("/api/admin/runtime")).json() if x["lane_id"] == lane["id"])
    assert any("simulated load failure" in line for line in runtime["failure_log"])
    assert runtime["exit_code"] not in (None, 0) and runtime["next_retry_at"] is not None
    failed = (await admin.get("/api/admin/runtime/events", params={"failed": "true"})).json()
    assert failed and "simulated load failure" in failed[0]["log_excerpt"] and failed[0]["lane_id"] == lane["id"]


# ---- scheduling with per-node models -------------------------------------------------------


async def test_models_are_per_node(app, make_client, start_agent, models_dir):
    admin, agent, node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    make_gguf(models_dir, "local.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m for m in (await admin.get("/api/models")).json()}
    local_id = app.state.nodes.local.node_id
    assert models["local"]["node_ids"] == [local_id] and models["remote"]["node_ids"] == [node_id]
    first = next(x for x in (await admin.get("/api/lanes")).json() if x["name"] != "B")
    assert first["node_id"] == local_id

    # A lane only serves (and can only be booked with) models its machine has.
    r = await admin.post(f"/api/models/{models['local']['id']}/default", params={"lane_id": lane["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_on_node"
    r = await admin.post(f"/api/models/{models['remote']['id']}/default", params={"lane_id": first["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_on_node"
    now = utcnow()
    start, end = (now + timedelta(hours=1)).isoformat(), (now + timedelta(hours=2)).isoformat()
    booking = {"start_at": start, "end_at": end}
    r = await admin.post(
        "/api/reservations", json={**booking, "model_id": models["local"]["id"], "lane_id": lane["id"]}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_on_node"
    r = await admin.post(
        "/api/reservations", json={**booking, "model_id": models["remote"]["id"], "lane_id": lane["id"]}
    )
    assert r.status_code == 201, r.text
    r = await admin.patch(
        f"/api/admin/lanes/{lane['id']}", json={"name": "B", "gpus": [1], "default_model_id": models["local"]["id"]}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "model_not_on_node"
    # GPUs are checked against the node's own GPUs, and only lanes of the same node may not overlap.
    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [5], "node_id": node_id})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "lane_gpus_invalid"
    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [0], "node_id": node_id})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "lane_gpus_overlap"
    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [1], "node_id": node_id})
    assert r.status_code == 201
    # Dev environments do not run on other machines yet.
    r = await admin.post(
        "/api/reservations", json={**booking, "devenv_template_id": 1, "lane_id": lane["id"], "model_id": None}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_remote_lane"

    # The same file on shared storage (same path everywhere) is one model, available on both machines.
    shared = make_gguf(agent.settings.models_dir, "shared.gguf")
    r = await admin.post("/api/models", json={"name": "shared", "path": str(shared)})
    assert r.status_code == 201 and r.json()["node_ids"] == [local_id]
    await admin.post("/api/models/scan", params={"node_id": node_id})
    shared_model = next(m for m in (await admin.get("/api/models")).json() if m["name"] == "shared")
    assert shared_model["node_ids"] == sorted([local_id, node_id])
    r = await admin.post(f"/api/models/{shared_model['id']}/default", params={"lane_id": lane["id"]})
    assert r.status_code == 200
    assert (await admin.post("/api/admin/lanes", json={"name": "D", "gpus": [], "node_id": 999})).status_code == 400


async def test_remote_download(app, make_client, start_agent):
    payload = b"GGUF" + b"\0" * 5000
    files = FastAPI()

    @files.get("/files/{name}")
    async def serve_file(name: str):
        return Response(payload, media_type="application/octet-stream")

    port = _free_port()
    server, task = await _serve(files, port)
    try:
        admin = await make_client("root")
        created = await _add_node(admin)
        node_id = created["node"]["id"]
        agent = await start_agent(created["token"])
        r = await admin.post(
            "/api/downloads", json={"url": f"http://127.0.0.1:{port}/files/fetched.gguf", "node_id": node_id}
        )
        assert r.status_code == 201, r.text

        async def done():
            tasks = (await admin.get("/api/downloads")).json()
            return tasks[0] if tasks[0]["status"] in ("done", "failed") else None

        result = await _until(done, what="remote download finished")
        assert result["status"] == "done", result
        assert result["node_id"] == node_id and result["downloaded_bytes"] == len(payload)
        assert (agent.settings.models_dir / "fetched.gguf").read_bytes() == payload
        assert not (app.state.settings.models_dir / "fetched.gguf").exists()
        model = next(m for m in (await admin.get("/api/models")).json() if m["id"] == result["model_id"])
        assert model["name"] == "fetched" and model["node_ids"] == [node_id]
    finally:
        await _stop(server, task)


async def test_server_only_features_on_remote_nodes(app, make_client, start_agent, monkeypatch):
    """Catalog deploys and model library downloads stay on the server; remote lanes reserve no server cores."""
    admin, _agent, node_id, lane, _models = await _remote_setup(app, make_client, start_agent)
    r = await admin.post(
        "/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "lane_id": lane["id"]}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "catalog_remote_lane"
    r = await admin.post(
        "/api/downloads", json={"url": "http://127.0.0.1:9/x.gguf", "node_id": node_id, "storage_id": 1}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "storage_remote_node"

    async def host_info(force: bool = False):
        return {"cpus": 32, "memory": 0}

    cpu = app.state.devenv.cpu
    monkeypatch.setattr(cpu, "host_info", host_info)
    async with SessionLocal() as db:
        layout = await cpu.layout(db)
    assert lane["id"] not in layout.lanes and len(layout.lanes) == 1


# ---- reconciliation ------------------------------------------------------------------------


async def test_reconnect_reconciles_running_servers(app, make_client, start_agent):
    admin, agent, node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    await admin.post(f"/api/models/{models['remote']['id']}/default", params={"lane_id": lane["id"]})
    await _until(lambda: _is_state(admin, "B", "ready"), what="remote model ready")
    pid = agent.runtimes[lane["id"]].pid

    # A second lane on the node, removed while the node is away: its server must not survive the reconnect.
    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [1], "node_id": node_id})
    lane_c = r.json()
    await admin.post(f"/api/models/{models['remote']['id']}/default", params={"lane_id": lane_c["id"]})
    await _until(lambda: _is_state(admin, "C", "ready"), what="second lane ready")
    orphan = agent.runtimes[lane_c["id"]]

    executor = app.state.nodes.get(node_id)
    await app.state.cluster.disconnect(node_id, 1001, "test")
    await _until(lambda: _is(not executor.online), what="disconnect")
    assert (await _lane(admin, "B"))["runtime"]["state"] == "offline"
    assert (await admin.delete(f"/api/admin/lanes/{lane_c['id']}")).status_code == 204
    assert orphan.state.value == "ready"  # nothing could be sent while the node was away

    await _until(lambda: _is(executor.online), what="reconnect")
    status = await _until(lambda: _lane_ready(admin, "B"), what="lane ready again")
    assert status["runtime"]["model_name"] == "remote"
    assert agent.runtimes[lane["id"]].pid == pid  # the running server was kept, not restarted
    await _until(lambda: _is(lane_c["id"] not in agent.runtimes), what="orphan stopped")
    assert orphan.state.value == "stopped"


async def _lane_ready(admin, name: str):
    lane = await _lane(admin, name)
    return lane if lane["runtime"]["state"] == "ready" else None


async def test_control_plane_restart_keeps_remote_servers(app, make_client, start_agent):
    """The control plane forgets everything (new lane manager); the agent's report restores the state."""
    admin, agent, node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    await admin.post(f"/api/models/{models['remote']['id']}/default", params={"lane_id": lane["id"]})
    await _until(lambda: _is_state(admin, "B", "ready"), what="remote model ready")
    pid = agent.runtimes[lane["id"]].pid

    executor = app.state.nodes.get(node_id)
    runtime = executor.runtimes[lane["id"]]
    runtime.apply({"lane_id": lane["id"], "state": "stopped"})  # as if freshly started
    await app.state.cluster.disconnect(node_id, 1001, "test")
    await _until(lambda: _is(not executor.online), what="disconnect")
    await _until(lambda: _is(executor.online), what="reconnect")
    assert runtime.state.value == "ready" and runtime.model_name == "remote"
    await asyncio.sleep(1.0)  # a few scheduler rounds: nothing is restarted
    assert agent.runtimes[lane["id"]].pid == pid
