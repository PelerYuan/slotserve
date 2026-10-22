from datetime import timedelta

from conftest import INVITE, make_gguf
from slotserve.db import utcnow


async def _admin_and_user(make_client):
    admin = await make_client("root")  # first account becomes admin
    bob = await make_client("bob")
    users = (await admin.get("/api/admin/users")).json()
    return admin, bob, {u["username"]: u for u in users}


async def test_admin_routes_require_admin(make_client):
    _, bob, _ = await _admin_and_user(make_client)
    for path in ("/api/admin/stats", "/api/admin/users", "/api/admin/keys", "/api/admin/settings", "/api/admin/audit"):
        r = await bob.get(path)
        assert r.status_code == 403, path
        assert r.json()["detail"]["code"] == "admin_required"


async def test_create_reset_and_delete_user(make_client):
    admin, _, _ = await _admin_and_user(make_client)
    r = await admin.post("/api/admin/users", json={"username": "carol", "password": "carol-pass"})
    assert r.status_code == 201 and r.json()["is_admin"] is False
    carol_id = r.json()["id"]

    carol = await make_client()
    r = await carol.post("/api/auth/login", json={"username": "carol", "password": "carol-pass"})
    assert r.status_code == 200
    r = await admin.post(f"/api/admin/users/{carol_id}/password", json={"password": "new-pass"})
    assert r.status_code == 204
    r = await carol.post("/api/auth/login", json={"username": "carol", "password": "carol-pass"})
    assert r.status_code == 401
    r = await carol.post("/api/auth/login", json={"username": "carol", "password": "new-pass"})
    assert r.status_code == 200

    assert (await admin.delete(f"/api/admin/users/{carol_id}")).status_code == 204
    assert (await carol.get("/api/auth/me")).status_code == 401
    names = [u["username"] for u in (await admin.get("/api/admin/users")).json()]
    assert "carol" not in names

    actions = [e["action"] for e in (await admin.get("/api/admin/audit?action=user.*")).json()["items"]]
    assert actions[:3] == ["user.delete", "user.reset_password", "user.create"]


async def test_disable_blocks_web_and_api_and_ends_bookings(make_client, models_dir):
    admin, bob, users = await _admin_and_user(make_client)
    make_gguf(models_dir, "m.gguf")
    await admin.post("/api/models/scan")
    model_id = (await admin.get("/api/models")).json()[0]["id"]
    key = (await bob.post("/api/keys", json={"name": "k"})).json()["key"]
    start = utcnow() + timedelta(days=1)
    r = await bob.post(
        "/api/reservations",
        json={"model_id": model_id, "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat()},
    )
    assert r.status_code == 201
    booking_id = r.json()[0]["id"]

    r = await admin.patch(f"/api/admin/users/{users['bob']['id']}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled_at"] is not None
    assert (await bob.get("/api/auth/me")).status_code == 401
    r = await bob.post("/api/auth/login", json={"username": "bob", "password": "secret123"})
    assert r.json()["detail"]["code"] == "account_disabled"
    r = await bob.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={"messages": []})
    assert r.status_code == 401

    cancelled = (await admin.get("/api/admin/reservations", params={"phase": "cancelled"})).json()
    assert [x["id"] for x in cancelled] == [booking_id]

    await admin.patch(f"/api/admin/users/{users['bob']['id']}", json={"disabled": False})
    r = await bob.post("/api/auth/login", json={"username": "bob", "password": "secret123"})
    assert r.status_code == 200


async def test_self_protection_and_admin_toggle(make_client):
    admin, _, users = await _admin_and_user(make_client)
    root_id, bob_id = users["root"]["id"], users["bob"]["id"]
    r = await admin.patch(f"/api/admin/users/{root_id}", json={"disabled": True})
    assert r.json()["detail"]["code"] == "cannot_modify_self"
    assert (await admin.delete(f"/api/admin/users/{root_id}")).status_code == 400

    r = await admin.patch(f"/api/admin/users/{bob_id}", json={"is_admin": True})
    assert r.json()["is_admin"] is True
    r = await admin.patch(f"/api/admin/users/{bob_id}", json={"is_admin": False})
    assert r.json()["is_admin"] is False


async def test_admin_keys_listing_and_revoke(make_client):
    admin, bob, _ = await _admin_and_user(make_client)
    raw = (await bob.post("/api/keys", json={"name": "laptop"})).json()["key"]
    keys = (await admin.get("/api/admin/keys")).json()
    assert [(k["username"], k["name"]) for k in keys] == [("bob", "laptop")]
    assert "key" not in keys[0]
    assert (await admin.delete(f"/api/admin/keys/{keys[0]['id']}")).status_code == 204
    r = await bob.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401


async def test_settings_override_and_reset(make_client):
    admin, _, _ = await _admin_and_user(make_client)
    r = await admin.put("/api/admin/settings", json={"registration_open": False, "max_reservation_hours": 2})
    assert r.status_code == 200
    view = {s["key"]: s for s in r.json()}
    assert view["registration_open"]["value"] is False and view["registration_open"]["overridden"]
    assert (await admin.get("/api/config")).json()["max_reservation_hours"] == 2

    newcomer = await make_client()
    body = {"username": "eve", "password": "secret123", "invite_code": INVITE}
    r = await newcomer.post("/api/auth/register", json=body)
    assert r.json()["detail"]["code"] == "registration_closed"

    r = await admin.put("/api/admin/settings", json={"max_reservation_hours": 0})
    assert r.json()["detail"]["code"] == "invalid_setting"

    r = await admin.put("/api/admin/settings", json={"registration_open": None, "invite_code": "fresh-code"})
    view = {s["key"]: s for s in r.json()}
    assert view["registration_open"]["value"] is True and not view["registration_open"]["overridden"]
    r = await newcomer.post("/api/auth/register", json=body)
    assert r.json()["detail"]["code"] == "invalid_invite_code"
    r = await newcomer.post("/api/auth/register", json={**body, "invite_code": "fresh-code"})
    assert r.status_code == 201

    items = (await admin.get("/api/admin/audit")).json()["items"]
    assert items[0]["action"] == "auth.register" and items[0]["detail"]["method"] == "invite_code"
    audit = items[1]
    assert audit["action"] == "settings.update" and audit["detail"]["after"]["invite_code"].startswith("fp:")


async def test_stats(make_client):
    admin, _, _ = await _admin_and_user(make_client)
    s = (await admin.get("/api/admin/stats")).json()
    assert s["users"] == 2 and s["admins"] == 1 and s["requests_24h"] == 0
