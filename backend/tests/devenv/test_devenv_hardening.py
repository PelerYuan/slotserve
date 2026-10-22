"""W1-01 end to end with the fake Docker: every container is hardened, every environment has its own SSH host key
(kept environments keep theirs), web templates start without credentials, kept containers from before harden()
are recreated once."""

from datetime import timedelta

from sqlalchemy import update
from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.containers.harden import check
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevProject
from slotserve.services.devenv.ssh import host_key_fingerprint, host_public_key


def _running(docker) -> dict:
    [c] = [c for c in docker.containers.values() if c["Running"] and c["Name"].startswith("slotserve-")]
    return c


async def _end(client, app, booking) -> None:
    await client.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)


async def test_every_container_is_hardened_and_keys_are_per_environment(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")

    first = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    c1 = _running(docker)
    key1 = docker.archives[c1["Id"]]["opt/slotserve/ssh_host_key"]
    assert host_public_key(key1).startswith("ssh-ed25519 ")
    [s1] = (await bob.get("/api/devenv/sessions")).json()
    assert s1["host_key_fingerprint"] == host_key_fingerprint(key1)
    keys = app.state.settings.data_dir / "devenv" / "hostkeys"
    assert (keys / f"s{s1['id']}").read_bytes() == key1
    await _end(bob, app, first)
    assert not (keys / f"s{s1['id']}").exists()  # gone with its environment

    second = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    key2 = docker.archives[_running(docker)["Id"]]["opt/slotserve/ssh_host_key"]
    assert key2 != key1  # two environments never share a host key
    await _end(bob, app, second)

    # whatever SlotServe created went through harden(); helpers never ran a person's image
    assert docker.created
    for _name, body in docker.created:
        assert check(body) == []
        assert "Privileged" not in body["HostConfig"]


async def test_kept_environment_keeps_its_key(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    start = utcnow()
    body = {
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=1)).isoformat(),
        "devenv_template_id": torch["id"],
        "devenv_project_name": "Thesis",
    }
    booking = (await bob.post("/api/reservations", json=body)).json()[0]
    await _ticks(app)
    container = _running(docker)
    key = docker.archives[container["Id"]]["opt/slotserve/ssh_host_key"]
    [project] = (await bob.get("/api/devenv/projects")).json()
    assert project["host_key_fingerprint"] == host_key_fingerprint(key)
    await _end(bob, app, booking)

    async def book_project():
        start = utcnow()
        r = await bob.post(
            "/api/reservations",
            json={
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "devenv_project_id": project["id"],
            },
        )
        assert r.status_code == 201, r.text
        await _ticks(app)
        return r.json()[0]

    # resumed: the same container, the same key
    created = len(docker.created)
    booking = await book_project()
    assert len(docker.created) == created and docker.archives[container["Id"]]["opt/slotserve/ssh_host_key"] == key
    await _end(bob, app, booking)

    # other GPUs: snapshotted and recreated, still the same key (StrictHostKeyChecking keeps working)
    async with SessionLocal() as db:
        await db.execute(update(DevProject).values(container_devices="7"))
        await db.commit()
    booking = await book_project()
    recreated = _running(docker)
    assert recreated["Id"] != container["Id"] and docker.commits
    assert docker.archives[recreated["Id"]]["opt/slotserve/ssh_host_key"] == key
    await _end(bob, app, booking)

    # a kept environment from before the upgrade: no key file and a container that was not hardened. It gets a key
    # of its own once (its fingerprint changes once) and is recreated once from a snapshot.
    (app.state.settings.data_dir / "devenv" / "hostkeys" / f"p{project['id']}").unlink()
    recreated["Config"]["Labels"].pop("slotserve.hardened")
    commits = len(docker.commits)
    booking = await book_project()
    newest = _running(docker)
    assert newest["Id"] != recreated["Id"] and len(docker.commits) == commits + 1
    new_key = docker.archives[newest["Id"]]["opt/slotserve/ssh_host_key"]
    assert new_key != key and check(newest["Config"]) == []
    [project] = (await bob.get("/api/devenv/projects")).json()
    assert project["host_key_fingerprint"] == host_key_fingerprint(new_key)
    await _end(bob, app, booking)
    # deleting the project removes its key
    assert (await bob.delete(f"/api/devenv/projects/{project['id']}")).status_code == 204
    assert not (app.state.settings.data_dir / "devenv" / "hostkeys" / f"p{project['id']}").exists()


async def test_web_templates_start_without_credentials(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    templates = {t["builtin_key"]: t for t in (await bob.get("/api/devenv/templates")).json()}

    # a template without a web interface still needs a way in
    r = await _book(bob, templates["pytorch"]["id"])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ssh_login_required"
    r = await bob.post("/api/devenv/cpu/start", json={"template_id": templates["pytorch"]["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ssh_login_required"

    # VS Code in the browser: no key, no password, still starts; SSH lets nobody in (never an empty root password)
    booking = (await _book(bob, templates["vscode"]["id"])).json()[0]
    await _ticks(app)
    files = docker.archives[_running(docker)["Id"]]
    assert files["opt/slotserve/root.hash"] == b"*" and files["opt/slotserve/ssh/authorized_keys"] == b""
    assert " -s " in files["opt/slotserve/ssh.sh"].decode()  # no password logins either
    await _end(bob, app, booking)
    r = await bob.post("/api/devenv/cpu/start", json={"template_id": templates["jupyter-pytorch"]["id"]})
    assert r.status_code == 201, r.text
