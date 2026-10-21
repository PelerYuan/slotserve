"""Multiple machines, hardened: the agent link (TLS, pinning, token rotation, login limits), clocks, versions,
health, per-node download queues with resume, catalog deploys and model libraries on nodes, usage per node and
keeping model servers across agent restarts.

Agents run in this process against the control plane on a real port (see test_cluster.py); downloads come from
small HTTP servers (a fake Hugging Face hub for the catalog), GPUs are fake.
"""

import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from test_cluster import _add_node, _free_port, _gpus, _is, _lane, _node, _serve, _stop, _until, live  # noqa: F401
from test_v11_catalog import FakeHub

from conftest import MOCK_SERVER, make_gguf
from slotserve.agent import node as agent_node
from slotserve.agent.node import NodeAgent
from slotserve.cluster import protocol
from slotserve.config import Settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DownloadTask, ModelLoadEvent, NodeModel, UsageSample
from slotserve.runtime.adopted import pid_alive
from slotserve.services.docker_api import DockerClient

# ---- helpers ---------------------------------------------------------------------------


@pytest.fixture
async def agents(app, live, tmp_path):  # noqa: F811 - the fixture imported from test_cluster
    """Starts node agents; ``name`` picks the machine's directories (the same name = the same machine)."""
    started: list[tuple[NodeAgent, asyncio.Task]] = []
    ports: dict[str, int] = {}

    def make(token: str, name: str = "b", server: str | None = None, **overrides) -> NodeAgent:
        root = tmp_path / f"node-{name}"
        ports.setdefault(name, _free_port())
        settings = Settings(
            _env_file=None,
            data_dir=root / "data",
            models_dir=root / "models",
            llama_server_cmd=f'"{sys.executable}" "{MOCK_SERVER}"',
            llama_port=ports[name],
            agent_model_host="127.0.0.1",
            stop_timeout_seconds=5,
            load_timeout_seconds=60,
            **overrides,
        )
        (root / "models").mkdir(parents=True, exist_ok=True)
        return NodeAgent(settings, server=server or live, token=token, stats=_gpus)

    async def start(token: str, name: str = "b", wait: bool = True, server: str | None = None, **overrides):
        agent = make(token, name, server, **overrides)
        task = asyncio.create_task(agent.run())
        started.append((agent, task))
        if wait:
            await asyncio.wait_for(agent.connected.wait(), 15)
        return agent

    start.make = make
    yield start
    for agent, task in started:
        agent.on_exit = "stop"
        await agent.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _remote_lane(admin, node_id: int, name: str = "B", gpus: list[int] | None = None) -> dict:
    r = await admin.post("/api/admin/lanes", json={"name": name, "gpus": gpus or [0], "node_id": node_id})
    assert r.status_code == 201, r.text
    return r.json()


async def _task(admin, task_id: int) -> dict:
    return next(t for t in (await admin.get("/api/downloads")).json() if t["id"] == task_id)


async def _task_when(admin, task_id: int, *states: str):
    task = await _task(admin, task_id)
    return task if task["status"] in states else None


class Files:
    """An HTTP file server with Range support; ``slow`` files stream in small pieces with pauses."""

    def __init__(self):
        self.app = FastAPI()
        self.files: dict[str, bytes] = {}
        self.slow: dict[str, float] = {}  # name -> pause between 1 KB pieces
        self.ranges: list[str | None] = []
        self.gate: asyncio.Event | None = None  # slow files stop after the first piece until it is set

        @self.app.get("/files/{name}")
        async def get(name: str, request: Request):
            data = self.files[name]
            rng = request.headers.get("range")
            self.ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            body = data[start:]
            headers = {"Content-Length": str(len(body))}
            code = 200
            if rng:
                code = 206
                headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
            pause = self.slow.get(name)
            if pause is None:
                return Response(body, status_code=code, headers=headers, media_type="application/octet-stream")

            async def pieces():
                for i in range(0, len(body), 1024):
                    yield body[i : i + 1024]
                    if self.gate is not None and not self.gate.is_set():
                        await self.gate.wait()
                    await asyncio.sleep(pause)

            return StreamingResponse(pieces(), status_code=code, headers=headers, media_type="application/octet-stream")


@pytest.fixture
async def files():
    f = Files()
    port = _free_port()
    server, task = await _serve(f.app, port)
    f.url = f"http://127.0.0.1:{port}/files"
    yield f
    if f.gate is not None:
        f.gate.set()
    await _stop(server, task)


# ---- TLS ---------------------------------------------------------------------------------


def _certificates(folder: Path) -> tuple[Path, Path, Path, str]:
    """A private CA and a server certificate for 127.0.0.1 signed by it: (ca, chain, key, server fingerprint)."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SlotServe test CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    ca_path, chain_path, key_path = folder / "ca.pem", folder / "server.pem", folder / "server.key"
    ca_path.write_bytes(ca.public_bytes(pem))
    chain_path.write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    key_path.write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )
    return ca_path, chain_path, key_path, protocol.fingerprint(leaf.public_bytes(serialization.Encoding.DER))


async def _relayed_echo(app, node_id: int) -> None:
    """A relayed stream (cluster/relay.py) over the TLS link: the agent's data connection verifies the server too."""

    async def echo(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        target = {"kind": "tcp", "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1]}
        stream = await app.state.cluster.relay.open(app.state.nodes.get(node_id), target)
        await stream.write(b"over tls")
        assert await stream.read() == b"over tls"
        await stream.close()
    finally:
        server.close()


async def test_tls_link_with_ca_bundle_or_pinned_fingerprint(app, make_client, agents, tmp_path):
    ca, chain, key, fingerprint = _certificates(tmp_path)
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        lifespan="off",
        log_level="warning",
        ssl_certfile=str(chain),
        ssl_keyfile=str(key),
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _until(lambda: _is(server.started), what="TLS server up")
    url = f"https://127.0.0.1:{port}"
    try:
        admin = await make_client("root")
        app.state.settings.ssl_certfile = str(chain)
        link = (await admin.get("/api/admin/nodes/server")).json()
        assert link["tls"] and link["fingerprint"] == fingerprint and link["fingerprint_source"] == "certificate_file"
        created = await _add_node(admin)
        node_id = created["node"]["id"]

        # Pinned (self-signed style: no CA needed), lower-case without colons also works.
        pin = fingerprint.replace(":", "").lower()
        pinned = await agents(created["token"], name="pin", server=url, agent_server_fingerprint=pin)
        assert pinned.url.startswith("wss://") and pinned.node_id == node_id
        await _relayed_echo(app, node_id)  # relayed data connections are pinned the same way
        await pinned.close()

        # Trusting the private CA instead.
        trusted = await agents(created["token"], name="ca", server=url, agent_ca_bundle=str(ca))
        assert trusted.node_id == node_id
        await _relayed_echo(app, node_id)
        await trusted.close()

        # A different pinned certificate: refused before the token is ever sent (no failed login on the server).
        attempts = len(app.state.cluster.logins._failures)
        wrong = await agents(created["token"], name="wrong", server=url, wait=False, agent_server_fingerprint="AB" * 32)
        await _until(lambda: _is(wrong.status == "rejected"), what="pin mismatch")
        assert "certificate_mismatch" in wrong.last_error
        assert len(app.state.cluster.logins._failures) == attempts

        # Without the CA the certificate is not trusted: the agent keeps retrying with an error.
        plain = await agents(created["token"], name="plain", server=url, wait=False)
        await _until(lambda: _is(bool(plain.last_error)), what="untrusted certificate")
        assert "CERTIFICATE_VERIFY_FAILED" in plain.last_error or "certificate" in plain.last_error.lower()
        assert plain.fatal_code is None and not plain.connected.is_set()

        with pytest.raises(ValueError):
            agents.make(created["token"], name="bad", server="http://127.0.0.1:1", agent_server_fingerprint=pin)
    finally:
        app.state.settings.ssl_certfile = ""
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 10)


# ---- tokens, logins, audit ---------------------------------------------------------------


async def test_token_rotation_in_band_and_audit(app, make_client, agents):
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"])
    executor = app.state.nodes.get(node_id)
    peer = executor.peer

    rotated = (await admin.post(f"/api/admin/nodes/{node_id}/token")).json()
    assert rotated["delivered"] is True and rotated["token"] != created["token"]
    assert agent.token == rotated["token"]
    assert executor.peer is peer and executor.online  # kept its connection
    stored = json.loads((agent.settings.data_dir / agent_node.TOKEN_FILE).read_text())
    assert stored["token"] == rotated["token"]

    # A restarted agent with the old token in its configuration uses the stored one.
    again = agents.make(created["token"])
    assert again.token == rotated["token"]
    # ... unless the configuration was changed to another token.
    assert agents.make("ssn-other").token == "ssn-other"

    # Reconnecting works with the new token; the old one is refused.
    await app.state.cluster.disconnect(node_id, 1001, "test")
    await _until(lambda: _is(executor.online and executor.peer is not peer), what="reconnect with the new token")
    old = await agents(created["token"], name="old", wait=False)
    old.token = created["token"]  # (its data directory has no stored token anyway)
    await _until(lambda: _is(old.status == "rejected"), what="old token refused")
    await old.close()

    # An offline node gets a token to put into its configuration (not delivered).
    await agent.close()
    await _until(lambda: _is(not executor.online), what="agent stopped")
    offline = (await admin.post(f"/api/admin/nodes/{node_id}/token")).json()
    assert offline["delivered"] is False

    await admin.post(f"/api/admin/nodes/{node_id}/revoke")
    audit = (await admin.get("/api/admin/audit")).json()["items"]
    actions = [(a["action"], a["detail"].get("delivered")) for a in audit if a["target"] == "gpu-b"]
    assert ("node.create", None) in actions and ("node.revoke", None) in actions
    assert ("node.token", True) in actions and ("node.token", False) in actions
    async with SessionLocal() as db:
        from slotserve.orm import Node

        row = await db.get(Node, node_id)
        assert row.token_hash == protocol_hash(offline["token"]) and offline["token"] not in (row.token_hash or "")


def protocol_hash(token: str) -> str:
    from slotserve.cluster.hub import hash_token

    return hash_token(token)


async def test_failed_agent_logins_are_rate_limited(app, make_client, agents):
    app.state.settings.node_login_attempts = 3
    admin = await make_client("root")
    created = await _add_node(admin)
    for i in range(3):
        bad = await agents(f"ssn-wrong-{i}", name=f"x{i}", wait=False)
        await _until(lambda bad=bad: _is(bad.status == "rejected"), what="wrong token")
        assert bad.fatal_code == protocol.CLOSE_INVALID_TOKEN
    # The address is locked out now, even with a valid token.
    good = await agents(created["token"], name="good", wait=False)
    await _until(lambda: _is(good.status == "throttled"), what="rate limited")
    assert good.fatal_code == protocol.CLOSE_RATE_LIMITED and "too many failed node logins" in good.last_error
    app.state.cluster.logins._failures.clear()
    good.request_stop()


# ---- clocks, versions, health ----------------------------------------------------------------


async def test_clock_skew_is_reported_and_events_use_server_time(app, make_client, agents, monkeypatch):
    app.state.settings.node_heartbeat_seconds = 0.2
    real = agent_node.utcnow
    monkeypatch.setattr(agent_node, "utcnow", lambda: real() + timedelta(seconds=120))
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"])
    lane = await _remote_lane(admin, node_id)
    node = await _until(
        lambda: _node_if(admin, node_id, lambda n: (n["clock_skew_seconds"] or 0) > 100), what="skew measured"
    )
    assert node["clock_skew_warning"] is True and abs(node["clock_skew_seconds"] - 120) < 5
    executor = app.state.nodes.get(node_id)
    shifted = executor.server_time((real() + timedelta(seconds=120)).isoformat())
    assert abs((shifted - real()).total_seconds()) < 5

    # A load event with the node's (wrong) times is recorded at the time it arrived, with its real duration.
    future = real() + timedelta(hours=1)
    await agent.peer.emit(
        protocol.E_LOAD_EVENT,
        {
            "model_id": None,
            "lane_id": lane["id"],
            "started_at": future.isoformat(),
            "finished_at": (future + timedelta(seconds=10)).isoformat(),
            "success": True,
        },
    )

    async def recorded():
        async with SessionLocal() as db:
            return (
                await db.execute(select(ModelLoadEvent).where(ModelLoadEvent.lane_id == lane["id"]))
            ).scalar_one_or_none()

    event = await _until(recorded, what="load event")
    assert abs((event.finished_at - real()).total_seconds()) < 5
    assert abs((event.finished_at - event.started_at).total_seconds() - 10) < 0.01

    health = (await admin.get("/api/health")).json()
    assert health["status"] == "degraded" and any("clock" in p for p in health["problems"])


async def _node_if(admin, node_id, predicate):
    node = await _node(admin, node_id)
    return node if predicate(node) else None


async def test_versions_update_hint_and_health(app, make_client, agents, monkeypatch):
    admin = await make_client("root")
    assert (await (await make_client()).get("/api/health")).status_code == 401
    member = await make_client("bob")
    assert (await member.get("/api/health")).status_code == 401
    health = (await admin.get("/api/health")).json()
    assert health["status"] == "ok" and health["nodes"] == {"online": 1} and health["problems"] == []

    monkeypatch.setattr(agent_node, "__version__", "0.0.1")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"])
    await _remote_lane(admin, node_id)
    node = await _node(admin, node_id)
    assert node["agent_version"] == "0.0.1" and node["version_status"] == "outdated"
    assert "docker compose" in node["update_commands"]["compose"]
    assert node["update_commands"]["pip"].startswith("pip install --upgrade 'slotserve==")
    assert agent.server_version == app.version

    await admin.put("/api/admin/settings", json={"metrics_token": "m"})
    bearer = {"Authorization": "Bearer m"}
    health = (await (await make_client()).get("/api/health", headers=bearer)).json()
    assert health["status"] == "degraded" and any("older agent" in p for p in health["problems"])
    assert {n["name"]: n["version_status"] for n in health["node_list"]}["gpu-b"] == "outdated"
    r = await (await make_client()).get("/api/health", params={"strict": "true"}, headers=bearer)
    assert r.status_code == 503

    # Offline with a lane: reported; protocol mismatches tell the agent which version to run.
    await agent.close()
    await _until(lambda: _node_if(admin, node_id, lambda n: n["status"] == "offline"), what="offline")
    health = (await admin.get("/api/health")).json()
    assert "node gpu-b is offline" in health["problems"] and health["lanes"].get("offline") == 1
    assert protocol.update_commands(app.version)["compose"] in protocol.mismatch_message(2, "agent", app.version)


# ---- downloads ----------------------------------------------------------------------------


async def test_download_queues_per_node(app, make_client, agents, files):
    """A node that is offline (or busy) never holds up the downloads of other machines."""
    files.files["b.gguf"] = b"GGUF" + b"b" * 4000
    files.files["c.gguf"] = b"GGUF" + b"c" * 4000
    files.files["local.gguf"] = b"GGUF" + b"l" * 4000
    files.files["slow.gguf"] = b"GGUF" + b"s" * 20_000
    files.slow["slow.gguf"] = 0.05
    files.gate = asyncio.Event()
    admin = await make_client("root")
    node_b = await _add_node(admin, "gpu-b")
    node_c = await _add_node(admin, "gpu-c")
    agent_c = await agents(node_c["token"], name="c")

    async def queue(name: str, node: dict | None) -> int:
        body = {"url": f"{files.url}/{name}"}
        if node is not None:
            body["node_id"] = node["node"]["id"]
        r = await admin.post("/api/downloads", json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    blocked = await queue("b.gguf", node_b)  # node b never connected yet
    slow = await queue("slow.gguf", node_c)  # holds node c's queue
    behind = await queue("c.gguf", node_c)
    local = await queue("local.gguf", None)
    await _until(lambda: _task_when(admin, local, "done"), what="local download done")
    assert (await _task(admin, slow))["status"] == "running"
    assert (await _task(admin, behind))["status"] == "queued"  # one at a time per node
    assert (await _task(admin, blocked))["status"] == "queued"
    nodes = {n["name"]: n for n in (await admin.get("/api/admin/nodes")).json()}
    assert nodes["gpu-c"]["download_running"] == slow and nodes["gpu-c"]["downloads_queued"] == 1
    assert nodes["gpu-b"]["downloads_queued"] == 1

    files.gate.set()
    await _until(lambda: _task_when(admin, behind, "done"), what="node c queue drained")
    assert (agent_c.settings.models_dir / "c.gguf").read_bytes() == files.files["c.gguf"]
    assert (await _task(admin, blocked))["status"] == "queued"

    agent_b = await agents(node_b["token"], name="b")
    done = await _until(lambda: _task_when(admin, blocked, "done"), what="node b download after it connected")
    assert (agent_b.settings.models_dir / "b.gguf").read_bytes() == files.files["b.gguf"]
    model = next(m for m in (await admin.get("/api/models")).json() if m["id"] == done["model_id"])
    assert model["node_ids"] == [node_b["node"]["id"]]


async def test_remote_download_resumes_after_the_agent_restarts(app, make_client, agents, files, monkeypatch):
    monkeypatch.setattr("slotserve.services.downloader.CHUNK_FLUSH_BYTES", 1024)
    payload = b"GGUF" + bytes(range(256)) * 80  # ~20 KB
    files.files["big.gguf"] = payload
    files.slow["big.gguf"] = 0.05
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"])
    task_id = (await admin.post("/api/downloads", json={"url": f"{files.url}/big.gguf", "node_id": node_id})).json()[
        "id"
    ]
    part = agent.settings.models_dir / "big.gguf.part"
    await _until(lambda: _is(part.exists() and part.stat().st_size >= 4096), what="partly downloaded")

    # The agent stops (its download with it): the task waits in the queue, the .part file stays on the node.
    await agent.close()
    await _until(lambda: _task_when(admin, task_id, "queued"), what="back in the queue")
    assert part.exists()
    files.slow["big.gguf"] = 0.0
    again = await agents(created["token"])  # the same machine
    result = await _until(lambda: _task_when(admin, task_id, "done", "failed"), what="resumed download")
    assert result["status"] == "done", result
    assert (again.settings.models_dir / "big.gguf").read_bytes() == payload
    assert any(r and r.startswith("bytes=") and not r.startswith("bytes=0-") for r in files.ranges)


async def test_reconnect_reconciles_downloads(app, make_client, agents, files):
    """A download cancelled while its node was away is cancelled on the node when it reconnects; a result the
    node reported while disconnected is not lost."""
    files.files["stale.gguf"] = b"GGUF" + b"x" * 30_000
    files.slow["stale.gguf"] = 0.01
    files.gate = asyncio.Event()  # the download hangs after its first piece
    files.files["quick.gguf"] = b"GGUF" + b"q" * 2000
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"], agent_retry_seconds=2.0)
    executor = app.state.nodes.get(node_id)
    stale = (await admin.post("/api/downloads", json={"url": f"{files.url}/stale.gguf", "node_id": node_id})).json()
    await _until(lambda: _is(stale["id"] in agent.downloads._tasks), what="download running on the node")

    await app.state.cluster.disconnect(node_id, 1001, "test")
    await _until(lambda: _task_when(admin, stale["id"], "queued"), what="re-queued while offline")
    assert (await admin.delete(f"/api/downloads/{stale['id']}")).status_code == 204
    assert (await _task(admin, stale["id"]))["status"] == "cancelled"
    await _until(lambda: _is(executor.online), what="reconnect")
    files.gate.set()  # the next piece shows the agent the cancellation
    await _until(lambda: _is(stale["id"] not in agent.downloads._tasks), what="stale download cancelled on the node")
    assert agent.downloads.results[stale["id"]]["status"] == "cancelled"

    # A download that finished while the node was disconnected: the agent remembers the result.
    agent.downloads.results[4242] = {"task_id": 4242, "status": "done", "path": "/somewhere/x.gguf"}
    path = await app.state.cluster.run_download(node_id, 4242, f"{files.url}/quick.gguf", "x.gguf", {})
    assert path == "/somewhere/x.gguf"


async def test_catalog_deploy_onto_a_remote_lane(app, make_client, agents, monkeypatch):
    hub = FakeHub()
    try:
        app.state.settings.download_min_free_gb = 0.0
        admin = await make_client("root")
        await admin.put("/api/admin/settings", json={"hf_endpoint": hub.url})
        hub.repos["Qwen/Qwen3-8B-GGUF"] = {"Qwen3-8B-Q4_K_M.gguf": b"GGUF" + b"\0" * 2000}
        created = await _add_node(admin)
        node_id = created["node"]["id"]
        agent = await agents(created["token"])
        executor = app.state.nodes.get(node_id)
        executor.inventory["models_free_bytes"] = 10**13
        lane = await _remote_lane(admin, node_id)

        # The catalog fits models against the lane's own machine (two 24 GB GPUs there; the lane has GPU 0).
        listed = (await admin.get("/api/catalog")).json()
        cat_lane = next(x for x in listed["lanes"] if x["id"] == lane["id"])
        assert cat_lane["memory_bytes"] == 24_000 * 1024 * 1024
        assert cat_lane["node_name"] == "gpu-b" and cat_lane["node_online"] is True

        r = await admin.post(
            "/api/catalog/deploy",
            json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "lane_id": lane["id"], "set_default": True},
        )
        assert r.status_code == 201, r.text
        deploy = r.json()
        assert deploy["node_id"] == node_id and deploy["files"] == 1
        async with SessionLocal() as db:
            tasks = list((await db.execute(select(DownloadTask))).scalars())
        assert [t.node_id for t in tasks] == [node_id]

        async def finished():
            d = next(x for x in (await admin.get("/api/catalog/deploys")).json() if x["id"] == deploy["id"])
            return d if d["status"] in ("done", "failed", "stalled") else None

        result = await _until(finished, what="remote deploy")
        assert result["status"] == "done", result
        target = agent.settings.models_dir / "Qwen3-8B-Q4_K_M.gguf"
        assert target.is_file() and not (app.state.settings.models_dir / target.name).exists()
        model = next(m for m in (await admin.get("/api/models")).json() if m["id"] == result["model_id"])
        assert model["node_ids"] == [node_id] and "-c " in model["args"]
        lanes = {x["id"]: x for x in (await admin.get("/api/lanes")).json()}
        assert lanes[lane["id"]]["default_model_id"] == model["id"]
        variant = next(v for v in (await admin.get("/api/catalog/qwen3-8b")).json()["variants"] if v["id"] == "q4_k_m")
        assert variant["node_ids"] == [node_id] and variant["models"][0]["id"] == model["id"]

        # Deploying again finds it registered on the node: nothing to download.
        r = await admin.post(
            "/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "lane_id": lane["id"]}
        )
        assert r.status_code == 201 and r.json()["status"] == "done" and r.json()["files"] == 0
        assert r.json()["model_id"] == model["id"]
    finally:
        hub.close()


async def test_model_libraries_on_nodes(app, make_client, agents, files, tmp_path):
    files.files["lib.gguf"] = b"GGUF" + b"L" * 3000
    files.files["nfs.gguf"] = b"GGUF" + b"N" * 3000
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    library = tmp_path / "library"
    library.mkdir()
    r = await admin.post(
        "/api/admin/storage",
        json={"name": "lib", "kind": "host", "purpose": "models", "host_path": "/srv/models-lib", "enabled": True},
    )
    assert r.status_code == 201, r.text
    host = r.json()
    r = await admin.post(
        "/api/downloads", json={"url": f"{files.url}/lib.gguf", "node_id": node_id, "storage_id": host["id"]}
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "storage_remote_node"
    body = {k: host[k] for k in ("name", "kind", "purpose", "host_path", "enabled")}
    r = await admin.put(f"/api/admin/storage/{host['id']}", json={**body, "node_ids": [node_id]})
    assert r.status_code == 200 and r.json()["node_ids"] == [node_id]
    r = await admin.put(f"/api/admin/storage/{host['id']}", json={**body, "node_ids": [999]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "storage_nodes_invalid"
    async with SessionLocal() as db:  # the directory as this test machine names it (validation wants a POSIX path)
        from slotserve.orm import Storage

        (await db.get(Storage, host["id"])).host_path = str(library)
        await db.commit()

    # NFS: the agent creates the Docker volume on its machine and keeps it mounted.
    fake = app.state.fake_docker
    fake.volume_root = tmp_path / "volumes"
    r = await admin.post(
        "/api/admin/storage",
        json={
            "name": "nas",
            "kind": "nfs",
            "purpose": "models",
            "server": "nas.local",
            "remote_path": "/export/models",
            "local_path": (tmp_path / "nas-on-server").as_posix(),
            "node_ids": [node_id],
            "enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    nas = r.json()

    agent = agents.make(created["token"])
    agent.libraries._docker_factory = lambda: DockerClient(
        "unix:///fake/docker.sock", transport=httpx.ASGITransport(app=fake.app)
    )
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(agent.connected.wait(), 15)
        await _until(lambda: _is(len(agent.libraries.dirs) == 2), what="libraries on the node")
        assert agent.libraries.dirs[host["id"]] == str(library)
        mountpoint = Path(agent.libraries.dirs[nas["id"]])
        volume = next(v for v in fake.volumes.values() if v["Options"].get("type") == "nfs")
        assert volume["Options"]["o"].startswith("addr=nas.local") and mountpoint.is_dir()
        holder = next(c for c in fake.containers.values() if c["Name"].lstrip("/").startswith("slotserve-lib-"))
        assert holder["Running"]
        node = await _until(
            lambda: _node_if(admin, node_id, lambda n: len(n["libraries"]) == 2), what="libraries reported"
        )
        assert all(lib["error"] is None for lib in node["libraries"])

        for name, storage in (("lib.gguf", host), ("nfs.gguf", nas)):
            r = await admin.post(
                "/api/downloads", json={"url": f"{files.url}/{name}", "node_id": node_id, "storage_id": storage["id"]}
            )
            assert r.status_code == 201, r.text
            done = await _until(lambda r=r: _task_when(admin, r.json()["id"], "done", "failed"), what=name)
            assert done["status"] == "done", done
        assert (library / "lib.gguf").read_bytes() == files.files["lib.gguf"]
        assert (mountpoint / "nfs.gguf").read_bytes() == files.files["nfs.gguf"]
        async with SessionLocal() as db:
            paths = set((await db.execute(select(NodeModel.path).where(NodeModel.node_id == node_id))).scalars())
        assert str((library / "lib.gguf").resolve()) in paths and str((mountpoint / "nfs.gguf").resolve()) in paths
        scanned = {f["path"] for f in (await agent.inventory())["models"]}
        assert str(mountpoint / "nfs.gguf") in scanned
    finally:
        await agent.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---- usage per node ----------------------------------------------------------------------


async def test_usage_per_node(app, make_client, agents, monkeypatch):
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    await agents(created["token"])
    lane = await _remote_lane(admin, node_id)
    executor = app.state.nodes.get(node_id)
    await _until(lambda: _is(bool(executor.metrics.get("gpus"))), what="metrics")
    when = (utcnow() - timedelta(minutes=3)).replace(second=0, microsecond=0)
    await app.state.usage_sampler.sample(when)
    async with SessionLocal() as db:
        rows = list((await db.execute(select(UsageSample).where(UsageSample.start == when))).scalars())
    assert {r.node_id for r in rows if r.lane_id == lane["id"]} == {node_id}
    assert {r.node_id for r in rows if r.lane_id != lane["id"]} == {app.state.nodes.local.node_id}

    usage = (await admin.get("/api/usage", params={"days": 1})).json()
    nodes = {n["node_name"]: n for n in usage["nodes"]}
    assert set(nodes) == {"local", "gpu-b"}
    assert nodes["gpu-b"]["lanes"] == 1 and nodes["gpu-b"]["sampled_gpu_hours"] > 0
    assert nodes["gpu-b"]["occupancy"] == {"free": nodes["gpu-b"]["sampled_gpu_hours"]}
    assert nodes["gpu-b"]["occupied_pct"] == 0.0
    assert next(x for x in usage["lanes"] if x["lane_id"] == lane["id"])["node_name"] == "gpu-b"
    assert {g["node_name"] for g in usage["gpus"] if g["lane_id"] == lane["id"]} == {"gpu-b"}
    csv = (await admin.get("/api/usage/export", params={"days": 1, "kind": "compute"})).text
    assert any(line.split(",")[1:3] == ["gpu-b", "B"] for line in csv.splitlines())
    csv = (await admin.get("/api/usage/export", params={"days": 1, "kind": "nodes"})).text
    assert any(line.startswith("gpu-b,1,1,") for line in csv.splitlines())

    await admin.put("/api/admin/settings", json={"metrics_token": "m"})
    text = (await admin.get("/metrics", headers={"Authorization": "Bearer m"})).text
    assert 'slotserve_gpu_utilization_ratio{node="gpu-b",gpu="0",name="Node GPU",lane="B"} 0.7' in text
    assert 'slotserve_gpu_utilization_ratio{node="gpu-b",gpu="1",name="Node GPU",lane=""} 0.7' in text
    assert 'slotserve_node_up{node="gpu-b"} 1' in text and 'slotserve_node_up{node="local"} 1' in text
    assert 'slotserve_gpu_seconds_total{node="gpu-b",lane="B",gpu="0",kind="free"} 60' in text
    assert 'slotserve_lane_occupant{node="gpu-b",lane="B",kind="free",user=""} 1' in text
    assert 'slotserve_node_clock_skew_seconds{node="gpu-b"}' in text


# ---- keeping model servers across agent restarts ---------------------------------------------


async def test_agent_keeps_model_servers_and_takes_them_over(app, make_client, agents, monkeypatch):
    monkeypatch.setenv("MOCK_LOAD_SECONDS", "0.5")
    admin = await make_client("root")
    created = await _add_node(admin)
    node_id = created["node"]["id"]
    agent = await agents(created["token"], agent_on_exit="keep")
    make_gguf(agent.settings.models_dir, "kept.gguf")
    lane = await _remote_lane(admin, node_id)
    await admin.post("/api/models/scan", params={"node_id": node_id})
    model = next(m for m in (await admin.get("/api/models")).json() if m["name"] == "kept")
    await admin.post(f"/api/models/{model['id']}/default", params={"lane_id": lane["id"]})
    await _until(lambda: _lane_state(admin, "B", "ready"), what="ready")
    pid = agent.runtimes[lane["id"]].pid
    try:
        await _keep_and_take_over(app, admin, agents, created, agent, lane, node_id, pid)
    finally:
        from slotserve.runtime.adopted import AdoptedProcess

        if pid_alive(pid):  # never leave the kept server behind, also when the test fails
            AdoptedProcess(pid).kill()


async def _keep_and_take_over(app, admin, agents, created, agent, lane, node_id, pid):
    await agent.close()  # keeps the model server running
    assert pid_alive(pid) and (agent.settings.data_dir / agent_node.STATE_FILE).is_file()
    executor = app.state.nodes.get(node_id)
    await _until(lambda: _is(not executor.online), what="agent gone")

    successor = await agents(created["token"])  # the same machine, default: stop on exit
    rt = successor.runtimes[lane["id"]]
    assert rt.pid == pid and rt.state.value == "ready" and rt.model_name == "kept"
    assert not (successor.settings.data_dir / agent_node.STATE_FILE).exists()
    status = await _until(lambda: _lane_ready(admin, "B"), what="ready again without a reload")
    assert status["runtime"]["model_name"] == "kept"
    await asyncio.sleep(1.0)
    assert successor.runtimes[lane["id"]].pid == pid  # the scheduler did not restart it

    await successor.close()  # on_exit=stop: the taken-over server is stopped too
    await _until(lambda: _is(not pid_alive(pid)), what="kept server stopped")


async def _lane_state(admin, name: str, state: str):
    return (await _lane(admin, name))["runtime"]["state"] == state


async def _lane_ready(admin, name: str):
    lane = await _lane(admin, name)
    return lane if lane["runtime"]["state"] == "ready" else None


async def test_reconnect_back_off_is_jittered(app):
    settings = Settings(_env_file=None, agent_retry_seconds=1.0, agent_max_retry_seconds=8.0)
    agent = NodeAgent(settings, server="http://127.0.0.1:1", token="ssn-x")
    delays = []
    for attempts in range(1, 8):
        agent.attempts = attempts
        delays.append(agent.next_delay())
    assert 0.75 <= delays[0] <= 1.25 and 1.5 <= delays[1] <= 2.5 and all(d <= 10 for d in delays)
    assert 6 <= delays[-1] <= 10
    agent.attempts = 6
    assert len({round(agent.next_delay(), 6) for _ in range(20)}) > 1  # not in lock-step
