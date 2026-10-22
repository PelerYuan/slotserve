"""F01 / F05: the model availability badge in /api/status (one place decides it) and the newcomer state."""

import asyncio
from datetime import timedelta

from sqlalchemy import update

from conftest import make_gguf, wait_for_state
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import LlmModel, User

HELLO = {"messages": [{"role": "user", "content": "hi"}]}


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


def _badges(status: dict) -> dict[str, dict]:
    return {m["name"]: m for m in status["models"]}


async def test_three_states_follow_the_gateway(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    badges = _badges((await bob.get("/api/status")).json())
    assert badges["small"]["state"] == "now" and not badges["small"]["loading"]
    assert badges["small"]["is_default"] and badges["small"]["lane_id"] is not None
    assert badges["large"]["state"] == "booking"
    # what the badge says is what the gateway does
    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 200
    r = await bob.post("/api/playground/chat", json={**HELLO, "model": "large"})
    assert r.status_code == 409

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
    badges = _badges((await bob.get("/api/status")).json())
    assert badges["small"]["state"] == "busy"
    assert badges["small"]["holder"] == "alice" and badges["small"]["until"] is not None
    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 403
    # for the person who booked it, it stays callable
    mine = _badges((await admin.get("/api/status")).json())
    assert mine["small"]["state"] == "now"


async def test_own_booking_of_another_model_is_now_while_it_loads(app, make_client, models_dir):
    admin, bob, models = await _setup(make_client, models_dir)
    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "model_id": models["large"],
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    badges = _badges((await bob.get("/api/status")).json())
    assert badges["large"]["state"] == "now"
    # small is not answered on bob's booked lane any more
    assert badges["small"]["state"] == "booking"
    await wait_for_state(bob, "ready", "large")
    badges = _badges((await bob.get("/api/status")).json())
    assert badges["large"] == {**badges["large"], "state": "now", "loading": False}
    assert _badges((await admin.get("/api/status")).json())["large"]["holder"] == "bob"


async def test_args_waiting_for_approval_are_flagged(app, make_client, models_dir):
    _admin, bob, models = await _setup(make_client, models_dir)
    assert _badges((await bob.get("/api/status")).json())["large"]["args_pending"] is False
    async with SessionLocal() as db:
        await db.execute(update(LlmModel).where(LlmModel.id == models["large"]).values(args_pending="--mlock"))
        await db.commit()
    badge = _badges((await bob.get("/api/status")).json())["large"]
    assert badge["args_pending"] is True and badge["state"] == "booking"


async def test_start_here_until_goals_done_or_dismissed(app, make_client, models_dir):
    admin, bob, _models = await _setup(make_client, models_dir)
    state = (await bob.get("/api/me/onboarding")).json()
    assert state == {"start": True, "done": {"call": False, "env": False, "book": False}, "seen": []}

    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 200
    await asyncio.sleep(0.3)  # usage rows are written in the background
    state = (await bob.get("/api/me/onboarding")).json()
    assert state["done"]["call"] is True and state["start"] is True

    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "model_id": None,
            "start_at": (now + timedelta(days=1)).isoformat(),
            "end_at": (now + timedelta(days=1, hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    state = (await bob.get("/api/me/onboarding")).json()
    # dev environments are off here, so calling and booking are all there is to do
    assert state["done"] == {"call": True, "env": False, "book": True} and state["start"] is False
    await admin.put("/api/admin/settings", json={"devenv_enabled": True})
    assert (await bob.get("/api/me/onboarding")).json()["start"] is True

    state = (await bob.post("/api/me/onboarding", json={"seen": ["playground_first", "playground_first"]})).json()
    assert state["seen"] == ["playground_first"]
    state = (await bob.post("/api/me/onboarding", json={"dismiss_start": True})).json()
    assert state["start"] is False
    # kept on the server: another device (a new session) sees the same
    again = await make_client()
    await again.post("/api/auth/login", json={"username": "bob", "password": "secret123"})
    assert (await again.get("/api/me/onboarding")).json() == state

    r = await bob.post("/api/me/onboarding", json={"seen": ["x"] * 21})
    assert r.status_code == 422


async def test_people_who_used_it_before_do_not_get_start_here(app, make_client, models_dir):
    _admin, bob, _models = await _setup(make_client, models_dir)
    assert (await bob.post("/api/playground/chat", json=HELLO)).status_code == 200
    await asyncio.sleep(0.3)
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.username == "bob").values(created_at=utcnow() - timedelta(days=30)))
        await db.commit()
    assert (await bob.get("/api/me/onboarding")).json()["start"] is False
    # a brand-new account that already made a call still gets it
    carol = await make_client("carol")
    assert (await carol.post("/api/playground/chat", json=HELLO)).status_code == 200
    await asyncio.sleep(0.3)
    assert (await carol.get("/api/me/onboarding")).json()["start"] is True


async def test_onboarding_needs_a_session(app, make_client):
    anon = await make_client()
    assert (await anon.get("/api/me/onboarding")).status_code == 401
