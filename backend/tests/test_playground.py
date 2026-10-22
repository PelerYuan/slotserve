"""F05 / D16: the Playground chats through the gateway as the signed-in person, without any key."""

import asyncio
from datetime import timedelta

from conftest import make_gguf, wait_for_state
from slotserve.db import utcnow

HELLO = {"messages": [{"role": "user", "content": "hello playground"}]}


async def _setup(make_client, models_dir):
    admin = await make_client("alice")
    bob = await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    await admin.post(f"/api/models/{models['small']}/default")
    await wait_for_state(admin, "ready", "small")
    return admin, bob, models


async def test_chat_without_a_key_is_metered_as_playground(make_client, models_dir):
    admin, bob, _ = await _setup(make_client, models_dir)
    state = (await bob.get("/api/playground")).json()
    assert state == {"enabled": True, "rate_per_minute": 20, "default_model_only": False, "default_model": "small"}

    r = await bob.post("/api/playground/chat", json=HELLO)
    assert r.status_code == 200, r.text
    assert "hello playground" in r.json()["choices"][0]["message"]["content"]

    async with bob.stream("POST", "/api/playground/chat", json={**HELLO, "stream": True}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert body.strip().endswith("data: [DONE]")

    assert (await bob.get("/api/keys")).json() == []  # no key was created
    await asyncio.sleep(0.3)  # usage rows are written in the background
    log = (await admin.get("/api/admin/requests", params={"source": "playground"})).json()
    assert log["total"] == 2
    assert {(i["username"], i["source"], i["key_name"]) for i in log["items"]} == {("bob", "playground", None)}
    assert (await admin.get("/api/admin/requests", params={"source": "api"})).json()["total"] == 0


async def test_needs_the_session_and_the_csrf_header(app, make_client, models_dir):
    _admin, bob, _ = await _setup(make_client, models_dir)
    key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
    anon = await make_client()
    r = await anon.post("/api/playground/chat", json=HELLO, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 401  # a key is no way into the Playground
    r = await bob.post("/api/playground/chat", json=HELLO, headers={"X-SlotServe": ""})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf_refused"
    r = await bob.post("/api/playground/chat", json={**HELLO, "tools": []})
    assert r.status_code == 422  # only the documented fields pass


async def test_never_switches_models_or_takes_a_booking(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    r = await bob.post("/api/playground/chat", json={**HELLO, "model": "large"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "model_not_loaded"
    assert "message" in r.json()["detail"]["params"]
    await asyncio.sleep(1.0)
    status = (await admin.get("/api/status")).json()["runtime"]
    assert (status["state"], status["model_name"]) == ("ready", "small")

    now = utcnow()
    r = await admin.post(
        "/api/reservations",
        json={
            "model_id": models["small"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    r = await bob.post("/api/playground/chat", json=HELLO)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "gpu_reserved"


async def test_admin_switch_rate_limit_and_default_only(app, make_client, models_dir):
    admin, bob, _ = await _setup(make_client, models_dir)
    put = await admin.put("/api/admin/settings", json={"playground_rate_per_minute": 2})
    assert put.status_code == 200, put.text
    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 200
    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 200
    r = await bob.post("/api/playground/chat", json=HELLO)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "playground_rate_limited"
    assert r.json()["detail"]["params"]["seconds"] >= 1
    assert (await admin.post("/api/playground/chat", json=HELLO)).status_code == 200  # per person

    await admin.put(
        "/api/admin/settings", json={"playground_default_model_only": True, "playground_rate_per_minute": 0}
    )
    r = await admin.post("/api/playground/chat", json={**HELLO, "model": "large"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "playground_model_not_allowed"
    assert (await admin.post("/api/playground/chat", json={**HELLO, "model": "small"})).status_code == 200

    await admin.put("/api/admin/settings", json={"playground_enabled": False})
    r = await admin.post("/api/playground/chat", json=HELLO)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "playground_disabled"
    assert (await bob.get("/api/playground")).json()["enabled"] is False
