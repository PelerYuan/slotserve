"""Draining, VRAM warnings, login throttling, persistence, outbox, backups and retention."""

import asyncio
import sqlite3
from datetime import timedelta

from sqlalchemy import select, update

from conftest import make_gguf, wait_for_state
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import OutboxItem, UsageDaily, UsageLog, User
from slotserve.runtime.scheduler import Scheduler
from slotserve.schemas import GpuOut
from slotserve.services import maintenance
from slotserve.services.notify import Notifier


async def _setup(make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf", size=3 * 1024 * 1024)
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    await admin.post(f"/api/models/{models['small']}/default")
    return admin, bob, models


async def _key(client) -> dict:
    return {"Authorization": f"Bearer {(await client.post('/api/keys', json={'name': 'k'})).json()['key']}"}


async def test_switch_waits_for_running_requests(monkeypatch, app, make_client, models_dir):
    monkeypatch.setenv("MOCK_TOKEN_DELAY", "0.05")
    monkeypatch.setattr(app.state.settings, "drain_seconds", 20)
    admin, bob, models = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    key = await _key(bob)

    async def stream():
        chunks = []
        async with bob.stream(
            "POST",
            "/v1/chat/completions",
            headers=key,
            json={"messages": [{"role": "user", "content": "a " * 80}], "stream": True},
        ) as resp:
            async for chunk in resp.aiter_text():
                chunks.append(chunk)
        return "".join(chunks)

    task = asyncio.create_task(stream())
    await asyncio.sleep(0.5)
    assert app.state.runtime.inflight == 1

    now = utcnow()
    r = await admin.post(
        "/api/reservations",
        json={
            "model_id": models["large"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201
    deadline = asyncio.get_running_loop().time() + 5
    while not app.state.runtime.draining and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert app.state.runtime.draining

    # admin owns the booking: new requests wait for the switch instead of hitting the old model
    admin_key = await _key(admin)
    r = await admin.post(
        "/v1/chat/completions", headers=admin_key, json={"messages": [{"role": "user", "content": "x"}]}
    )
    assert r.status_code == 503

    body = await task
    assert body.strip().endswith("data: [DONE]"), body[-200:]
    await wait_for_state(admin, "ready", "large")
    assert app.state.runtime.inflight == 0


async def test_oom_failure_is_explained(make_client, models_dir, app):
    admin, _bob, models = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    await admin.post("/api/models/scan")
    p = (await admin.post(f"/api/models/{models['large']}/presets", json={"name": "huge", "args": "--mock-oom"})).json()
    await admin.post(
        "/api/admin/runtime/manual", json={"action": "load", "model_id": models["large"], "preset_id": p["id"]}
    )
    deadline = asyncio.get_running_loop().time() + 15
    events = []
    while asyncio.get_running_loop().time() < deadline:
        events = [e for e in (await admin.get("/api/admin/stats")).json()["load_events"] if not e["success"]]
        if events:
            break
        await asyncio.sleep(0.2)
    assert events and "GPU out of memory" in events[0]["error"], events


async def test_vram_warning(monkeypatch, make_client, models_dir):
    async def tiny_gpu():
        return [
            GpuOut(index=0, name="Test GPU", memory_used_mb=0, memory_total_mb=2, utilization_pct=0, temperature_c=30)
        ]

    monkeypatch.setattr("slotserve.runtime.gpu.gpu_stats", tiny_gpu)
    admin, _bob, _models = await _setup(make_client, models_dir)
    flags = {m["name"]: m["exceeds_vram"] for m in (await admin.get("/api/models")).json()}
    assert flags == {"small": False, "large": True}


async def test_login_throttling(make_client):
    await make_client("victim")
    attacker = await make_client()
    for _ in range(10):
        r = await attacker.post("/api/auth/login", json={"username": "victim", "password": "wrong"})
        assert r.status_code == 401
    r = await attacker.post("/api/auth/login", json={"username": "victim", "password": "secret123"})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"
    # other accounts are unaffected
    await make_client("other")
    r = await attacker.post("/api/auth/login", json={"username": "other", "password": "secret123"})
    assert r.status_code == 200


async def test_manual_override_survives_restart(app, make_client, models_dir):
    admin, _bob, models = await _setup(make_client, models_dir)
    await admin.post("/api/admin/runtime/manual", json={"action": "load", "model_id": models["large"], "hours": 2})
    fresh = Scheduler(app.state.runtime, app.state.settings)
    await fresh.restore_manual()
    assert fresh.manual is not None and fresh.manual.model_id == models["large"] and fresh.manual.by == "root"
    await admin.post("/api/admin/runtime/manual", json={"action": "auto"})
    fresh = Scheduler(app.state.runtime, app.state.settings)
    await fresh.restore_manual()
    assert fresh.manual is None


async def test_outbox_retries_and_restores(app, make_client, models_dir):
    tg = app.state.fake_telegram
    admin, _bob, _models = await _setup(make_client, models_dir)
    await admin.put("/api/admin/settings", json={"telegram_bot_token": "1:x"})
    deadline = asyncio.get_running_loop().time() + 10
    while app.state.telegram.username is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalar_one()
        user.telegram_chat_id = 777
        user.locale = "en"
        await db.commit()

    tg.fail_sends = True
    app.state.notifier.emit("download_done", user_id=user.id, filename="x.gguf")
    await asyncio.sleep(0.5)
    assert not tg.texts(777)
    async with SessionLocal() as db:
        await db.execute(update(OutboxItem).values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    tg.fail_sends = False
    async with SessionLocal() as db:
        await app.state.notifier.retry_due(db)
    assert any("x.gguf" in t for t in tg.texts(777))

    # events still queued at shutdown come back after a restart
    old = Notifier(app.state.settings)
    old.emit("user_registered", username="late")
    assert await old.save_pending() == 1
    new = Notifier(app.state.settings)
    assert await new.restore_pending() == 1 and new.queue.qsize() == 1


async def test_backups(app, make_client):
    admin = await make_client("root")
    bob = await make_client("bob")
    await app.state.housekeeper.tick()
    listed = (await admin.get("/api/admin/backups")).json()
    assert any(b["kind"] == "daily" for b in listed)
    r = await admin.post("/api/admin/backups")
    assert r.status_code == 201 and r.json()["kind"] == "manual"
    data = await admin.get(f"/api/admin/backups/{r.json()['name']}")
    assert data.status_code == 200 and data.content.startswith(b"SQLite format 3")
    con = sqlite3.connect(maintenance.backup_dir(app.state.settings) / r.json()["name"])
    assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    con.close()
    assert (await bob.get("/api/admin/backups")).status_code == 403
    assert (await admin.get("/api/admin/backups/..%2Fslotserve.db")).status_code == 404


async def test_retention_rolls_up_old_usage(app, make_client):
    admin = await make_client("root")
    r = await admin.put("/api/admin/settings", json={"usage_retention_days": 30})
    assert r.json()["detail"]["code"] == "invalid_setting"
    await admin.put("/api/admin/settings", json={"usage_retention_days": 90, "metrics_token": "m"})
    uid = (await admin.get("/api/auth/me")).json()["id"]
    old = utcnow() - timedelta(days=100)
    async with SessionLocal() as db:
        for i in range(3):
            db.add(
                UsageLog(
                    user_id=uid,
                    model_name="m1",
                    endpoint="chat/completions",
                    prompt_tokens=10,
                    completion_tokens=5,
                    duration_ms=100,
                    status_code=500 if i == 0 else 200,
                    created_at=old,
                )
            )
        db.add(
            UsageLog(
                user_id=uid,
                model_name="m1",
                endpoint="chat/completions",
                prompt_tokens=1,
                completion_tokens=1,
                duration_ms=1,
                status_code=200,
                created_at=utcnow(),
            )
        )
        await db.commit()
        stats = await maintenance.apply_retention(db, app.state.settings, utcnow())
    assert stats["usage_rows"] == 3
    async with SessionLocal() as db:
        daily = await db.get(UsageDaily, (old.strftime("%Y-%m-%d"), uid, "m1"))
        assert (daily.requests, daily.failed, daily.prompt_tokens) == (3, 1, 30)

    csv = (await admin.get("/api/usage/export", params={"days": 365})).text
    assert f"{old:%Y-%m-%d},root,m1,3,30,15" in csv
    metrics = (await admin.get("/metrics", headers={"Authorization": "Bearer m"})).text
    assert 'slotserve_requests_total{user="root",model="m1",outcome="ok"} 3' in metrics
    assert 'slotserve_requests_total{user="root",model="m1",outcome="error"} 1' in metrics
