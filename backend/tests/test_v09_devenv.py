"""Dev environments: SSH keys, templates, the container lifecycle, saving images; Docker is faked."""

import asyncio
import base64
import os
from datetime import timedelta

from slotserve.db import utcnow
from slotserve.services.devenv import SshBundle, parse_port_range, parse_public_key


def ed25519_key(comment: str = "bob@laptop") -> str:
    blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + os.urandom(32)
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def test_parsers():
    key, fp = parse_public_key(ed25519_key())
    assert key.startswith("ssh-ed25519 ") and key.endswith(" bob@laptop") and fp.startswith("SHA256:")
    for bad in ("hello", "ssh-ed25519 notbase64!", "ssh-rsa " + base64.b64encode(b"xxxxssh-ed25519").decode()):
        try:
            parse_public_key(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert parse_port_range("2200-2299") == (2200, 2299)
    for bad in ("2200", "3000-2000", "0-10"):
        try:
            parse_port_range(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def _setup(app, make_client):
    app.state.devenv._bundle = SshBundle(b"\x7fELFfake", b"hostkey", "SHA256:fakehostkey")
    admin = await make_client("root")
    bob = await make_client("bob")
    r = await admin.put("/api/admin/settings", json={"devenv_enabled": True})
    assert r.status_code == 200, r.text
    return admin, bob


async def _ticks(app, n: int = 3) -> None:
    for _ in range(n):
        await app.state.devenv.tick()
        await asyncio.sleep(0.05)


async def _book(client, template_id: int, start=None, hours: float = 1):
    start = start or utcnow()
    return await client.post(
        "/api/reservations",
        json={
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=hours)).isoformat(),
            "devenv_template_id": template_id,
        },
    )


async def test_keys_and_templates(app, make_client):
    admin, bob = await _setup(app, make_client)
    assert (await bob.post("/api/ssh-keys", json={"public_key": "not a key at all, really"})).status_code == 400
    r = await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    assert r.status_code == 201 and r.json()["name"] == "bob@laptop" and r.json()["key_type"] == "ssh-ed25519"
    key = ed25519_key("second")
    assert (await bob.post("/api/ssh-keys", json={"name": "desk", "public_key": key})).status_code == 201
    assert (await bob.post("/api/ssh-keys", json={"public_key": key})).status_code == 409
    assert len((await bob.get("/api/ssh-keys")).json()) == 2

    templates = (await bob.get("/api/devenv/templates")).json()
    assert {t["builtin_key"] for t in templates} == {"pytorch", "jupyter-pytorch", "tensorflow", "cuda"}
    info = (await bob.get("/api/devenv")).json()
    assert info["enabled"] and info["docker"] == {"ok": True} and info["host_key_fingerprint"] == "SHA256:fakehostkey"

    mine = await bob.post("/api/devenv/templates", json={"name": "Mine", "image": "ghcr.io/me/env:1"})
    assert mine.status_code == 201 and mine.json()["kind"] == "custom" and mine.json()["mine"]
    assert (await bob.post("/api/devenv/templates", json={"name": "x", "image": "bad image"})).status_code == 400
    assert (
        await bob.post("/api/devenv/templates", json={"name": "x", "image": "a/b", "shared": True})
    ).status_code == 403
    await admin.put("/api/admin/settings", json={"devenv_allowed_registries": "docker.io, nvcr.io"})
    r = await bob.post("/api/devenv/templates", json={"name": "x", "image": "ghcr.io/me/env:2"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_registry_not_allowed"
    assert (
        await bob.post("/api/devenv/templates", json={"name": "x", "image": "nvcr.io/nvidia/pytorch:25.01-py3"})
    ).status_code == 201

    # admins can switch a built-in off; others no longer see it
    torch = next(t for t in templates if t["builtin_key"] == "pytorch")
    assert (await bob.patch(f"/api/devenv/templates/{torch['id']}", json={"enabled": False})).status_code == 403
    assert (await admin.patch(f"/api/devenv/templates/{torch['id']}", json={"enabled": False})).status_code == 200
    assert torch["id"] not in {t["id"] for t in (await bob.get("/api/devenv/templates")).json()}


async def test_lifecycle(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    templates = {t["builtin_key"]: t for t in (await bob.get("/api/devenv/templates")).json()}
    torch = templates["pytorch"]

    # a key is required, and dev environments must be switched on
    r = await _book(bob, torch["id"])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ssh_key_required"
    key = ed25519_key()
    await bob.post("/api/ssh-keys", json={"public_key": key})
    await admin.put("/api/admin/settings", json={"devenv_enabled": False})
    assert (await _book(bob, torch["id"])).json()["detail"]["code"] == "devenv_disabled"
    await admin.put("/api/admin/settings", json={"devenv_enabled": True})

    docker.busy_ports.add("2200")  # something else on the host already uses the first port
    booking = (await _book(bob, torch["id"])).json()[0]
    assert booking["devenv_template_name"] == "PyTorch" and booking["exclusive"]
    await _ticks(app)

    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running", session
    assert torch["image"] in docker.pulls
    assert session["ssh_port"] == 2201 and session["ssh_command"] == "ssh -p 2201 root@test"
    [container] = docker.containers.values()
    config = container["Config"]
    assert config["Image"] == torch["image"] and config["Entrypoint"] == ["/bin/sh", "/opt/slotserve/start.sh"]
    assert config["HostConfig"]["DeviceRequests"][0]["Capabilities"] == [["gpu"]]
    assert {"Type": "volume", "Source": f"slotserve-workspace-u{booking['user_id']}", "Target": "/workspace"} in config[
        "HostConfig"
    ]["Mounts"]
    files = docker.archives[container["Id"]]
    assert files["opt/slotserve/ssh/authorized_keys"].decode().strip() == key
    assert files["opt/slotserve/ssh_host_key"] == b"hostkey" and b"dropbear" in files["opt/slotserve/start.sh"]
    assert (
        container["Modes"]["opt/slotserve/ssh"] == 0o700 and container["Modes"]["opt/slotserve/ssh_host_key"] == 0o600
    )

    # someone else cannot see or touch it
    eve = await make_client("eve")
    assert (await eve.get("/api/devenv/sessions")).json() == []
    assert (await eve.post(f"/api/devenv/sessions/{session['id']}/restart")).status_code == 404
    everyone = (await admin.get("/api/devenv/sessions?everyone=true")).json()
    assert [s["username"] for s in everyone] == ["bob"]

    # save it as an image, then it is offered as a template
    r = await bob.post(f"/api/devenv/sessions/{session['id']}/save", json={"name": "My Torch"})
    assert r.status_code == 202
    for _ in range(50):
        saved = [t for t in (await bob.get("/api/devenv/templates")).json() if t["kind"] == "saved"]
        if saved:
            break
        await asyncio.sleep(0.05)
    assert saved[0]["name"] == "My Torch" and saved[0]["image"].startswith(f"slotserve/u{booking['user_id']}-my-torch:")
    assert saved[0]["image"] in docker.images
    await admin.put("/api/admin/settings", json={"devenv_saved_images": 1})
    r = await bob.post(f"/api/devenv/sessions/{session['id']}/save", json={"name": "Again"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_saved_limit"

    # the container dies: the session says why; a restart brings it back
    container["Running"], container["ExitCode"] = False, 137
    await _ticks(app, 1)
    session = (await bob.get("/api/devenv/sessions")).json()[0]
    assert session["state"] == "failed" and "exit code 137" in session["error"] and session["ssh_command"] is None
    assert not docker.containers
    assert (await bob.post(f"/api/devenv/sessions/{session['id']}/restart")).status_code == 202
    await _ticks(app)
    session = (await bob.get("/api/devenv/sessions")).json()[0]
    assert session["state"] == "running" and len(docker.containers) == 1

    # ending the booking removes the container; the workspace volume is not touched
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    session = (await bob.get("/api/devenv/sessions")).json()[0]
    assert session["state"] == "stopped" and not docker.containers

    # a saved image in use cannot be deleted; otherwise deleting removes the image too
    assert (await bob.delete(f"/api/devenv/templates/{saved[0]['id']}")).status_code == 204
    assert saved[0]["image"] not in docker.images


async def test_pull_failure_and_cleanup(app, make_client):
    docker = app.state.fake_docker
    stray = docker.add_container("slotserve-dev-999", {"slotserve.devenv": "1", "slotserve.session": "999"})
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    t = (await bob.post("/api/devenv/templates", json={"name": "Broken", "image": "example/missing:1"})).json()
    await _book(bob, t["id"])
    await _ticks(app)
    assert stray not in docker.containers
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "failed" and "not found" in session["error"]

    # a booking later today is prepared ahead: the image is pulled, nothing is started yet
    later = (await bob.get("/api/devenv/templates")).json()
    cuda = next(x for x in later if x["builtin_key"] == "cuda")
    await bob.delete(f"/api/reservations/{session['reservation_id']}")
    await _book(bob, cuda["id"], start=utcnow() + timedelta(minutes=30))
    await _ticks(app)
    sessions = (await bob.get("/api/devenv/sessions")).json()
    upcoming = next(s for s in sessions if s["image"] == cuda["image"])
    assert upcoming["state"] == "ready" and cuda["image"] in docker.images and not docker.containers
