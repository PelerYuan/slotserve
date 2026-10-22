"""R1-01: no installation runs with a publicly known session key."""

import base64
import json
import os
import stat

import pytest
from itsdangerous import TimestampSigner
from security_helpers import client, register_admin, running_app
from starlette.middleware.sessions import SessionMiddleware

from slotserve.config import Settings
from slotserve.security import decrypt_api_key, encrypt_api_key
from slotserve.security import secret_key as sk


def forged_cookie(key: str, uid: int = 1) -> str:
    """A session cookie for ``uid`` signed like Starlette's SessionMiddleware does."""
    return TimestampSigner(key).sign(base64.b64encode(json.dumps({"uid": uid}).encode())).decode()


def session_key_of(app) -> str:
    (mw,) = [m for m in app.user_middleware if m.cls is SessionMiddleware]
    return mw.kwargs["secret_key"]


@pytest.mark.parametrize("value", [None, "change-me", "", "  "])
async def test_unset_or_default_key_is_generated(tmp_path, monkeypatch, value):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_SECRET_KEY=value) as app:
        path = tmp_path / "data" / "secret_key"
        assert path.is_file()
        key = path.read_text().strip()
        assert len(key) >= 40 and key != "change-me"
        assert app.state.settings.secret_key == key == session_key_of(app)
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # a restart keeps the key (sessions and encrypted copies stay valid)
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_SECRET_KEY=value) as app:
        assert app.state.settings.secret_key == key


async def test_explicit_key_wins_and_no_file_is_written(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_SECRET_KEY="configured-in-dot-env") as app:
        assert app.state.settings.secret_key == "configured-in-dot-env" == session_key_of(app)
        assert not (tmp_path / "data" / "secret_key").exists()


async def test_explicit_key_wins_over_an_existing_file(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret_key").write_text("generated-earlier\n")
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_SECRET_KEY="configured-in-dot-env") as app:
        assert app.state.settings.secret_key == "configured-in-dot-env"


@pytest.mark.parametrize("configured", [None, "configured-in-dot-env"])
async def test_cookie_signed_with_change_me_is_refused(tmp_path, monkeypatch, configured):
    async with running_app(tmp_path, monkeypatch, SLOTSERVE_SECRET_KEY=configured) as app:
        async with client(app) as admin:
            await register_admin(app, admin)
            assert (await admin.get("/api/auth/me")).json()["is_admin"] is True
        async with client(app, cookies={"slotserve_session": forged_cookie("change-me")}) as forger:
            assert (await forger.get("/api/auth/me")).status_code == 401
            assert (await forger.get("/api/admin/settings")).status_code == 401


def test_resolve_leaves_settings_alone_for_explicit_keys(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path, secret_key="short")
    assert sk.resolve(s) == "short"
    assert not (tmp_path / "secret_key").exists()


def test_empty_key_file_is_replaced(tmp_path):
    (tmp_path / "secret_key").write_text("")
    key = sk.load_or_create(tmp_path)
    assert key and (tmp_path / "secret_key").read_text().strip() == key


def test_copies_encrypted_with_the_old_default_stay_readable():
    """API key copies and SMB passwords saved while the key was still ``change-me``."""
    token = encrypt_api_key("smb-password", "change-me")
    assert decrypt_api_key(token, "a-generated-key") == "smb-password"
    assert decrypt_api_key(encrypt_api_key("x", "other"), "a-generated-key") is None
