"""Exclusive bookings, presets, edits, series, quotas, waitlist, manual control, fallback, exports."""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import httpx
from conftest import make_gguf, wait_for_state

from slotserve import db as dbmod
from slotserve.db import utcnow


def iso(dt: datetime) -> str:
    return dt.isoformat()


async def _setup(make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    await admin.post(f"/api/models/{models['small']}/default")
    return admin, bob, models


async def _key(client) -> dict:
    return {"Authorization": f"Bearer {(await client.post('/api/keys', json={'name': 'k'})).json()['key']}"}


async def _book(client, start, end, model_id=None, **extra):
    return await client.post(
        "/api/reservations", json={"model_id": model_id, "start_at": iso(start), "end_at": iso(end), **extra}
    )


def _hours(h: float) -> datetime:
    return utcnow() + timedelta(hours=h)


# ---- exclusive bookings -------------------------------------------------------


async def test_exclusive_booking_stops_llama_and_blocks_gateway(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    keys = {"root": await _key(admin), "bob": await _key(bob)}

    r = await _book(bob, utcnow(), _hours(1))
    assert r.status_code == 201, r.text
    assert r.json()[0]["exclusive"] is True and r.json()[0]["model_name"] is None
    rid = r.json()[0]["id"]
    await wait_for_state(admin, "stopped")

    r = await bob.post("/v1/chat/completions", headers=keys["bob"], json={"messages": []})
    assert r.status_code == 503 and r.json()["error"]["code"] == "gpu_exclusive"
    r = await admin.post("/v1/chat/completions", headers=keys["root"], json={"messages": []})
    assert r.status_code == 403 and r.json()["error"]["code"] == "gpu_reserved"

    await bob.delete(f"/api/reservations/{rid}")
    await wait_for_state(admin, "ready", "small")


# ---- presets ------------------------------------------------------------------


async def test_presets_and_booking_with_preset(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    r = await admin.post(f"/api/models/{models['large']}/presets", json={"name": "long", "args": "-c 32768"})
    assert r.status_code == 201
    preset_id = r.json()["id"]
    assert (await admin.post(f"/api/models/{models['large']}/presets", json={"name": "long"})).status_code == 409
    listed = {m["name"]: m for m in (await admin.get("/api/models")).json()}
    assert [p["name"] for p in listed["large"]["presets"]] == ["long"]

    # preset must belong to the model
    r = await _book(bob, _hours(1), _hours(2), models["small"], preset_id=preset_id)
    assert r.status_code == 404

    r = await _book(bob, utcnow(), _hours(1), models["large"], preset_id=preset_id)
    assert r.status_code == 201 and r.json()[0]["preset_name"] == "long"
    status = await wait_for_state(admin, "ready", "large")
    assert status["preset_name"] == "long"
    log = "\n".join(app.state.runtime.tail_log(50))
    assert "-c 32768" in log


# ---- edit, extend, series --------------------------------------------------------


async def test_edit_extend_and_locks(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    r = await _book(bob, _hours(2), _hours(3), models["large"])
    rid = r.json()[0]["id"]
    r = await bob.patch(f"/api/reservations/{rid}", json={"start_at": iso(_hours(4)), "end_at": iso(_hours(5))})
    assert r.status_code == 200 and r.json()["start_at"] > iso(_hours(3.9))
    r = await bob.patch(f"/api/reservations/{rid}", json={"model_id": None})
    assert r.json()["exclusive"] is True

    # admin books for root itself in the way, then bob cannot extend into it
    await _book(admin, _hours(5), _hours(6), models["small"])
    r = await bob.post(f"/api/reservations/{rid}/extend", json={"minutes": 30})
    assert r.json()["detail"]["code"] == "reservation_conflict"

    active = (await _book(bob, utcnow(), _hours(1), models["small"])).json()[0]
    r = await bob.post(f"/api/reservations/{active['id']}/extend", json={"minutes": 30})
    assert r.status_code == 200
    r = await bob.patch(f"/api/reservations/{active['id']}", json={"start_at": iso(_hours(0.5))})
    assert r.json()["detail"]["code"] == "reservation_active_locked"


async def test_weekly_series_all_or_nothing(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    start = _hours(24)
    await _book(admin, start + timedelta(weeks=2), start + timedelta(weeks=2, hours=1), models["small"])
    r = await _book(bob, start, start + timedelta(hours=1), models["large"], repeat_weeks=3)
    assert r.status_code == 409 and r.json()["detail"]["params"]["count"] == 1
    assert len((await bob.get("/api/reservations", params={"mine": True})).json()) == 0

    r = await _book(bob, start - timedelta(hours=2), start - timedelta(hours=1), models["large"], repeat_weeks=3)
    assert r.status_code == 201 and len(r.json()) == 4
    assert len({x["series_id"] for x in r.json()}) == 1
    first = r.json()[0]["id"]
    r = await bob.delete(f"/api/reservations/{first}", params={"series": True})
    assert len(r.json()) == 4 and all(x["phase"] == "cancelled" for x in r.json())


async def test_admin_books_for_someone_else(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    users = {u["username"]: u["id"] for u in (await admin.get("/api/admin/users")).json()}
    r = await _book(admin, _hours(2), _hours(3), models["large"], user_id=users["bob"])
    assert r.status_code == 201 and r.json()[0]["username"] == "bob"
    r = await _book(bob, _hours(4), _hours(5), models["large"], user_id=users["root"])
    assert r.status_code == 403


# ---- quotas ----------------------------------------------------------------------


async def test_quotas(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    await admin.put("/api/admin/settings", json={"quota_upcoming": 1, "quota_weekly_hours": 3})
    assert (await _book(bob, _hours(30), _hours(32), models["small"])).status_code == 201
    r = await _book(bob, _hours(40), _hours(41), models["small"])
    assert r.json()["detail"]["code"] == "quota_upcoming"
    # admins are exempt
    assert (await _book(admin, _hours(40), _hours(41), models["small"])).status_code == 201

    await admin.put("/api/admin/settings", json={"quota_upcoming": 0})
    r = await _book(bob, _hours(33), _hours(35), models["small"])
    # 2 h + 2 h in the same week exceeds 3 h (unless the week boundary separates them)
    assert r.status_code in (201, 400)
    if r.status_code == 400:
        assert r.json()["detail"]["code"] == "quota_weekly"
    assert (await admin.get("/api/config")).json()["quota_weekly_hours"] == 3


# ---- waitlist -------------------------------------------------------------------


async def test_waitlist_auto_books_freed_time(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    taken = (await _book(admin, _hours(3), _hours(5), models["small"])).json()[0]
    r = await bob.post(
        "/api/waitlist",
        json={"model_id": models["large"], "start_at": iso(_hours(3)), "end_at": iso(_hours(4)), "auto_book": True},
    )
    assert r.status_code == 201
    await app.state.housekeeper.tick()
    assert (await bob.get("/api/waitlist")).json()[0]["status"] == "waiting"

    await admin.delete(f"/api/reservations/{taken['id']}")
    await app.state.housekeeper.tick()
    mine = (await bob.get("/api/reservations", params={"mine": True})).json()
    assert len(mine) == 1 and mine[0]["model_name"] == "large"
    left = (await bob.get("/api/waitlist")).json()
    assert left == [], [(x["status"], x["reservation_id"]) for x in left]


# ---- manual control & fallback ---------------------------------------------------


async def test_manual_load_unload_and_auto(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    assert (await bob.post("/api/admin/runtime/manual", json={"action": "unload"})).status_code == 403

    r = await admin.post("/api/admin/runtime/manual", json={"action": "load", "model_id": models["large"], "hours": 1})
    assert r.status_code == 204
    await wait_for_state(admin, "ready", "large")
    status = (await admin.get("/api/status")).json()
    assert status["manual"]["model_name"] == "large" and status["reason"] == "manual"

    await admin.post("/api/admin/runtime/manual", json={"action": "unload"})
    await wait_for_state(admin, "stopped")
    await admin.post("/api/admin/runtime/manual", json={"action": "auto"})
    await wait_for_state(admin, "ready", "small")

    await _book(bob, utcnow(), _hours(1), models["large"])
    r = await admin.post("/api/admin/runtime/manual", json={"action": "unload"})
    assert r.json()["detail"]["code"] == "reservation_active"


async def test_failed_booked_model_falls_back_to_default(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    bad = (await admin.post(f"/api/models/{models['large']}/presets", json={"name": "broken", "args": "--mock-fail"})).json()
    key = await _key(bob)
    await _book(bob, utcnow(), _hours(1), models["large"], preset_id=bad["id"])
    deadline = asyncio.get_running_loop().time() + 20
    status = None
    while asyncio.get_running_loop().time() < deadline:
        status = (await admin.get("/api/status")).json()
        if status["reason"] == "fallback" and status["runtime"]["state"] == "ready":
            break
        await asyncio.sleep(0.2)
    assert status["reason"] == "fallback" and status["fallback_from"] == "large", status
    assert status["runtime"]["model_name"] == "small"
    r = await bob.post("/v1/chat/completions", headers=key, json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text


# ---- telegram -------------------------------------------------------------------


async def _wait(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_telegram_link_notify_and_buttons(app, make_client, models_dir):
    tg = app.state.fake_telegram
    admin, bob, models = await _setup(make_client, models_dir)
    assert (await bob.post("/api/telegram/link")).json()["detail"]["code"] == "telegram_unavailable"

    await admin.put("/api/admin/settings", json={"telegram_bot_token": "123:abc"})

    async def bot_ready():
        return (await bob.get("/api/telegram")).json()["bot_username"] == "slot_test_bot"

    await _wait(bot_ready)
    link = (await bob.post("/api/telegram/link")).json()["link_url"]
    token = link.rsplit("start=", 1)[1]
    await bob.put("/api/auth/preferences", json={"locale": "en"})
    tg.message(555, f"/start {token}")

    async def linked():
        return (await bob.get("/api/telegram")).json()["linked"]

    await _wait(linked)
    assert any("Linked to SlotServe account bob" in t for t in tg.texts(555))

    # admin cancels bob's booking -> bob is told
    res = (await _book(bob, _hours(2), _hours(3), models["large"])).json()[0]
    await admin.delete(f"/api/reservations/{res['id']}")

    async def told():
        return any("cancelled your booking" in t for t in tg.texts(555))

    await _wait(told)

    # running booking ending soon -> reminder with buttons -> extend via button
    active = (await _book(bob, utcnow(), utcnow() + timedelta(minutes=25), models["small"])).json()[0]
    await app.state.housekeeper.tick(utcnow() + timedelta(minutes=16))

    async def reminded():
        return any("ends at" in t for t in tg.texts(555))

    await _wait(reminded)
    tg.button(555, f"ext:{active['id']}:30")

    async def extended():
        return any(t.startswith("Extended until") for t in tg.texts(555))

    await _wait(extended)
    mine = {r["id"]: r for r in (await bob.get("/api/reservations", params={"mine": True})).json()}
    assert mine[active["id"]]["end_at"] > iso(utcnow() + timedelta(minutes=50))

    tg.message(555, "/mine")

    async def listed():
        return any("•" in t for t in tg.texts(555))

    await _wait(listed)
    tg.message(555, "/unlink")
    await _wait(lambda: _not(linked))


async def _not(pred):
    return not await pred()


# ---- calendar, metrics, export, requests, migration ----------------------------------


async def test_ical_feed(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    await _book(bob, _hours(2), _hours(3), models["large"], note="eval, run; 1")
    links = (await bob.get("/api/calendar")).json()
    anon = await make_client()
    feed = await anon.get(links["mine_url"].replace("http://test", ""))
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in feed.text and "eval\\, run\\; 1" in feed.text
    reset = (await bob.post("/api/calendar/reset")).json()
    assert (await anon.get(links["mine_url"].replace("http://test", ""))).status_code == 404
    assert (await anon.get(reset["all_url"].replace("http://test", ""))).status_code == 200


async def test_metrics_requires_token(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    anon = await make_client()
    assert (await anon.get("/metrics")).status_code == 404
    await admin.put("/api/admin/settings", json={"metrics_token": "scrape-me"})
    assert (await anon.get("/metrics")).status_code == 401
    r = await anon.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert r.status_code == 200 and 'slotserve_runtime_state{state="ready"}' in r.text


async def test_usage_export_and_request_log(make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    await wait_for_state(admin, "ready", "small")
    key = await _key(bob)
    await bob.post("/v1/chat/completions", headers=key, json={"messages": [{"role": "user", "content": "x"}]})
    await asyncio.sleep(0.3)
    r = await bob.get("/api/usage/export", params={"kind": "requests"})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿time,user") and ",bob," in r.text
    r = await bob.get("/api/usage/export")
    assert r.text.splitlines()[0] == "﻿date,user,model,requests,prompt_tokens,completion_tokens"
    log = (await admin.get("/api/admin/requests")).json()
    assert log["total"] == 1 and log["items"][0]["username"] == "bob" and log["items"][0]["key_name"] == "k"


async def test_hf_listing(monkeypatch, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    tree = [
        {"type": "file", "path": "README.md", "size": 10},
        {"type": "file", "path": "m-Q4_K_M.gguf", "size": 5, "lfs": {"size": 4_000_000_000}},
        {"type": "file", "path": "big/m-Q8_0-00002-of-00002.gguf", "size": 1, "lfs": {"size": 2}},
        {"type": "file", "path": "big/m-Q8_0-00001-of-00002.gguf", "size": 1, "lfs": {"size": 3}},
        {"type": "file", "path": "mmproj-f16.gguf", "size": 7},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/models/org/m-GGUF/tree/main"
        return httpx.Response(200, json=tree)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "slotserve.api.downloads.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    r = await bob.get("/api/downloads/hf/files", params={"repo": "org/m-GGUF"})
    assert r.status_code == 200, r.text
    files = {f["path"]: f for f in r.json()["files"]}
    split = files["big/m-Q8_0-00001-of-00002.gguf"]
    assert split["parts"] == 2 and split["size_bytes"] == 5 and split["files"][-1].endswith("00001-of-00002.gguf")
    assert files["mmproj-f16.gguf"]["kind"] == "mmproj"
    assert r.json()["files"][-1]["kind"] == "mmproj"

    r = await bob.post("/api/downloads/hf", json={"repo": "org/m-GGUF", "files": split["files"]})
    assert r.status_code == 201
    assert [t["filename"] for t in r.json()] == ["m-Q8_0-00002-of-00002.gguf", "m-Q8_0-00001-of-00002.gguf"]
    assert r.json()[0]["url"] == "https://huggingface.co/org/m-GGUF/resolve/main/big/m-Q8_0-00002-of-00002.gguf"
    bad = await bob.post("/api/downloads/hf", json={"repo": "org/m-GGUF", "files": ["../etc/passwd.gguf"]})
    assert bad.status_code == 400


def test_migration_from_v02_schema(tmp_path):
    db_path = tmp_path / "slotserve.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(32), password_hash VARCHAR(255),
                            is_admin BOOLEAN, created_at DATETIME, disabled_at DATETIME);
        CREATE TABLE models (id INTEGER PRIMARY KEY, name VARCHAR(128));
        CREATE TABLE reservations (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            model_id INTEGER NOT NULL REFERENCES models (id) ON DELETE RESTRICT,
            start_at DATETIME NOT NULL, end_at DATETIME NOT NULL, status VARCHAR(16) NOT NULL,
            note TEXT NOT NULL, released_at DATETIME, created_at DATETIME NOT NULL);
        CREATE TABLE usage_logs (id INTEGER PRIMARY KEY,
            reservation_id INTEGER REFERENCES reservations (id) ON DELETE SET NULL);
        INSERT INTO users VALUES (1, 'a', 'x', 1, '2026-01-01 00:00:00', NULL);
        INSERT INTO models VALUES (1, 'm');
        INSERT INTO reservations VALUES (7, 1, 1, '2026-01-01 01:00:00', '2026-01-01 02:00:00', 'booked', 'n',
                                         NULL, '2026-01-01 00:00:00');
        INSERT INTO usage_logs VALUES (1, 7);
        """
    )
    con.commit()
    con.close()

    assert dbmod.prepare(db_path, tmp_path / "backups") == 0
    assert len(list((tmp_path / "backups").glob("slotserve-v0-*.db"))) == 1
    con = sqlite3.connect(db_path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(reservations)")}
    assert cols["model_id"][3] == 0 and "series_id" in cols and "preset_id" in cols
    assert con.execute("SELECT id, note FROM reservations").fetchall() == [(7, "n")]
    assert con.execute("SELECT reservation_id FROM usage_logs").fetchone()[0] == 7  # FK not cascaded
    con.close()
    assert dbmod.prepare(db_path, tmp_path / "backups") is None  # idempotent


async def test_preferences_and_registration_alert(app, make_client, models_dir):
    admin = await make_client("root")
    r = await admin.put("/api/auth/preferences", json={"locale": "zh-CN", "notify_prefs": {"reminders": False}})
    assert r.json()["locale"] == "zh-CN"
    assert (await admin.get("/api/telegram")).json()["notify_prefs"]["reminders"] is False
    await make_client("newbie")
    await asyncio.sleep(0.1)
    # not linked, so nothing is sent - but the event must not crash the notifier
    assert app.state.notifier.queue.qsize() == 0

