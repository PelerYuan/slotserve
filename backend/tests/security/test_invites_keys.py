"""R1-11 / F06: invite links instead of the global invite code; API key scope and expiry; environment keys end
with their environment (401 on /v1)."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from security_helpers import INVITE, PASSWORD, client, register, register_admin, running_app

from slotserve import db as dbmod
from slotserve import orm
from slotserve.db import utcnow
from slotserve.security import credentials


@pytest.fixture
async def app(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as application:
        yield application


def audit(app) -> list[tuple[str, dict]]:
    con = sqlite3.connect(app.state.settings.data_dir / "slotserve.db")
    try:
        return [(a, json.loads(d)) for a, d in con.execute("SELECT action, detail FROM audit_logs ORDER BY id")]
    finally:
        con.close()


async def test_invite_link_signs_up_and_counts(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        r = await admin.post("/api/admin/invites", json={"note": "lab", "max_uses": 1})
        assert r.status_code == 201, r.text
        inv = r.json()
        token = inv["token"]
        assert token.startswith("ssi-") and inv["path"] == f"/login?invite={token}"
        assert INVITE not in inv["path"]  # never the global invite code in a link
        assert (inv["uses"], inv["max_uses"], inv["state"], inv["requires_approval"]) == (0, 1, "active", False)
        expires = datetime.fromisoformat(inv["expires_at"].replace("Z", "+00:00"))
        assert abs(utcnow() + timedelta(days=7) - expires) < timedelta(minutes=1)

        async with client(app) as anon:
            check = (await anon.post("/api/invites/check", json={"token": token})).json()
            assert check["valid"] is True and check["requires_approval"] is False
            r = await register(anon, "alice", invite_token=token)
            assert r.status_code == 201 and r.json()["approval_pending"] is False
            assert (await anon.get("/api/auth/me")).json()["username"] == "alice"
        async with client(app) as late:
            r = await register(late, "bob", invite_token=token)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "invite_used_up"
            check = (await late.post("/api/invites/check", json={"token": token})).json()
            assert check == {"valid": False, "code": "invite_used_up", "requires_approval": False, "expires_at": None}

        listed = (await admin.get("/api/admin/invites")).json()
        assert listed[0]["state"] == "used_up" and [u["username"] for u in listed[0]["users"]] == ["alice"]
        assert "token" not in listed[0]
    actions = audit(app)
    assert ("invite.create" in [a for a, _ in actions]) and any(
        a == "auth.register" and d.get("invite_id") == inv["id"] for a, d in actions
    )


async def test_revoked_and_expired_invites_fail(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        revoked = (await admin.post("/api/admin/invites", json={})).json()
        assert (await admin.delete(f"/api/admin/invites/{revoked['id']}")).status_code == 204
        expired = (await admin.post("/api/admin/invites", json={})).json()
    async with dbmod.SessionLocal() as db:
        row = await db.get(orm.Invite, expired["id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    async with client(app) as anon:
        for inv, code in ((revoked, "invite_revoked"), (expired, "invite_expired")):
            r = await register(anon, f"user{inv['id']}", invite_token=inv["token"])
            assert r.status_code == 403 and r.json()["detail"]["code"] == code
        r = await register(anon, "guess", invite_token="ssi-guessed")
        assert r.json()["detail"]["code"] == "invite_invalid"
        # the global code keeps working (expert layer)
        assert (await register(anon, "coder", invite_code=INVITE)).status_code == 201


async def test_invite_with_approval(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        token = (await admin.post("/api/admin/invites", json={"requires_approval": True})).json()["token"]
        async with client(app) as carol:
            r = await register(carol, "carol", invite_token=token)
            assert r.status_code == 201 and r.json()["approval_pending"] is True
            assert (await carol.get("/api/auth/me")).status_code == 401  # not signed in
            r = await carol.post("/api/auth/login", json={"username": "carol", "password": PASSWORD})
            assert r.status_code == 403 and r.json()["detail"]["code"] == "account_pending"

            pending = (await admin.get("/api/admin/invites/pending")).json()
            assert [p["username"] for p in pending] == ["carol"]
            r = await admin.post(f"/api/admin/invites/pending/{pending[0]['id']}/approve")
            assert r.status_code == 200 and r.json()["approval_pending"] is False
            r = await carol.post("/api/auth/login", json={"username": "carol", "password": PASSWORD})
            assert r.status_code == 200
        async with client(app) as dave:
            await register(dave, "dave", invite_token=token)
            dave_id = (await admin.get("/api/admin/invites/pending")).json()[0]["id"]
            assert (await admin.post(f"/api/admin/invites/pending/{dave_id}/reject")).status_code == 204
            r = await dave.post("/api/auth/login", json={"username": "dave", "password": PASSWORD})
            assert r.status_code == 401
    assert {"user.approve", "user.reject"} <= {a for a, _ in audit(app)}


async def test_only_admins_manage_invites(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        async with client(app) as bob:
            await register(bob, "bob", invite_code=INVITE)
            assert (await bob.post("/api/admin/invites", json={})).status_code == 403
            assert (await bob.get("/api/admin/invites")).status_code == 403
        r = await admin.post("/api/admin/invites", json={"days": 91})
        assert r.status_code == 422


async def test_key_scope_and_expiry(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        r = await admin.post("/api/keys", json={"name": "root-key", "scope": "admin"})
        assert r.status_code == 422  # there is no admin scope
        r = await admin.post("/api/keys", json={"name": "short", "expires_in_days": 90})
        assert r.status_code == 201
        key = r.json()
        assert (key["scope"], key["kind"], key["expired"]) == ("llm", "api", False) and key["expires_at"]
        assert key["prefix"] == key["key"][:10]
        legacy = (await admin.post("/api/keys", json={"name": "plain"})).json()
        assert legacy["expires_at"] is None
    async with client(app, csrf_header=False) as script:
        bearer = {"authorization": f"Bearer {key['key']}"}
        assert (await script.get("/v1/models", headers=bearer)).status_code == 200
        async with dbmod.SessionLocal() as db:
            row = await db.get(orm.ApiKey, key["id"])
            assert row.last_used_at is not None and row.last_ip
            row.expires_at = utcnow() - timedelta(seconds=1)
            await db.commit()
        r = await script.get("/v1/models", headers=bearer)
        assert r.status_code == 401 and r.json()["error"]["code"] == "expired_api_key"
        # keys made without scope/expiry (as before 0.10) keep working
        r = await script.get("/v1/models", headers={"authorization": f"Bearer {legacy['key']}"})
        assert r.status_code == 200
    assert "key.create" in {a for a, _ in audit(app)}


async def test_environment_key_stops_with_the_environment(app):
    async with client(app) as admin:
        await register_admin(app, admin)
        uid = (await admin.get("/api/auth/me")).json()["id"]
        async with dbmod.SessionLocal() as db:
            s = orm.DevSession(kind="booking", user_id=uid, image="img", state="starting")
            db.add(s)
            await db.flush()
            raw = await credentials.issue_env_key(db, s.id)
            await db.commit()
            sid = s.id
        listed = (await admin.get("/api/keys")).json()
        assert all(k["kind"] == "api" for k in listed)  # environment keys are not the person's keys
    async with client(app, csrf_header=False) as container:
        bearer = {"authorization": f"Bearer {raw}"}
        assert (await container.get("/v1/models", headers=bearer)).status_code == 200
        async with dbmod.SessionLocal() as db:
            (await db.get(orm.DevSession, sid)).state = "stopped"
            await db.commit()
        r = await container.get("/v1/models", headers=bearer)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
        # an environment key is no session: it cannot use SlotServe's own API
        assert (await container.get("/api/auth/me", headers=bearer)).status_code == 401
