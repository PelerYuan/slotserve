"""Several lanes (GPU groups): separate processes, calendars and gateway routing."""

import asyncio
from datetime import timedelta

import pytest

from conftest import make_gguf
from slotserve.db import utcnow
from slotserve.schemas import GpuOut


@pytest.fixture(autouse=True)
def four_gpus(monkeypatch):
    async def fake():
        return [
            GpuOut(
                index=i, name="Test GPU", memory_used_mb=0, memory_total_mb=24_000, utilization_pct=0, temperature_c=30
            )
            for i in range(4)
        ]

    monkeypatch.setattr("slotserve.api.lanes.gpu_stats", fake)


async def _lanes_status(client):
    return {lane["name"]: lane for lane in (await client.get("/api/status")).json()["lanes"]}


async def _wait(client, predicate, timeout=25.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await _lanes_status(client)
        if predicate(last):
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"lanes never reached the expected state: {last}")


async def _setup(make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    alice = await make_client("alice")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    first = (await admin.get("/api/lanes")).json()[0]
    await admin.post(f"/api/models/{models['small']}/default", params={"lane_id": first["id"]})
    r = await admin.post("/api/admin/lanes", json={"name": "B", "gpus": [2, 3], "default_model_id": models["large"]})
    assert r.status_code == 201, r.text
    keys = {}
    for name, c in (("root", admin), ("bob", bob), ("alice", alice)):
        keys[name] = {"Authorization": f"Bearer {(await c.post('/api/keys', json={'name': 'k'})).json()['key']}"}
    return admin, bob, alice, models, first, r.json(), keys


async def test_lane_admin_rules(app, make_client, models_dir):
    admin, bob, alice, models, first, lane_b, _keys = await _setup(make_client, models_dir)
    assert lane_b["gpus"] == [2, 3] and lane_b["default_model_name"] == "large"
    assert app.state.lanes.get(lane_b["id"]).runtime.devices == [2, 3]
    assert app.state.lanes.get(lane_b["id"]).runtime.port == app.state.settings.llama_port + lane_b["id"] - 1

    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [3]})
    assert r.json()["detail"]["code"] == "lane_gpus_overlap"
    r = await admin.post("/api/admin/lanes", json={"name": "b", "gpus": [1]})
    assert r.json()["detail"]["code"] == "lane_name_taken"
    r = await admin.post("/api/admin/lanes", json={"name": "C", "gpus": [7]})
    assert r.json()["detail"]["code"] == "lane_gpus_invalid"
    assert (await bob.post("/api/admin/lanes", json={"name": "X", "gpus": []})).status_code == 403

    # bookings on different lanes do not clash
    start, end = (utcnow() + timedelta(hours=3)).isoformat(), (utcnow() + timedelta(hours=4)).isoformat()
    r1 = await bob.post("/api/reservations", json={"model_id": models["small"], "start_at": start, "end_at": end})
    r2 = await alice.post(
        "/api/reservations",
        json={"model_id": models["large"], "start_at": start, "end_at": end, "lane_id": lane_b["id"]},
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()[0]["lane_name"] == "B"
    r3 = await alice.post("/api/reservations", json={"model_id": models["small"], "start_at": start, "end_at": end})
    assert r3.json()["detail"]["code"] == "reservation_conflict"

    assert (await admin.delete(f"/api/admin/lanes/{lane_b['id']}")).json()["detail"]["code"] == "lane_has_reservations"
    await alice.delete(f"/api/reservations/{r2.json()[0]['id']}")
    assert (await admin.delete(f"/api/admin/lanes/{lane_b['id']}")).status_code == 204
    assert (await admin.delete(f"/api/admin/lanes/{first['id']}")).json()["detail"]["code"] == "last_lane"
    assert len((await admin.get("/api/lanes")).json()) == 1


async def test_two_lanes_serve_and_route(app, make_client, models_dir):
    admin, bob, alice, models, first, lane_b, keys = await _setup(make_client, models_dir)
    lanes = await _wait(
        admin,
        lambda s: all(lane["runtime"]["state"] == "ready" for lane in s.values()) and len(s) == 2,
    )
    assert {lane["runtime"]["model_name"] for lane in lanes.values()} == {"small", "large"}

    listed = (await alice.get("/v1/models", headers=keys["alice"])).json()["data"]
    assert sorted(m["id"] for m in listed) == ["large", "small"]

    async def chat(client, key, model=None):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        if model:
            body["model"] = model
        r = await client.post("/v1/chat/completions", headers=key, json=body)
        return r.status_code, (r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.json())

    assert (await chat(alice, keys["alice"], "large"))[1].startswith("[large]")
    assert (await chat(alice, keys["alice"], "small"))[1].startswith("[small]")

    # bob books the first lane: others are routed to lane B, bob gets his own lane
    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "model_id": models["small"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201
    await _wait(admin, lambda s: s[first["name"]]["active_reservation"] is not None)
    assert (await chat(alice, keys["alice"]))[1].startswith("[large]")
    code, err = await chat(alice, keys["alice"], "small")
    assert code == 403 and err["error"]["code"] == "gpu_reserved"
    assert (await chat(bob, keys["bob"]))[1].startswith("[small]")
    assert (await chat(bob, keys["bob"], "large"))[1].startswith("[large]")

    status = await _lanes_status(admin)
    assert status["B"]["active_reservation"] is None and status["B"]["default_model"] == "large"
    logs = (await admin.get("/api/runtime/logs", params={"lane_id": lane_b["id"]})).json()["lines"]
    assert any("--alias large" in line for line in logs)
