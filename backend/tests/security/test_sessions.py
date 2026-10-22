"""R1-S3: server-side sessions. A password change signs out the other browsers, sudo mode survives in the stored
session, sign-in gets a fresh token, and a request may not carry the cookie together with a bearer token."""

import contextlib
import sqlite3

import pytest
from security_helpers import PASSWORD, client, register, register_admin, running_app

from slotserve.security import sessions as sess


@pytest.fixture
async def app(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as application:
        yield application


def session_rows(app) -> list[tuple]:
    con = sqlite3.connect(app.state.settings.data_dir / "slotserve.db")
    try:
        return con.execute("SELECT id, user_id, token_hash, revoked_at, revoked_reason FROM sessions").fetchall()
    finally:
        con.close()


@contextlib.asynccontextmanager
async def login(app, username: str, password: str = PASSWORD):
    async with client(app) as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        yield c


async def test_cookie_is_a_random_token_stored_as_hash(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        token = admin.cookies.get("slotserve_session")
        assert token and "." not in token  # not a signed payload
        rows = session_rows(app)
        assert len(rows) == 1 and rows[0][2] == sess._hash(token) and rows[0][3] is None


async def test_password_change_signs_out_other_browsers(app):
    async with client(app) as admin:
        await register_admin(app, admin)
    async with login(app, "root") as a, login(app, "root") as b:
        assert (await b.get("/api/auth/me")).status_code == 200
        r = await a.put("/api/auth/password", json={"old_password": PASSWORD, "new_password": "another123"})
        assert r.status_code == 204
        assert (await a.get("/api/auth/me")).status_code == 200  # this browser stays signed in
        assert (await b.get("/api/auth/me")).status_code == 401
        reasons = {row[4] for row in session_rows(app) if row[3] is not None}
        assert "password" in reasons


async def test_sudo_survives_in_the_stored_session(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        assert (await admin.get("/api/auth/sudo")).json()["active"] is True  # granted at sign-up
        sess._cache.clear()  # read back from the database, not the cache
        assert (await admin.get("/api/auth/sudo")).json()["active"] is True
        await admin.post("/api/auth/logout")
        assert (await admin.get("/api/auth/me")).status_code == 401


async def test_sign_in_gets_a_new_token(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        before = admin.cookies.get("slotserve_session")
        r = await admin.post("/api/auth/login", json={"username": "root", "password": PASSWORD})
        assert r.status_code == 200
        after = admin.cookies.get("slotserve_session")
        assert after and after != before
        replaced = [row for row in session_rows(app) if row[2] == sess._hash(before)]
        assert replaced[0][4] == "replaced"


async def test_logout_ends_the_session_on_the_server(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        token = admin.cookies.get("slotserve_session")
        await admin.post("/api/auth/logout")
    async with client(app, cookies={"slotserve_session": token}) as replay:
        assert (await replay.get("/api/auth/me")).status_code == 401


async def test_list_and_end_sessions(app):
    async with client(app) as admin:
        await register_admin(app, admin)
    async with login(app, "root") as a, login(app, "root") as b:
        listed = (await a.get("/api/auth/sessions")).json()
        assert len(listed) == 3 and sum(s["current"] for s in listed) == 1  # sign-up, a, b
        b_id = (await b.get("/api/auth/sessions")).json()
        other = next(s for s in b_id if s["current"])
        assert set(other) >= {"id", "created_at", "last_seen_at", "expires_at", "ip", "user_agent"}
        assert (await a.delete(f"/api/auth/sessions/{other['id']}")).status_code == 204
        assert (await b.get("/api/auth/me")).status_code == 401
        assert (await a.delete(f"/api/auth/sessions/{other['id']}")).status_code == 404
        async with login(app, "root") as c:
            assert (await a.post("/api/auth/sessions/revoke-others")).json() == {"revoked": 2}
            assert (await c.get("/api/auth/me")).status_code == 401
        assert (await a.get("/api/auth/me")).status_code == 200


async def test_admin_disable_and_reset_sign_the_person_out(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        async with client(app) as bob:
            assert (await register(bob, "bob", invite_code="sec-invite")).status_code == 201
            bob_id = (await bob.get("/api/auth/me")).json()["id"]
            r = await admin.post(f"/api/admin/users/{bob_id}/password", json={"password": "newpass123"})
            assert r.status_code == 204
            assert (await bob.get("/api/auth/me")).status_code == 401
        async with login(app, "bob", "newpass123") as bob2:
            assert (await admin.patch(f"/api/admin/users/{bob_id}", json={"disabled": True})).status_code == 200
            assert (await admin.patch(f"/api/admin/users/{bob_id}", json={"disabled": False})).status_code == 200
            assert (await bob2.get("/api/auth/me")).status_code == 401  # re-enabling does not revive it


async def test_cookie_and_bearer_together_are_refused(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        bearer = {"authorization": "Bearer sk-ss-whatever"}
        r = await admin.get("/api/auth/me", headers=bearer)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "mixed_credentials"
        r = await admin.post("/api/keys", json={"name": "x"}, headers=bearer)
        assert r.status_code == 400
        # /v1 only reads the key; the cookie is ignored there
        r = await admin.get("/v1/models", headers=bearer)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"


async def test_unknown_cookie_is_cleared(app):
    async with client(app, cookies={"slotserve_session": "made-up"}) as c:
        r = await c.get("/api/auth/me")
        assert r.status_code == 401
        assert "slotserve_session=null" in r.headers.get("set-cookie", "")
