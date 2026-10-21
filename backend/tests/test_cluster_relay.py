# ruff: noqa: F811  (the cluster fixtures are imported from test_cluster)
"""The relay: every byte to and from a node travels over connections its agent opens to the control plane.

A real control plane (uvicorn on a random port) and an in-process agent; targets on the "node" are servers on the
loopback interface (an echo server stands in for SSH, a WebSocket echo server for a web interface).
"""

import asyncio
import contextlib
import os

import httpx
import pytest
import websockets
from test_cluster import _add_node, _free_port, _remote_setup, _until, live, start_agent  # noqa: F401

from slotserve.cluster import protocol
from slotserve.cluster.relay import RelayError, RelayListener


async def _echo_server(greeting: bytes = b""):
    """A TCP echo server; on EOF from the client it says "bye" and closes (exercises half-close)."""
    seen = {"connections": 0, "closed": 0}

    async def handle(reader, writer):
        seen["connections"] += 1
        if greeting:
            writer.write(greeting)
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.write(b"bye")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            seen["closed"] += 1
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


async def _node(app, make_client, start_agent):
    admin = await make_client("root")
    created = await _add_node(admin)
    agent = await start_agent(created["token"])
    return admin, agent, app.state.nodes.get(created["node"]["id"])


async def _listener(app, executor, target: dict) -> RelayListener:
    listener = RelayListener.to(app.state.cluster.relay, lambda: executor, target, "127.0.0.1", 0, "test")
    await listener.start()
    return listener


async def _read_all(reader) -> bytes:
    out = b""
    while chunk := await reader.read(65536):
        out += chunk
    return out


async def test_tcp_relay_echo_half_close_and_counters(app, make_client, start_agent):
    _admin, agent, executor = await _node(app, make_client, start_agent)
    server, port, seen = await _echo_server(greeting=b"SSH-2.0-test\r\n")
    listener = await _listener(app, executor, {"kind": "tcp", "host": "127.0.0.1", "port": port})
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        assert await reader.readexactly(14) == b"SSH-2.0-test\r\n"
        payload = os.urandom(3 * 1024 * 1024 + 17)  # several chunks, both ways at once
        writer.write(payload)
        await writer.drain()
        echoed = await reader.readexactly(len(payload))
        assert echoed == payload
        writer.write_eof()  # half-close: the target sees EOF and still answers
        assert await asyncio.wait_for(_read_all(reader), 10) == b"bye"
        writer.close()
        stats = executor.relay_stats()
        assert stats["opened"] == 1 and stats["bytes_out"] >= len(payload) and stats["bytes_in"] >= len(payload)
        await _until(lambda: _is(executor.relay_stats()["open"] == 0), what="stream closed", timeout=10)
        await _until(lambda: _is(agent.relay.open_streams == 0), what="agent side closed", timeout=10)
        assert seen == {"connections": 1, "closed": 1}
    finally:
        await listener.close()
        server.close()


async def _is(value: bool) -> bool:
    return value


async def test_relay_targets_limits_idle_and_auth(app, make_client, start_agent, live):
    admin, agent, executor = await _node(app, make_client, start_agent)
    relay = app.state.cluster.relay
    server, port, _seen = await _echo_server()
    try:
        # Unreachable or forbidden targets are refused by the agent, with a reason.
        with pytest.raises(RelayError) as err:
            await relay.open(executor, {"kind": "tcp", "host": "127.0.0.1", "port": _free_port()})
        assert err.value.code == "unreachable"
        with pytest.raises(RelayError) as err:
            await relay.open(executor, {"kind": "tcp", "host": "10.1.2.3", "port": 22})
        assert err.value.code == "forbidden"
        assert executor.relay_stats()["failed"] == 2 and executor.relay_stats()["open"] == 0

        # At most relay_max_streams streams per node.
        app.state.settings.relay_max_streams = 2
        target = {"kind": "tcp", "host": "127.0.0.1", "port": port}
        first, second = await relay.open(executor, target), await relay.open(executor, target)
        with pytest.raises(RelayError) as err:
            await relay.open(executor, target)
        assert err.value.code == "too_many_streams" and executor.relay_stats()["rejected"] == 1
        assert executor.relay_stats()["by_kind"] == {"tcp": 2}
        await first.write(b"ping")
        assert await first.read() == b"ping"
        await first.close()
        third = await relay.open(executor, target)  # room again

        # Streams without traffic are closed after relay_idle_seconds.
        # ... unless they have no idle limit of their own (SSH): those stay however quiet they are.
        app.state.settings.relay_max_streams = 10
        quiet_ssh = await relay.open(executor, target, idle=0)
        app.state.settings.relay_idle_seconds = 0.3
        await asyncio.sleep(0.5)
        await relay.sweep()
        assert second.closed.is_set() and third.closed.is_set() and not quiet_ssh.closed.is_set()
        assert await second.read() == b""
        await quiet_ssh.write(b"still here")
        assert await quiet_ssh.read() == b"still here"
        await quiet_ssh.close()
        await _until(lambda: _is(agent.relay.open_streams == 0), what="agent closed idle streams", timeout=10)
        app.state.settings.relay_idle_seconds = 3600

        # Slow handling of a node's events (database writes) does not hold up its other frames.
        hub = app.state.cluster
        original = hub._on_event

        async def slow(executor_, name, data):
            if name == protocol.E_INVENTORY:
                await asyncio.sleep(3)
            await original(executor_, name, data)

        hub._on_event = slow
        executor.peer.on_event = lambda name, data: slow(executor, name, data)
        await agent.peer.emit(protocol.E_INVENTORY, await agent.inventory())
        started = asyncio.get_running_loop().time()
        fast = await relay.open(executor, target)
        assert asyncio.get_running_loop().time() - started < 2
        await fast.close()
        hub._on_event = original

        # A data connection needs the node's token and the stream's one-time secret.
        url = live.replace("http://", "ws://") + protocol.STREAM_PATH + "/nope"
        with pytest.raises(websockets.InvalidStatus):
            await websockets.connect(url, additional_headers={"Authorization": f"Bearer {agent.token}"})
        stream_task = asyncio.create_task(relay.open(executor, target))
        await _until(lambda: _is(bool(relay._pending) or stream_task.done()), what="stream pending")
        if relay._pending:
            pending_id = next(iter(relay._pending))
            bad = {"Authorization": "Bearer ssn-wrong", protocol.STREAM_SECRET_HEADER: "x"}
            with pytest.raises(websockets.InvalidStatus):
                await websockets.connect(
                    f"{live.replace('http://', 'ws://')}{protocol.STREAM_PATH}/{pending_id}", additional_headers=bad
                )
        stream = await stream_task
        await stream.write(b"still fine")
        assert await stream.read() == b"still fine"
        await stream.close()

        # After an in-band token rotation the agent's relayed connections use (and need) the new token.
        r = await admin.post(f"/api/admin/nodes/{executor.node_id}/token")
        assert r.status_code == 200 and r.json()["token"] == agent.token
        stream = await relay.open(executor, target)
        await stream.write(b"new token")
        assert await stream.read() == b"new token"
        await stream.close()

        # Revoking a node ends its streams.
        stream = await relay.open(executor, target)
        await app.state.cluster.disconnect(executor.node_id, protocol.CLOSE_REVOKED, "revoked")
        assert stream.closed.is_set()
    finally:
        server.close()


async def test_websocket_through_relay(app, make_client, start_agent):
    _admin, _agent, executor = await _node(app, make_client, start_agent)

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async with websockets.serve(echo, "127.0.0.1", 0) as ws_server:
        port = next(iter(ws_server.sockets)).getsockname()[1]
        listener = await _listener(app, executor, {"kind": "tcp", "host": "127.0.0.1", "port": port})
        try:
            async with websockets.connect(f"ws://127.0.0.1:{listener.port}/x") as ws:
                await ws.send("hello")
                assert await ws.recv() == "hello"
                big = os.urandom(200_000)
                await ws.send(big)
                assert await ws.recv() == big
        finally:
            await listener.close()


async def test_gateway_streams_through_relay_and_cancels(app, make_client, start_agent, monkeypatch, live):
    monkeypatch.setenv("MOCK_TOKEN_DELAY", "0.2")
    admin, agent, node_id, lane, models = await _remote_setup(app, make_client, start_agent)
    executor = app.state.nodes.get(node_id)
    await admin.post(f"/api/models/{models['remote']['id']}/default", params={"lane_id": lane["id"]})

    async def ready():
        lanes = (await admin.get("/api/status")).json()["lanes"]
        return next(x for x in lanes if x["id"] == lane["id"])["runtime"]["state"] == "ready"

    await _until(ready, what="remote model ready")
    key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
    body = {"model": "remote", "stream": True, "messages": [{"role": "user", "content": " ".join(["word"] * 60)}]}
    headers = {"Authorization": f"Bearer {key}"}

    # A streamed answer arrives chunk by chunk through the relay (over a real HTTP connection).
    async with httpx.AsyncClient(base_url=live, timeout=30) as client:
        async with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as resp:
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
            first = None
            async for chunk in resp.aiter_text():
                if "data:" in chunk:
                    first = chunk
                    break
            assert first is not None and "[remote]" in first
            assert executor.relay_stats()["by_kind"].get("model") == 1
            # The client goes away mid-answer: the relayed connection to the model server is closed too.
    await _until(lambda: _is(executor.relay_stats()["by_kind"].get("model") is None), what="relay closed", timeout=10)
    await _until(lambda: _is(agent.relay.open_streams == 0), what="agent closed", timeout=10)
    assert executor.relay_stats()["bytes_in"] > 0

    # A complete answer still records usage as before.
    async with httpx.AsyncClient(base_url=live, timeout=60) as client:
        r = await client.post("/v1/chat/completions", json={**body, "stream": False}, headers=headers, timeout=60)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"].startswith("[remote]")
    with contextlib.suppress(Exception):
        await executor.close_clients()
