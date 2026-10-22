"""F11 / A13 / R1-11 against the fake Docker: the environment key's life (issued at the start, extended with the
booking, revoked at the end), the container-side gateway that serves only ``/v1``, and images saved from an
environment without the key, the SSH host key, root's password hash or SlotServe's variables."""

import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import select
from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.containers.contributors.platform_env import ENV_FILE, KEY_FILE
from slotserve.db import SessionLocal
from slotserve.gateway.container_gw import ContainerGateway
from slotserve.orm import ApiKey, DevSession
from slotserve.security import credentials

SHADOW = b"root:$6$salt$alicehashalicehash:19000:0:99999:7:::\ndaemon:*:19000:0:99999:7:::\n"


async def _with_gateway(app) -> ContainerGateway:
    """A container-side gateway that is not bound to a socket (tests call its app directly)."""
    app.state.settings.devenv_gateway_port = 8002
    gateway = ContainerGateway(app.state, serve=False)
    app.state.container_gw = gateway
    return gateway


async def _running(app, make_client):
    admin, bob = await _setup(app, make_client)
    gateway = await _with_gateway(app)
    assert (await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})).status_code == 201
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    booking = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running"
    [container] = app.state.fake_docker.containers.values()
    return admin, bob, gateway, booking, session, container


def _gw_client(gateway: ContainerGateway, key: str | None = None) -> httpx.AsyncClient:
    from slotserve.gateway.container_gw import create_gateway_app

    app = create_gateway_app(gateway.state)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://slotserve-gw:8002", headers=headers)


async def test_environment_key_life(app, make_client):
    docker = app.state.fake_docker
    _, bob, gateway, booking, session, container = await _running(app, make_client)
    assert gateway.url == "http://slotserve-gw:8002/v1" and gateway.extra_host == "slotserve-gw:172.17.0.1"

    # the key is a 0600 file; Docker's environment (docker inspect, /proc/1/environ) only has the endpoint
    files = docker.archives[container["Id"]]
    key = files[KEY_FILE].decode()
    assert key.startswith(credentials.ENV_KEY_PREFIX) and container["Modes"][KEY_FILE] == 0o600
    env = container["Config"]["Env"]
    assert "OPENAI_BASE_URL=http://slotserve-gw:8002/v1" in env and f"SLOTSERVE_ENV_ID={session['id']}" in env
    assert not any(key in e for e in env) and not any(e.startswith("OPENAI_API_KEY=") for e in env)
    assert container["Config"]["HostConfig"]["ExtraHosts"] == ["slotserve-gw:172.17.0.1"]
    inspected = await app.state.devenv.docker.inspect(container["Id"])
    assert key not in str(inspected)

    # the container-side gateway: /v1 with the key, nothing else
    async with _gw_client(gateway, key) as gw, _gw_client(gateway) as anonymous:
        assert (await gw.get("/v1/models")).status_code == 200
        assert (await anonymous.get("/v1/models")).status_code == 401
        for path in ("/api/auth/me", "/api/devenv/sessions", "/healthz", "/", "/api/admin/settings"):
            assert (await gw.get(path)).status_code == 404, path
        assert (await gw.post("/api/auth/login", json={"username": "bob", "password": "secret123"})).status_code == 404

        # extending the booking moves the key's end and the end the shells read
        async with SessionLocal() as db:
            before = (await db.execute(select(ApiKey).where(ApiKey.kind == "env"))).scalar_one().expires_at
        r = await bob.post(f"/api/reservations/{booking['id']}/extend", json={"minutes": 60})
        assert r.status_code == 200, r.text
        await _ticks(app, 1)
        async with SessionLocal() as db:
            after = (await db.execute(select(ApiKey).where(ApiKey.kind == "env"))).scalar_one().expires_at
        assert after - before >= timedelta(minutes=59)
        assert r.json()["end_at"][:16].encode() in docker.archives[container["Id"]][ENV_FILE]

        # the booking ends: the key is revoked and answers 401
        await bob.delete(f"/api/reservations/{booking['id']}")
        await _ticks(app, 2)
        assert (await gw.get("/v1/models")).status_code == 401
    async with SessionLocal() as db:
        row = (await db.execute(select(ApiKey).where(ApiKey.kind == "env"))).scalar_one()
        assert row.revoked_at is not None


async def test_no_key_when_switched_off(app, make_client):
    admin, bob = await _setup(app, make_client)
    await _with_gateway(app)
    r = await admin.put("/api/admin/settings", json={"devenv_llm_key": False})
    assert r.status_code == 200, r.text
    assert (await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})).status_code == 201
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    await _book(bob, torch["id"])
    await _ticks(app)
    [container] = app.state.fake_docker.containers.values()
    assert KEY_FILE not in app.state.fake_docker.archives[container["Id"]]
    assert "OPENAI_BASE_URL=http://slotserve-gw:8002/v1" in container["Config"]["Env"]
    async with SessionLocal() as db:
        assert (await db.execute(select(ApiKey).where(ApiKey.kind == "env"))).first() is None


async def _saved(bob, session_id: int, name: str) -> dict:
    r = await bob.post(f"/api/devenv/sessions/{session_id}/save", json={"name": name})
    assert r.status_code == 202, r.text
    for _ in range(100):
        saved = [t for t in (await bob.get("/api/devenv/templates")).json() if t["name"] == name]
        if saved:
            return saved[0]
        await asyncio.sleep(0.05)
    raise AssertionError("not saved")


async def test_saved_image_has_no_secrets(app, make_client):
    """R1-11: docker commit of a running environment carries neither the key file, the SSH host key, root's
    password hash nor values in Config.Env; the running environment gets its files back."""
    docker = app.state.fake_docker
    _, bob, gateway, _, session, container = await _running(app, make_client)
    cid = container["Id"]
    docker.image_files.setdefault(container["Config"]["Image"], {})["/etc/shadow"] = SHADOW
    before = dict(docker.archives[cid])
    key = before[KEY_FILE].decode()
    assert before["opt/slotserve/ssh_host_key"]

    template = await _saved(bob, session["id"], "Mine")
    saved = docker.saved[template["image"]]
    files, env = saved["files"], saved["Env"]
    assert files[KEY_FILE] == b"" and files["opt/slotserve/ssh_host_key"] == b""
    assert files["etc/shadow"].startswith(b"root:*:") and b"alicehash" not in files["etc/shadow"]
    assert b"daemon:*:" in files["etc/shadow"]
    assert not any(key.encode() in data for data in files.values())
    for entry in env:
        name, _, value = entry.partition("=")
        if name.startswith("SLOTSERVE_") or name.startswith("OPENAI_") or name == "JUPYTER_TOKEN":
            assert value == "", entry
    assert any(e.startswith("PATH=/") for e in env) and not any(key in e for e in env)

    # the running environment keeps working: same key, host key and shadow file as before
    after = docker.archives[cid]
    assert after[KEY_FILE].decode() == key
    assert after["opt/slotserve/ssh_host_key"] == before["opt/slotserve/ssh_host_key"]
    assert after["etc/shadow"] == SHADOW
    async with _gw_client(gateway, key) as gw:
        assert (await gw.get("/v1/models")).status_code == 200


async def test_snapshot_of_a_stopped_container_has_no_secrets(app, make_client):
    """The kept environment's snapshot (``_resume`` when the container cannot be reused) goes through the same
    cleaning; a stopped container gets nothing back (it is removed right after)."""
    docker = app.state.fake_docker
    _, _, _, _, session, container = await _running(app, make_client)
    cid = container["Id"]
    container["Running"] = False
    ref = "slotserve/project-9:snapshot"
    await app.state.devenv._commit_clean(cid, ref, "SlotServe project test", None)
    files = docker.saved[ref]["files"]
    assert files[KEY_FILE] == b"" and files["opt/slotserve/ssh_host_key"] == b""
    assert docker.archives[cid][KEY_FILE] == b""  # not put back
    async with SessionLocal() as db:
        s = await db.get(DevSession, session["id"])
        assert s.state == "running"
