"""End-to-end: real scheduler + mock llama-server subprocess + gateway."""

import asyncio
import json
from datetime import timedelta

from conftest import make_gguf, wait_for_state

from slotserve.db import utcnow


async def _setup(make_client, models_dir):
    admin = await make_client("alice")
    bob = await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    await admin.post(f"/api/models/{models['small']}/default")
    keys = {}
    for name, c in (("alice", admin), ("bob", bob)):
        keys[name] = {"Authorization": f"Bearer {(await c.post('/api/keys', json={'name': 'k'})).json()['key']}"}
    return admin, bob, models, keys


async def test_free_access_and_usage(make_client, models_dir):
    admin, bob, models, keys = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")

    listed = (await bob.get("/v1/models", headers=keys["bob"])).json()
    assert [m["id"] for m in listed["data"]] == ["small"]

    r = await bob.post(
        "/v1/chat/completions",
        headers=keys["bob"],
        json={"model": "anything", "messages": [{"role": "user", "content": "hello there"}]},
    )
    assert r.status_code == 200, r.text
    assert "hello there" in r.json()["choices"][0]["message"]["content"]

    # Streaming: usage is injected and recorded even though the client did not ask for it.
    async with bob.stream(
        "POST",
        "/v1/chat/completions",
        headers=keys["bob"],
        json={"messages": [{"role": "user", "content": "stream me"}], "stream": True},
    ) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert body.strip().endswith("data: [DONE]")
    assert '"usage"' in body

    await asyncio.sleep(0.3)  # usage rows are written in the background
    usage = (await admin.get("/api/usage")).json()
    bob_row = next(u for u in usage["users"] if u["username"] == "bob")
    assert bob_row["requests"] == 2
    assert bob_row["prompt_tokens"] > 0 and bob_row["completion_tokens"] > 0


async def test_model_not_loaded_is_409(make_client, models_dir):
    admin, _, _, keys = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    r = await admin.post("/v1/chat/completions", headers=keys["alice"], json={"model": "large", "messages": []})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "model_not_loaded"


async def test_reservation_is_exclusive_and_switches_model(make_client, models_dir):
    admin, bob, models, keys = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")

    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={"model_id": models["large"], "start_at": now.isoformat(), "end_at": (now + timedelta(hours=1)).isoformat()},
    )
    assert r.status_code == 201
    res_id = r.json()[0]["id"]

    # Alice is locked out right away, even while the model is switching.
    r = await admin.post("/v1/chat/completions", headers=keys["alice"], json={"messages": []})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "gpu_reserved"

    await wait_for_state(admin, "ready", "large")
    r = await bob.post("/v1/completions", headers=keys["bob"], json={"prompt": "hi"})
    assert r.status_code == 200
    status = (await bob.get("/api/status")).json()
    assert status["active_reservation"]["id"] == res_id and status["free_access"] is False

    # Releasing returns the GPU to the default model and to everyone.
    await bob.delete(f"/api/reservations/{res_id}")
    await wait_for_state(admin, "ready", "small")
    r = await admin.post("/v1/chat/completions", headers=keys["alice"], json={"messages": []})
    assert r.status_code == 200


async def test_loading_returns_503_with_retry_after(make_client, models_dir, monkeypatch):
    monkeypatch.setenv("MOCK_LOAD_SECONDS", "3")
    admin, _, _, keys = await _setup(make_client, models_dir)
    await wait_for_state(admin, "loading", "small")
    r = await admin.post("/v1/chat/completions", headers=keys["alice"], json={"messages": []})
    assert r.status_code == 503
    assert "retry-after" in r.headers


async def test_failed_start_reports_error_and_logs(make_client, models_dir, app):
    admin, _, models, _ = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    # A launch argument that makes the mock exit immediately.
    await admin.patch(f"/api/models/{models['small']}", json={"args": "--port notanumber"})
    await admin.post("/api/runtime/restart")
    # --port is managed by SlotServe and stripped, so the restart still succeeds.
    await wait_for_state(admin, "ready", "small")
    logs = (await admin.get("/api/runtime/logs")).json()["lines"]
    assert any("start:" in line for line in logs)


async def test_status_payload(make_client, models_dir):
    admin, *_ = await _setup(make_client, models_dir)
    s = (await admin.get("/api/status")).json()
    assert s["default_model"] == "small"
    assert s["free_access"] is True
    assert isinstance(s["gpus"], list)
    cfg = (await admin.get("/api/config")).json()
    assert cfg["slot_minutes"] == 30
    assert json.dumps(s)
