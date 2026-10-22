"""R1-01: the first admin needs the setup code; the default invite code opens no sign-up."""

import logging
import os
import stat

from security_helpers import INVITE, client, register, register_admin, running_app
from sqlalchemy import select

from slotserve.config import Settings
from slotserve.db import SessionLocal
from slotserve.orm import AuditLog
from slotserve.security import setup_code as sc


def code_file(tmp_path):
    return tmp_path / "data" / "setup_code"


async def test_first_admin_needs_the_setup_code(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="slotserve")
    async with running_app(tmp_path, monkeypatch) as app:
        code = app.state.setup_code.current()
        assert code and code_file(tmp_path).read_text().strip() == code
        assert code in caplog.text  # printed in the server log
        if os.name != "nt":
            assert stat.S_IMODE(code_file(tmp_path).stat().st_mode) == 0o600
        async with client(app) as c:
            assert (await c.get("/api/auth/setup")).json() == {"setup_required": True}
            r = await register(c, "mallory", invite_code=INVITE)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "setup_code_required"
            r = await register(c, "mallory", setup_code="AAAA-BBBB-CCCC")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "invalid_setup_code"
            assert (await c.get("/api/auth/me")).status_code == 401

            # dashes, spaces and case do not matter
            r = await register(c, "root", setup_code=f"  {code.replace('-', ' ').lower()} ")
            assert r.status_code == 201 and r.json()["is_admin"] is True
            assert (await c.get("/api/auth/me")).json()["username"] == "root"
            assert (await c.get("/api/auth/setup")).json() == {"setup_required": False}
        assert app.state.setup_code.current() is None and not code_file(tmp_path).exists()
        async with SessionLocal() as db:
            actions = (await db.execute(select(AuditLog.action))).scalars().all()
        assert "auth.first_admin" in actions

        async with client(app) as c:  # the used code is worthless; everyone else needs the invite code
            r = await register(c, "mallory", setup_code=code)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "invalid_invite_code"
            r = await register(c, "bob", invite_code=INVITE)
            assert r.status_code == 201 and r.json()["is_admin"] is False


async def test_default_invite_code_only_admits_the_first_admin(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="slotserve")
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_INVITE_CODE=None) as app:
        assert "invite code is not set" in caplog.text
        async with client(app) as c:
            await register_admin(app, c)
        async with client(app) as c:
            r = await register(c, "bob", invite_code="change-me")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "invite_code_not_set"
        async with client(app) as admin:
            await admin.post("/api/auth/login", json={"username": "root", "password": "secret123"})
            r = await admin.put("/api/admin/settings", json={"invite_code": "a-real-code"})
            assert r.status_code == 200, r.text
        async with client(app) as c:
            assert (await register(c, "bob", invite_code="a-real-code")).status_code == 201


async def test_installation_with_an_admin_never_asks_for_a_code(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as app:
        async with client(app) as c:
            await register_admin(app, c)
    # restart, as after an upgrade of an installation that already has its admin
    async with running_app(tmp_path, monkeypatch) as app:
        assert app.state.setup_code.current() is None and not code_file(tmp_path).exists()
        async with client(app) as c:
            assert (await c.get("/api/auth/setup")).json() == {"setup_required": False}
            r = await register(c, "bob", invite_code=INVITE)
            assert r.status_code == 201 and r.json()["is_admin"] is False


async def test_code_survives_restarts_until_used(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as app:
        first = app.state.setup_code.current()
    async with running_app(tmp_path, monkeypatch) as app:
        assert app.state.setup_code.current() == first


async def test_registration_closed_does_not_block_the_first_admin(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_REGISTRATION_OPEN="false") as app:
        async with client(app) as c:
            await register_admin(app, c)
        async with client(app) as c:
            r = await register(c, "bob", invite_code=INVITE)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "registration_closed"


async def test_cli_setup_code(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    code = await sc.cli_setup_code(settings)
    assert code and (await sc.cli_setup_code(settings)) == code
    async with running_app(tmp_path, monkeypatch) as app:
        assert app.state.setup_code.current() == code  # the server uses the same code
        async with client(app) as c:
            await register_admin(app, c)
    assert await sc.cli_setup_code(settings) is None


def test_code_format():
    code = sc.generate()
    assert len(code) == 14 and code.count("-") == 2
    assert set(code.replace("-", "")) <= set(sc.ALPHABET)
    assert sc.normalize(" ab-cd ef ") == "ABCDEF"
