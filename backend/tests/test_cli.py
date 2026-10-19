from slotserve.__main__ import _update_user
from slotserve.security import verify_password


async def test_cli_reset_and_promote(make_client, app):
    await make_client("alice")
    await make_client("bob")
    assert await _update_user("BOB", password="newpass1", admin=True)
    assert not await _update_user("nobody", admin=True)

    from slotserve import db
    from slotserve.config import get_settings

    db.configure(get_settings().database_url)  # the CLI disposed the shared engine
    c = await make_client()
    r = await c.post("/api/auth/login", json={"username": "bob", "password": "newpass1"})
    assert r.status_code == 200 and r.json()["is_admin"] is True
    assert verify_password("newpass1", (await _get_hash("bob")))


async def _get_hash(username):
    from sqlalchemy import select

    from slotserve import db
    from slotserve.orm import User

    async with db.SessionLocal() as s:
        return (await s.execute(select(User.password_hash).where(User.username == username))).scalar_one()
