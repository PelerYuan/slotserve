from datetime import datetime, timedelta, timezone

from conftest import make_gguf


def iso(dt: datetime) -> str:
    return dt.isoformat()


def hours_from_now(h: float) -> datetime:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now + timedelta(hours=h)


async def _model_id(client, models_dir, name="tiny.gguf") -> int:
    make_gguf(models_dir, name)
    await client.post("/api/models/scan")
    models = (await client.get("/api/models")).json()
    return next(m["id"] for m in models if m["file_name"] == name)


async def book(client, model_id, start, end, note=""):
    return await client.post(
        "/api/reservations", json={"model_id": model_id, "start_at": iso(start), "end_at": iso(end), "note": note}
    )


async def test_book_and_conflict(make_client, models_dir):
    alice = await make_client("alice")
    bob = await make_client("bob")
    mid = await _model_id(alice, models_dir)

    r = await book(alice, mid, hours_from_now(2), hours_from_now(4), "training")
    assert r.status_code == 201, r.text
    assert r.json()["phase"] == "upcoming"
    assert r.json()["username"] == "alice"

    r = await book(bob, mid, hours_from_now(3), hours_from_now(5))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "reservation_conflict"
    assert r.json()["detail"]["params"]["username"] == "alice"

    # Back-to-back is fine.
    assert (await book(bob, mid, hours_from_now(4), hours_from_now(5))).status_code == 201


async def test_validation(make_client, models_dir):
    alice = await make_client("alice")
    mid = await _model_id(alice, models_dir)
    assert (await book(alice, mid, hours_from_now(-3), hours_from_now(-2))).json()["detail"]["code"] == "reservation_in_past"
    assert (await book(alice, mid, hours_from_now(1), hours_from_now(20))).json()["detail"]["code"] == "reservation_too_long"
    assert (await book(alice, mid, hours_from_now(2), hours_from_now(1))).json()["detail"]["code"] == "reservation_too_short"
    assert (await book(alice, mid, hours_from_now(24 * 40), hours_from_now(24 * 40 + 1))).json()["detail"]["code"] == "reservation_too_far"
    assert (await book(alice, 9999, hours_from_now(1), hours_from_now(2))).status_code == 404


async def test_cancel_and_release(make_client, models_dir):
    alice = await make_client("alice")
    bob = await make_client("bob")
    carol = await make_client("carol")
    mid = await _model_id(alice, models_dir)

    upcoming = (await book(bob, mid, hours_from_now(5), hours_from_now(6))).json()
    # Non-owner, non-admin cannot cancel.
    assert (await carol.delete(f"/api/reservations/{upcoming['id']}")).status_code == 403
    r = await bob.delete(f"/api/reservations/{upcoming['id']}")
    assert r.json()["phase"] == "cancelled"

    now = datetime.now(timezone.utc)
    active = (await book(bob, mid, now, now + timedelta(hours=1))).json()
    assert active["phase"] == "active"
    r = await bob.delete(f"/api/reservations/{active['id']}")
    assert r.json()["phase"] == "finished"
    assert r.json()["released_at"] is not None

    # Slot is free again immediately.
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert (await book(carol, mid, later, later + timedelta(minutes=30))).status_code == 201


async def test_admin_can_cancel_any(make_client, models_dir):
    admin = await make_client("alice")
    bob = await make_client("bob")
    mid = await _model_id(admin, models_dir)
    res = (await book(bob, mid, hours_from_now(2), hours_from_now(3))).json()
    assert (await admin.delete(f"/api/reservations/{res['id']}")).status_code == 200


async def test_list_filters(make_client, models_dir):
    alice = await make_client("alice")
    bob = await make_client("bob")
    mid = await _model_id(alice, models_dir)
    await book(alice, mid, hours_from_now(1), hours_from_now(2))
    await book(bob, mid, hours_from_now(3), hours_from_now(4))
    assert len((await alice.get("/api/reservations")).json()) == 2
    mine = (await alice.get("/api/reservations", params={"mine": True})).json()
    assert [r["username"] for r in mine] == ["alice"]
