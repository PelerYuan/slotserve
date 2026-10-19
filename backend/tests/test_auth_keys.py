from conftest import INVITE


async def test_register_requires_invite(make_client):
    c = await make_client()
    r = await c.post("/api/auth/register", json={"username": "alice", "password": "secret123", "invite_code": "nope"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "invalid_invite_code"


async def test_first_user_is_admin(make_client):
    alice = await make_client("alice")
    bob = await make_client("bob")
    assert (await alice.get("/api/auth/me")).json()["is_admin"] is True
    assert (await bob.get("/api/auth/me")).json()["is_admin"] is False


async def test_duplicate_username_case_insensitive(make_client):
    await make_client("alice")
    c = await make_client()
    r = await c.post("/api/auth/register", json={"username": "ALICE", "password": "secret123", "invite_code": INVITE})
    assert r.status_code == 409


async def test_login_logout(make_client):
    await make_client("alice")
    c = await make_client()
    assert (await c.post("/api/auth/login", json={"username": "alice", "password": "wrong"})).status_code == 401
    assert (await c.post("/api/auth/login", json={"username": "alice", "password": "secret123"})).status_code == 200
    assert (await c.get("/api/auth/me")).status_code == 200
    await c.post("/api/auth/logout")
    assert (await c.get("/api/auth/me")).status_code == 401


async def test_password_change(make_client):
    c = await make_client("alice")
    r = await c.put("/api/auth/password", json={"old_password": "secret123", "new_password": "another123"})
    assert r.status_code == 204
    fresh = await make_client()
    assert (
        await fresh.post("/api/auth/login", json={"username": "alice", "password": "another123"})
    ).status_code == 200


async def test_api_key_lifecycle(make_client):
    c = await make_client("alice")
    created = (await c.post("/api/keys", json={"name": "laptop"})).json()
    assert created["key"].startswith("sk-ss-")
    assert created["key"].startswith(created["prefix"])

    keys = (await c.get("/api/keys")).json()
    assert [k["name"] for k in keys] == ["laptop"]
    assert "key" not in keys[0]

    auth = {"Authorization": f"Bearer {created['key']}"}
    assert (await c.get("/v1/models", headers=auth)).status_code == 200

    assert (await c.delete(f"/api/keys/{created['id']}")).status_code == 204
    assert (await c.get("/api/keys")).json() == []
    r = await c.get("/v1/models", headers=auth)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


async def test_cannot_revoke_others_key(make_client):
    alice = await make_client("alice")
    bob = await make_client("bob")
    key = (await alice.post("/api/keys", json={"name": "k"})).json()
    assert (await bob.delete(f"/api/keys/{key['id']}")).status_code == 404


async def test_requires_login(make_client):
    c = await make_client()
    for path in ("/api/models", "/api/keys", "/api/status", "/api/reservations", "/api/usage"):
        assert (await c.get(path)).status_code == 401, path
