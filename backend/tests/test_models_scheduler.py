from datetime import timedelta
from pathlib import Path

from conftest import make_gguf
from slotserve.config import get_settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import LlmModel, Reservation
from slotserve.runtime.manager import strip_managed_flags
from slotserve.runtime.scheduler import compute_target
from slotserve.services import queries, scanner


def test_split_and_mmproj_detection():
    assert scanner.is_primary_gguf(Path("qwen-q4.gguf"))
    assert scanner.is_primary_gguf(Path("big-00001-of-00003.gguf"))
    assert not scanner.is_primary_gguf(Path("big-00002-of-00003.gguf"))
    assert not scanner.is_primary_gguf(Path("mmproj-model-f16.gguf"))
    assert not scanner.is_primary_gguf(Path("notes.txt"))
    assert scanner.model_name_for(Path("big-00001-of-00003.gguf")) == "big"


def test_strip_managed_flags():
    args = ["-ngl", "99", "-m", "/x.gguf", "--port=9", "--alias", "a", "-c", "4096"]
    assert strip_managed_flags(args) == ["-ngl", "99", "-c", "4096"]


async def test_scan_and_edit(make_client, models_dir):
    admin = await make_client("alice")
    bob = await make_client("bob")
    make_gguf(models_dir, "a.gguf", 2048)
    make_gguf(models_dir, "sub/big-00001-of-00002.gguf", 1000)
    make_gguf(models_dir, "sub/big-00002-of-00002.gguf", 500)
    make_gguf(models_dir, "sub/mmproj-f16.gguf")

    r = (await admin.post("/api/models/scan")).json()
    assert sorted(r["added"]) == ["a", "big"]
    assert (await admin.post("/api/models/scan")).json() == {"added": [], "skipped": 2}

    models = {m["name"]: m for m in (await admin.get("/api/models")).json()}
    assert models["big"]["size_bytes"] == 1500
    assert models["a"]["args"] == get_settings().default_llama_args

    mid = models["a"]["id"]
    r = await admin.patch(f"/api/models/{mid}", json={"name": "alpha", "args": "-ngl 99 -c 4096 --embedding"})
    assert r.status_code == 200 and r.json()["name"] == "alpha"
    assert (await admin.patch(f"/api/models/{mid}", json={"name": "big"})).status_code == 409
    # Bob registered nothing, so he cannot edit; only admins set the default.
    assert (await bob.patch(f"/api/models/{mid}", json={"args": "x"})).status_code == 403
    assert (await bob.post(f"/api/models/{mid}/default")).status_code == 403
    assert (await admin.post(f"/api/models/{mid}/default")).json()["is_default"] is True


async def test_delete_model_guards(make_client, models_dir):
    admin = await make_client("alice")
    make_gguf(models_dir, "a.gguf")
    await admin.post("/api/models/scan")
    mid = (await admin.get("/api/models")).json()[0]["id"]
    start = utcnow() + timedelta(hours=2)
    await admin.post(
        "/api/reservations",
        json={"model_id": mid, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()},
    )
    r = await admin.delete(f"/api/models/{mid}")
    assert r.json()["detail"]["code"] == "model_has_reservations"


async def _seed(names_sizes):
    async with SessionLocal() as db:
        models = []
        for name, size, default in names_sizes:
            m = LlmModel(name=name, file_path=f"/m/{name}.gguf", size_bytes=size, is_default=default)
            db.add(m)
            models.append(m)
        await db.flush()
        lane = await queries.primary_lane(db)
        lane.default_model_id = next((m.id for m in models if m.is_default), lane.default_model_id)
        await db.commit()
        return [m.id for m in models]


async def _reserve(model_id, start, end):
    from slotserve.orm import User

    async with SessionLocal() as db:
        user = User(username=f"u{start.timestamp()}", password_hash="x")
        db.add(user)
        await db.flush()
        db.add(Reservation(user_id=user.id, model_id=model_id, start_at=start, end_at=end))
        await db.commit()


async def test_compute_target_rules(app):
    settings = app.state.settings
    # default loads in ~15s + 150MB/150MBps = 16s; big in 15 + 300 = 315s
    default_id, big_id = await _seed([("default", 150_000_000, True), ("big", 45_000_000_000, False)])
    now = utcnow()

    async with SessionLocal() as db:
        t = await compute_target(db, settings, now, None)
        assert t.model.id == default_id and t.reason == "default"

    # Reservation far away -> still default.
    await _reserve(big_id, now + timedelta(hours=3), now + timedelta(hours=4))
    async with SessionLocal() as db:
        assert (await compute_target(db, settings, now, (default_id, None))).model.id == default_id
        # 5 minutes before start (< 315s + 60s buffer) -> preload.
        t = await compute_target(db, settings, now + timedelta(hours=3) - timedelta(minutes=5), (default_id, None))
        assert t.model.id == big_id and t.reason == "preload"
        # During the reservation -> reserved model.
        t = await compute_target(db, settings, now + timedelta(hours=3, minutes=10), (big_id, None))
        assert t.model.id == big_id and t.reason == "reservation"
        # Just after -> default again.
        t = await compute_target(db, settings, now + timedelta(hours=4, minutes=1), (big_id, None))
        assert t.model.id == default_id


async def test_compute_target_skips_short_default_window(app):
    settings = app.state.settings
    _default_id, big_id = await _seed([("default", 15_000_000_000, True), ("big", 45_000_000_000, False)])
    now = utcnow()
    # big[0..1h], gap 8 minutes, big again. Loading default (115s) then big (315s) does not fit well.
    await _reserve(big_id, now, now + timedelta(hours=1))
    await _reserve(big_id, now + timedelta(hours=1, minutes=8), now + timedelta(hours=2))
    async with SessionLocal() as db:
        t = await compute_target(db, settings, now + timedelta(hours=1, seconds=10), (big_id, None))
        assert t.model.id == big_id and t.reason == "preload-early"


async def test_minimum_preload_lead(app, monkeypatch):
    settings = app.state.settings
    # "fast" loads in ~16 s, so without a minimum it would only start ~76 s before the booking.
    default_id, fast_id = await _seed([("base", 150_000_000, True), ("fast", 150_000_000, False)])
    now = utcnow()
    await _reserve(fast_id, now + timedelta(hours=2), now + timedelta(hours=3))
    start = now + timedelta(hours=2)
    monkeypatch.setattr(settings, "preload_min_minutes", 10)
    async with SessionLocal() as db:
        t = await compute_target(db, settings, start - timedelta(minutes=11), (default_id, None))
        assert t.model.id == default_id
        t = await compute_target(db, settings, start - timedelta(minutes=9), (default_id, None))
        assert t.model.id == fast_id and t.reason == "preload"


async def test_runtime_settings_are_editable(make_client):
    admin = await make_client("root")
    view = {s["key"]: s for s in (await admin.get("/api/admin/settings")).json()}
    assert view["disk_read_mbps"]["kind"] == "float" and view["disk_read_mbps"]["unit"] == "MB/s"
    assert view["slot_minutes"]["choices"] == [5, 10, 15, 20, 30, 60]
    r = await admin.put(
        "/api/admin/settings", json={"disk_read_mbps": 480.5, "slot_minutes": 15, "preload_min_minutes": 10}
    )
    assert r.status_code == 200, r.text
    view = {s["key"]: s for s in r.json()}
    assert view["disk_read_mbps"]["value"] == 480.5 and view["preload_min_minutes"]["value"] == 10
    assert (await admin.get("/api/config")).json()["slot_minutes"] == 15
    r = await admin.put("/api/admin/settings", json={"slot_minutes": 7})
    assert r.json()["detail"]["code"] == "invalid_setting"
    r = await admin.put("/api/admin/settings", json={"load_timeout_seconds": 5})
    assert r.json()["detail"]["code"] == "invalid_setting"
