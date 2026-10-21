"""Viewing API keys again: off by default, an admin switch, and switching it off drops every stored copy."""

from sqlalchemy import select

from slotserve.db import SessionLocal
from slotserve.orm import ApiKey


async def test_key_reveal_switch(make_client):
    admin = await make_client("root")
    bob = await make_client("bob")
    eve = await make_client("eve")

    before = (await bob.post("/api/keys", json={"name": "before"})).json()
    assert before["revealable"] is False
    r = await bob.get(f"/api/keys/{before['id']}/reveal")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "key_reveal_disabled"

    assert (await bob.put("/api/admin/settings", json={"api_key_reveal": True})).status_code == 403
    assert (await admin.put("/api/admin/settings", json={"api_key_reveal": True})).status_code == 200

    after = (await bob.post("/api/keys", json={"name": "after"})).json()
    assert after["revealable"] is True
    shown = await bob.get(f"/api/keys/{after['id']}/reveal")
    assert shown.status_code == 200 and shown.json()["key"] == after["key"]
    async with SessionLocal() as db:  # never stored in the clear
        stored = (await db.execute(select(ApiKey.key_encrypted).where(ApiKey.id == after["id"]))).scalar_one()
    assert stored and after["key"] not in stored

    listed = {k["name"]: k["revealable"] for k in (await bob.get("/api/keys")).json()}
    assert listed == {"before": False, "after": True}
    r = await bob.get(f"/api/keys/{before['id']}/reveal")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "key_not_stored"
    assert (await eve.get(f"/api/keys/{after['id']}/reveal")).status_code == 404

    # Off drops the copies; switching it back on does not bring them back.
    await admin.put("/api/admin/settings", json={"api_key_reveal": False})
    assert all(not k["revealable"] for k in (await bob.get("/api/keys")).json())
    assert (await bob.get(f"/api/keys/{after['id']}/reveal")).status_code == 403
    await admin.put("/api/admin/settings", json={"api_key_reveal": True})
    assert (await bob.get(f"/api/keys/{after['id']}/reveal")).status_code == 409

    # The key itself keeps working throughout.
    me = await bob.get("/v1/models", headers={"Authorization": f"Bearer {after['key']}"})
    assert me.status_code == 200
