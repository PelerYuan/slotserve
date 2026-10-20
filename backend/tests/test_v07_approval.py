"""Approval mode: pending bookings, review, expiry and notifications."""

import asyncio
from datetime import timedelta

from conftest import make_gguf, wait_for_state
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import User


async def _setup(make_client, models_dir, mode="long", hours=2):
    admin = await make_client("root")
    bob = await make_client("bob")
    alice = await make_client("alice")
    make_gguf(models_dir, "small.gguf")
    make_gguf(models_dir, "large.gguf")
    await admin.post("/api/models/scan")
    models = {m["name"]: m["id"] for m in (await admin.get("/api/models")).json()}
    await admin.post(f"/api/models/{models['small']}/default")
    await admin.put("/api/admin/settings", json={"approval_mode": mode, "approval_hours": hours})
    return admin, bob, alice, models


def _h(hours: float):
    return (utcnow() + timedelta(hours=hours)).isoformat()


async def _book(client, start, end, model_id, **extra):
    return await client.post(
        "/api/reservations", json={"model_id": model_id, "start_at": start, "end_at": end, **extra}
    )


async def test_long_bookings_wait_for_approval(make_client, models_dir):
    admin, bob, alice, models = await _setup(make_client, models_dir)
    assert (await admin.get("/api/config")).json()["approval_mode"] == "long"

    short = (await _book(bob, _h(1), _h(2), models["large"])).json()[0]
    assert short["status"] == "booked"
    long = (await _book(bob, _h(3), _h(6), models["large"])).json()[0]
    assert long["status"] == "pending" and long["phase"] == "pending"

    # pending time is held: nobody else can book over it
    r = await _book(alice, _h(4), _h(5), models["small"])
    assert r.json()["detail"]["code"] == "reservation_conflict"

    # admins' own long bookings need no approval
    mine = (await _book(admin, _h(10), _h(14), models["small"])).json()[0]
    assert mine["status"] == "booked"

    listed = (await admin.get("/api/admin/reservations", params={"phase": "pending"})).json()
    assert [x["id"] for x in listed] == [long["id"]]
    assert (await admin.get("/api/admin/stats")).json()["pending_reservations"] == 1
    assert (await bob.post(f"/api/admin/reservations/{long['id']}/approve", json={})).status_code == 403

    r = await admin.post(f"/api/admin/reservations/{long['id']}/approve", json={})
    assert r.status_code == 200 and r.json()[0]["status"] == "booked" and r.json()[0]["reviewed_by"] == "root"
    r = await admin.post(f"/api/admin/reservations/{long['id']}/approve", json={})
    assert r.json()["detail"]["code"] == "not_pending"


async def test_reject_frees_time_and_edit_requires_review_again(make_client, models_dir):
    admin, bob, alice, models = await _setup(make_client, models_dir, mode="all")
    r = (await _book(bob, _h(3), _h(4), models["large"])).json()[0]
    assert r["status"] == "pending"
    rej = await admin.post(f"/api/admin/reservations/{r['id']}/reject", json={"note": "GPU maintenance"})
    assert rej.json()[0]["phase"] == "rejected" and rej.json()[0]["review_note"] == "GPU maintenance"
    assert (await _book(alice, _h(3), _h(4), models["small"])).json()[0]["status"] == "pending"

    # an approved booking edited by its owner goes back to review
    b = (await _book(bob, _h(8), _h(9), models["large"])).json()[0]
    await admin.post(f"/api/admin/reservations/{b['id']}/approve", json={})
    r = await bob.patch(f"/api/reservations/{b['id']}", json={"end_at": _h(9.5)})
    assert r.json()["status"] == "pending"


async def test_series_review(make_client, models_dir):
    admin, bob, _alice, models = await _setup(make_client, models_dir, mode="all")
    created = (await _book(bob, _h(30), _h(31), models["large"], repeat_weeks=2)).json()
    assert len(created) == 3 and {c["status"] for c in created} == {"pending"}
    done = await admin.post(f"/api/admin/reservations/{created[0]['id']}/approve", json={"series": True})
    assert len(done.json()) == 3 and {c["status"] for c in done.json()} == {"booked"}


async def test_pending_does_not_load_model_and_expires(app, make_client, models_dir):
    admin, bob, _alice, models = await _setup(make_client, models_dir, mode="all")
    await wait_for_state(admin, "ready", "small")
    now = utcnow()
    r = (await _book(bob, now.isoformat(), (now + timedelta(hours=1)).isoformat(), models["large"])).json()[0]
    assert r["status"] == "pending"
    await asyncio.sleep(1)
    status = (await admin.get("/api/status")).json()
    assert status["runtime"]["model_name"] == "small" and status["active_reservation"] is None

    await app.state.housekeeper.tick()
    mine = (await bob.get("/api/reservations", params={"mine": True, "include_cancelled": True})).json()
    assert [x["phase"] for x in mine if x["id"] == r["id"]] == ["expired"]


async def test_telegram_review_buttons(app, make_client, models_dir):
    tg = app.state.fake_telegram
    admin, bob, _alice, models = await _setup(make_client, models_dir, mode="all")
    await admin.put("/api/admin/settings", json={"telegram_bot_token": "1:x"})
    async with SessionLocal() as db:
        from sqlalchemy import select

        for name, chat in (("root", 901), ("bob", 902)):
            u = (await db.execute(select(User).where(User.username == name))).scalar_one()
            u.telegram_chat_id, u.locale = chat, "en"
        await db.commit()
    deadline = asyncio.get_running_loop().time() + 10
    while app.state.telegram.username is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    r = (await _book(bob, _h(5), _h(6), models["large"])).json()[0]
    while not any("asks for" in t for t in tg.texts(901)) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert any("asks for" in t for t in tg.texts(901))
    tg.button(901, f"apr:{r['id']}")
    while not any("was approved" in t for t in tg.texts(902)) and asyncio.get_running_loop().time() < deadline + 10:
        await asyncio.sleep(0.05)
    assert any("was approved" in t for t in tg.texts(902))
