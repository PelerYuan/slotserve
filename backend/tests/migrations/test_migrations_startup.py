"""A real 0.8.0 database: the current server starts on it, migrates it and serves its data."""

import json

import httpx
import pytest
from migration_helpers import load_fixture
from sqlalchemy import select, text

from slotserve import db as dbmod
from slotserve.orm import AuditLog, KeyValue, User

PASSWORD = "fixture-pass"  # tests/fixtures/db/make_fixture.py


@pytest.fixture
def release_data(tmp_path):
    load_fixture("v0.8.0", tmp_path / "data" / "slotserve.db")


@pytest.fixture
async def app(release_data, app):  # the conftest app, started on the fixture database
    return app


async def _login(app, username: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-SlotServe": "1"}
    )
    r = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


async def test_server_starts_on_upgraded_release_database(app, tmp_path):
    data = tmp_path / "data"
    [snapshot] = (data / "backups").iterdir()
    assert snapshot.name.startswith("slotserve-vschema1-to-v")
    assert json.loads((data / "migration_report.json").read_text())["dropped_rows"] == 0

    admin = await _login(app, "admin")
    try:
        assert (await admin.get("/api/auth/me")).json()["is_admin"] is True
        users = (await admin.get("/api/admin/users")).json()
        assert {u["username"] for u in users} == {"admin", "alice", "bob"}
        keys = (await admin.get("/api/admin/keys")).json()
        assert {k["prefix"] for k in keys} >= {"sk-fx0", "sk-fx1"}
        models = (await admin.get("/api/models")).json()
        assert {m["name"] for m in models} >= {"fixture-model-0-Q4_K_M", "fixture-model-1-Q4_K_M"}
        assert all(m.get("args_pending") is None for m in models)  # 0004: nothing waits for review
        reservations = (
            await admin.get(
                "/api/admin/reservations", params={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"}
            )
        ).json()
        assert len(reservations) >= 2
        settings = {s["key"]: s["value"] for s in (await admin.get("/api/admin/settings")).json()}
        assert settings["max_reservation_hours"] == 8  # an admin override from 0.8.0 still applies
        # the upgraded database takes new rows of every kind
        r = await admin.post("/api/keys", json={"name": "after upgrade"})
        assert r.status_code == 201, r.text
    finally:
        await admin.aclose()
    alice = await _login(app, "alice")
    await alice.aclose()


async def test_durable_tables_commit_with_full_sync(app):
    """WAL with synchronous=NORMAL in general; users, keys, audit and settings are written with FULL."""
    async with dbmod.SessionLocal() as session:
        session.add(KeyValue(key="sync-test", value="{}"))
        await session.flush()
        assert (await session.execute(text("PRAGMA synchronous"))).scalar() == 1  # NORMAL
        await session.commit()
    async with dbmod.SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == "bob"))).scalar_one()
        session.add(AuditLog(actor_id=user.id, actor_name="bob", action="test.sync", target=""))
        await session.flush()
        assert (await session.execute(text("PRAGMA synchronous"))).scalar() == 2  # FULL
        await session.commit()
    async with dbmod.SessionLocal() as session:  # the next transaction is back to NORMAL
        assert (await session.execute(text("PRAGMA synchronous"))).scalar() == 1
        # a durable row after the transaction began cannot switch any more: it still commits (with NORMAL)
        session.add(KeyValue(key="sync-test-2", value="{}"))
        await session.flush()
        session.add(AuditLog(actor_id=None, actor_name="", action="test.sync", target=""))
        await session.commit()
