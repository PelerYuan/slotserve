"""Multiple machines: node agents connected over a real WebSocket to the control plane (uvicorn, random port).

Every agent runs in this process with its own models and data directories and starts the mock model server as
a subprocess, exactly like a single-machine SlotServe does; the gateway reaches it over HTTP.
"""

import asyncio
import contextlib
import socket
import sys
from datetime import timedelta

import pytest
import uvicorn

from conftest import MOCK_SERVER
from slotserve.agent.node import NodeAgent
from slotserve.cluster import protocol
from slotserve.config import Settings
from slotserve.db import utcnow
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

    async def start(token: str, name: str = "b", wait: bool = True, protocol_version: int | None = None) -> NodeAgent:
        root = tmp_path / f"node-{name}"
        settings = Settings(
            _env_file=None,
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
