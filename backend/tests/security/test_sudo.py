"""Sudo mode (R1-09): ``Depends(sudo_user)`` needs the password entered within the last five minutes."""

from types import SimpleNamespace

import pytest
from fastapi import Depends
from security_helpers import INVITE, PASSWORD, client, register, register_admin, running_app

from slotserve.deps import sudo_admin, sudo_user
from slotserve.security import sudo


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(sudo, "_now", c)
    return c


def fake_request():
    return SimpleNamespace(session={})


def test_window(clock):
    req = fake_request()
    assert not sudo.is_active(req, 1)
    assert sudo.grant(req, 1) == clock.now + 300
    clock.now += 299
    assert sudo.is_active(req, 1)
    assert not sudo.is_active(req, 2)  # bound to the account that entered the password
    clock.now += 2
    assert not sudo.is_active(req, 1)


def test_clock_jumping_back_and_garbage_count_as_inactive(clock):
    req = fake_request()
    sudo.grant(req, 1)
    clock.now -= 3600
    assert not sudo.is_active(req, 1)
    req.session[sudo.SESSION_KEY] = {"uid": 1, "at": "soon"}
    assert not sudo.is_active(req, 1)
    req.session[sudo.SESSION_KEY] = "x"
    assert not sudo.is_active(req, 1)
    sudo.revoke(req)
    assert sudo.SESSION_KEY not in req.session


async def test_sudo_dependency(tmp_path, monkeypatch, clock):
    async with running_app(tmp_path, monkeypatch) as app:

        @app.post("/api/test-sudo")
        async def needs_sudo(user=Depends(sudo_user)):
            return {"user": user.username}

        @app.post("/api/test-sudo-admin")
        async def needs_sudo_admin(user=Depends(sudo_admin)):
            return {"user": user.username}

        async with client(app) as root, client(app) as bob:
            await register_admin(app, root)
            assert (await register(bob, "bob", invite_code=INVITE)).status_code == 201

            # signing up the first admin counts as entering the password
            assert (await root.post("/api/test-sudo-admin")).status_code == 200
            # a member who signed up has not entered it for sudo yet
            r = await bob.post("/api/test-sudo")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "sudo_required"
            assert (await bob.get("/api/auth/sudo")).json()["active"] is False

            r = await bob.post("/api/auth/sudo", json={"password": "wrong"})
            assert r.status_code == 401
            r = await bob.post("/api/auth/sudo", json={"password": PASSWORD})
            assert r.status_code == 200 and r.json()["active"] is True
            assert (await bob.post("/api/test-sudo")).json() == {"user": "bob"}
            assert (await bob.post("/api/test-sudo-admin")).status_code == 403  # sudo does not make an admin

            clock.now += 301
            assert (await bob.post("/api/test-sudo")).status_code == 403
            assert (await root.post("/api/test-sudo-admin")).status_code == 403
            state = (await root.get("/api/auth/sudo")).json()
            assert state == {"active": False, "expires_at": None, "window_seconds": 300}

            # signing in again starts a fresh window
            await root.post("/api/auth/login", json={"username": "root", "password": PASSWORD})
            assert (await root.post("/api/test-sudo-admin")).status_code == 200
            assert (await root.get("/api/auth/sudo")).json()["active"] is True

            await root.post("/api/auth/logout")
            assert (await root.post("/api/test-sudo-admin")).status_code == 401


async def test_sudo_needs_a_session(tmp_path, monkeypatch):
    async with running_app(tmp_path, monkeypatch) as app:
        async with client(app) as anon:
            assert (await anon.post("/api/auth/sudo", json={"password": "x"})).status_code == 401
