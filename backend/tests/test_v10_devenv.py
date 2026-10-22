"""Dev environments 0.10: SSH passwords, idle lanes with a shared environment, kept projects, storage."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.config import get_settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevProject, DevSession, User
from slotserve.runtime.scheduler import compute_target
from slotserve.services.devenv import DevEnvError, can_access, check_persist_paths, unix_names
from slotserve.services.shacrypt import is_sha512_crypt, sha512_crypt, verify

# The test vectors published with the SHA-crypt specification (Drepper).
VECTORS = [
    (
        "$6$saltstring",
        "Hello world!",
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1",
    ),
    (
        "$6$rounds=10000$saltstringsaltstring",
        "Hello world!",
        "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vOuZeHBy/"
        "YTBmSK6H9qs/y3RnOaw5v.",
    ),
    (
        "$6$rounds=5000$toolongsaltstring",
        "This is just a test",
        "$6$rounds=5000$toolongsaltstrin$lQ8jolhgVRVhY4b5pZKaysCLi0QBxGoNeKQzQ3glMhwllF7oGDZxUhx1yxdYcz/"
        "e1JSbq3y6JMxxl8audkUEm0",
    ),
    (
        "$6$rounds=1400$anotherlongsaltstring",
        "a very much longer text to encrypt.  This one even stretches over morethan one line.",
        "$6$rounds=1400$anotherlongsalts$POfYwTEok97VWcjxIiSOjiykti.o/pQs.wPvMxQ6Fm7I6IoYN3CmLs66x9t0oSwbtEW7o7UmJEiDw"
        "Gqd8p4ur1",
    ),
    (
        "$6$rounds=77777$short",
        "we have a short salt string but not a short password",
        "$6$rounds=77777$short$WuQyW2YR.hBNpjjRhpYD/ifIw05xdfeEyQoMxIXbkvr0gge1a1x3yRULJ5CCaUeOxFmtlcGZelFl5CxtgfiAc0",
    ),
    (
        "$6$rounds=123456$asaltof16chars..",
        "a short string",
        "$6$rounds=123456$asaltof16chars..$BtCwjqMJGx5hrJhZywWvt0RLE8uZ4oPwcelCjmw2kSYu.Ec6ycULevoBK25fs2xXgMNrCzIMVcgEJ"
        "AstJeonj1",
    ),
    (
        "$6$rounds=10$roundstoolow",
        "the minimum number is still observed",
        "$6$rounds=1000$roundstoolow$kUMsbe306n21p9R.FRkW3IGn.S9NPN0x50YhH1xhLsPuWGsUSklZt58jaTfF4ZEQpyUNGc0dqbpBYYBa"
        "HHrsX.",
    ),
]


@pytest.mark.parametrize(("setting", "password", "expected"), VECTORS)
def test_sha512_crypt_vectors(setting, password, expected):
    assert sha512_crypt(password, setting) == expected


def test_sha512_crypt_random_salt():
    h = sha512_crypt("correct horse")
    assert is_sha512_crypt(h) and h.count("$") == 3 and len(h.split("$")[2]) == 16
    assert verify("correct horse", h) and not verify("wrong horse", h)
    assert sha512_crypt("correct horse") != h  # a new salt every time


def test_helpers():
    users = [User(id=i, username=n) for i, n in enumerate(["Bob", "bob", "root", "9lives", "a.b", "ubuntu"], 1)]
    names = unix_names(users)
    assert names == {1: "bob", 2: "bob_2", 3: "root_3", 4: "u9lives", 5: "a_b", 6: "ubuntu_6"}
    assert check_persist_paths(["/root", " /opt/conda/ ", "/root", ""]) == ["/root", "/opt/conda"]
    for bad in (
        "relative",
        "/",
        "/etc",
        "/proc/1",
        "/opt/slotserve/x",
        "/a/../etc",
        "/workspace",
        "/usr/local",
        "/a b",
    ):
        with pytest.raises(DevEnvError):
            check_persist_paths([bad])
    with pytest.raises(DevEnvError):
        check_persist_paths([f"/data{i}" for i in range(9)])
    owner, other, admin = User(id=1, is_admin=False), User(id=2, is_admin=False), User(id=3, is_admin=True)
    mine, shared = DevSession(kind="booking", user_id=1), DevSession(kind="shared", user_id=None)
    assert can_access(mine, owner) and can_access(mine, admin) and not can_access(mine, other)
    assert can_access(shared, other) and not can_access(mine, None)


async def _wait(predicate, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition never became true")


async def test_password(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    assert (await bob.get("/api/devenv/password")).json() == {"set": False, "login_enabled": True}
    assert (await bob.put("/api/devenv/password", json={"password": "short"})).status_code == 422
    r = await bob.put("/api/devenv/password", json={"password": "a long secret"})
    assert r.status_code == 204 and r.text == ""
    assert (await bob.get("/api/devenv/password")).json()["set"] is True
    info = (await bob.get("/api/devenv")).json()
    assert info["password_set"] and info["password_login"]
    async with SessionLocal() as db:
        stored = (await db.execute(select(User.devenv_password_hash).where(User.username == "bob"))).scalar_one()
    assert is_sha512_crypt(stored) and verify("a long secret", stored)
    me = (await bob.get("/api/auth/me")).text
    assert "$6$" not in me and "a long secret" not in me

    # a password is enough to book without an SSH key; root gets the hash and passwords are accepted
    booking = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running" and session["ssh_command"].startswith("ssh -p 2200 root@")
    [container] = docker.containers.values()
    files = docker.archives[container["Id"]]
    assert files["opt/slotserve/root.hash"].decode() == stored
    ssh = files["opt/slotserve/ssh.sh"].decode()
    assert " -s " not in ssh and "-T 3" in ssh and "set_hash root" in ssh
    assert "ssh.sh --setup" in files["opt/slotserve/start.sh"].decode()
    assert "chpasswd -e" in files["opt/slotserve/lib.sh"].decode()
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)

    # with password login off, a key is needed again and Dropbear refuses passwords
    await admin.put("/api/admin/settings", json={"devenv_password_login": False})
    r = await _book(bob, torch["id"])
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ssh_key_required"
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    booking = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    container = next(c for c in docker.containers.values() if c["Running"])
    files = docker.archives[container["Id"]]
    assert files["opt/slotserve/root.hash"] == b"*" and " -s " in files["opt/slotserve/ssh.sh"].decode()

    assert (await bob.delete("/api/devenv/password")).status_code == 204
    assert (await bob.get("/api/devenv/password")).json() == {"set": False, "login_enabled": False}


async def test_idle_modes_in_scheduler(app, make_client):
    admin, _bob = await _setup(app, make_client)
    lane = (await admin.get("/api/lanes")).json()[0]
    assert lane["idle_mode"] == "model"
    r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json={"name": lane["name"], "idle_mode": "devenv"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "lane_idle_template"
    r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json={"name": lane["name"], "idle_mode": "none"})
    assert r.status_code == 200 and r.json()["idle_mode"] == "none" and r.json()["idle_template_id"] is None
    # leaving idle_mode out keeps it
    r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json={"name": "Renamed"})
    assert r.json()["idle_mode"] == "none"
    settings = get_settings()
    async with SessionLocal() as db:
        target = await compute_target(db, settings, utcnow(), None, None, lane["id"])
    assert target.model is None and target.reason == "idle-none"
    status = (await admin.get("/api/status")).json()["lanes"][0]
    assert status["idle_mode"] == "none"


async def test_shared_environment(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    eve = await make_client("eve")
    templates = (await admin.get("/api/devenv/templates")).json()
    cuda = next(t for t in templates if t["builtin_key"] == "cuda")
    mine = (await bob.post("/api/devenv/templates", json={"name": "Mine", "image": "example/mine:1"})).json()
    key = ed25519_key("bob@desk")
    await bob.post("/api/ssh-keys", json={"public_key": key})
    await eve.put("/api/devenv/password", json={"password": "eve's password"})
    lane = (await admin.get("/api/lanes")).json()[0]
    body = {"name": lane["name"], "idle_mode": "devenv", "idle_template_id": mine["id"]}
    r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json=body)
    assert r.status_code == 400  # only templates offered to everyone
    body["idle_template_id"] = cuda["id"]
    r = await admin.patch(f"/api/admin/lanes/{lane['id']}", json=body)
    assert r.status_code == 200 and r.json()["idle_template_name"] == "CUDA"

    scheduler = app.state.lanes.get(lane["id"]).scheduler

    async def idle():
        return scheduler.target is not None and scheduler.target.reason == "idle-devenv"

    await _wait(idle)
    await _ticks(app)
    sessions = (await eve.get("/api/devenv/sessions")).json()
    [shared] = [s for s in sessions if s["kind"] == "shared"]
    assert shared["state"] == "running" and shared["reservation_id"] is None and shared["username"] is None
    assert shared["ssh_command"] == f"ssh -p {shared['ssh_port']} eve@test" and shared["lane_id"] == lane["id"]
    assert (await bob.get("/api/devenv/sessions")).json()[0]["ssh_command"].endswith(" bob@test")
    assert app.state.devenv.holds_gpu(lane["id"])

    [container] = [c for c in docker.containers.values() if c["Running"]]
    config = container["Config"]
    assert container["Name"] == f"slotserve-idle-{lane['id']}" and config["Image"] == cuda["image"]
    mounts = config["HostConfig"]["Mounts"]
    assert {"Type": "volume", "Source": f"slotserve-idle-home-l{lane['id']}", "Target": "/home"} in mounts
    assert not any(m["Target"] == "/workspace" for m in mounts)
    files = docker.archives[container["Id"]]
    ssh = files["opt/slotserve/ssh.sh"].decode()
    assert " -w " in ssh and " -s " not in ssh and "-T 3" in ssh and "accounts.sh" in ssh and "-D " not in ssh
    async with SessionLocal() as db:
        users = {u.username: u for u in (await db.execute(select(User))).scalars()}
    accounts = files["opt/slotserve/accounts"].decode().splitlines()
    assert f"bob:{20000 + users['bob'].id}:*" in accounts
    assert f"eve:{20000 + users['eve'].id}:{users['eve'].devenv_password_hash}" in accounts
    assert files["opt/slotserve/keys/bob"].decode().strip() == key and files["opt/slotserve/keys/eve"] == b""
    assert b"useradd" in files["opt/slotserve/accounts.sh"] and b"adduser" in files["opt/slotserve/accounts.sh"]

    # anyone can see it, only admins can restart it or read its logs
    assert (await bob.post(f"/api/devenv/sessions/{shared['id']}/restart")).status_code == 403
    assert (await bob.get(f"/api/devenv/sessions/{shared['id']}/logs")).status_code == 403
    assert (await admin.get(f"/api/devenv/sessions/{shared['id']}/logs")).status_code == 200
    r = await admin.post(f"/api/devenv/sessions/{shared['id']}/save", json={"name": "x"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_shared_save"

    # a new key is copied in and the accounts script runs again
    newer = ed25519_key("bob@laptop")
    await bob.post("/api/ssh-keys", json={"public_key": newer})
    await _ticks(app, 1)
    assert docker.execs and docker.exec_calls[-1] == (container["Id"], ["/bin/sh", "/opt/slotserve/accounts.sh"])
    assert newer in docker.archives[container["Id"]]["opt/slotserve/keys/bob"].decode()
    runs = len(docker.exec_calls)
    await _ticks(app, 1)
    assert len(docker.exec_calls) == runs  # nothing changed, nothing to do

    # a failing refresh is shown to admins
    docker.exec_result = (2, b"slotserve: this image has neither useradd nor adduser")
    await eve.delete("/api/devenv/password")
    await _ticks(app, 1)
    shared = next(s for s in (await admin.get("/api/devenv/sessions")).json() if s["kind"] == "shared")
    assert "neither useradd nor adduser" in shared["error"]
    assert next(s for s in (await eve.get("/api/devenv/sessions")).json() if s["kind"] == "shared")["error"] is None
    docker.exec_result = (0, b"")

    # a booking stops the shared environment; the lane's status says what it does when idle
    status = (await bob.get("/api/status")).json()["lanes"][0]
    assert status["idle_mode"] == "devenv" and status["idle_template_name"] == "CUDA"
    assert status["shared_state"] == "running" and status["reason"] == "idle-devenv"
    start_at = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={"start_at": start_at.isoformat(), "end_at": (start_at + timedelta(hours=1)).isoformat()},
    )
    assert r.status_code == 201, r.text

    async def busy():
        return scheduler.target is not None and scheduler.target.reason == "exclusive"

    await _wait(busy)
    await _ticks(app, 2)
    assert not any(c["Running"] for c in docker.containers.values())
    assert not app.state.devenv.holds_gpu(lane["id"])
    assert not [s for s in (await bob.get("/api/devenv/sessions")).json() if s["kind"] == "shared"]
    assert (await bob.get("/api/status")).json()["lanes"][0]["shared_state"] is None

    # ... and it comes back once the lane is idle again
    await bob.delete(f"/api/reservations/{r.json()[0]['id']}")
    await _wait(idle)
    await _ticks(app, 2)
    assert [s["state"] for s in (await bob.get("/api/devenv/sessions")).json() if s["kind"] == "shared"] == ["running"]


async def test_scheduler_waits_for_shared_environment(app, make_client):
    """A model is not loaded while the shared environment still holds the GPUs."""
    await _setup(app, make_client)
    ctx = app.state.lanes.primary
    app.state.devenv._holding = {ctx.id}
    called = []

    async def ensure(*args, **kwargs):
        called.append(args)

    ctx.runtime.ensure = ensure
    ctx.scheduler.hold = app.state.lanes._holds
    from slotserve.orm import LlmModel
    from slotserve.runtime.scheduler import Target

    async def fake_target(*a, **k):
        return Target(LlmModel(id=1, name="m"), None, 1.0, "preload")

    import slotserve.runtime.scheduler as sched

    original = sched.compute_target
    sched.compute_target = fake_target
    try:
        await ctx.scheduler.tick()
        assert not called and ctx.scheduler.target.reason == "preload"
        app.state.devenv._holding = set()
        await ctx.scheduler.tick()
        assert called
    finally:
        sched.compute_target = original


async def test_projects(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    tpl = await bob.patch(f"/api/devenv/templates/{torch['id']}", json={"persist_paths": ["/root"]})
    assert tpl.status_code == 403  # built-ins belong to admins
    r = await admin.patch(f"/api/devenv/templates/{torch['id']}", json={"persist_paths": ["/etc"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_path_invalid"
    r = await admin.patch(f"/api/devenv/templates/{torch['id']}", json={"persist_paths": ["/root", "/opt/conda"]})
    assert r.json()["persist_paths"] == ["/root", "/opt/conda"]

    start = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=1)).isoformat(),
            "devenv_template_id": torch["id"],
            "devenv_project_name": "Thesis",
        },
    )
    assert r.status_code == 201, r.text
    booking = r.json()[0]
    assert booking["devenv_project_name"] == "Thesis"
    [project] = (await bob.get("/api/devenv/projects")).json()
    assert (
        project["state"] == "new" and project["persist_paths"] == ["/root", "/opt/conda"] and project["upcoming"] == 1
    )
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running" and session["project_name"] == "Thesis"
    [container] = docker.containers.values()
    assert container["Name"] == f"slotserve-proj-{project['id']}"
    assert container["Config"]["Labels"]["slotserve.project"] == str(project["id"])
    mounts = container["Config"]["HostConfig"]["Mounts"]
    assert {"Type": "volume", "Source": f"slotserve-project{project['id']}-root", "Target": "/root"} in mounts
    assert {"Type": "volume", "Source": f"slotserve-project{project['id']}-opt-conda", "Target": "/opt/conda"} in mounts
    assert (await bob.get("/api/devenv/projects")).json()[0]["state"] == "running"

    # the booking ends: the container is stopped and kept
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    assert container["Id"] in docker.containers and not container["Running"]
    [project] = (await bob.get("/api/devenv/projects")).json()
    assert project["state"] == "paused" and project["size_bytes"] == 4096 and project["last_used_at"]

    # continuing it starts the same container again, with fresh keys
    created = len(docker.created)
    newer = ed25519_key("new@laptop")
    await bob.post("/api/ssh-keys", json={"public_key": newer})
    booking = (
        await bob.post(
            "/api/reservations",
            json={
                "start_at": utcnow().isoformat(),
                "end_at": (utcnow() + timedelta(hours=1)).isoformat(),
                "devenv_project_id": project["id"],
            },
        )
    ).json()[0]
    assert booking["devenv_template_id"] == torch["id"] and booking["devenv_project_id"] == project["id"]
    await _ticks(app)
    assert len(docker.created) == created and container["Running"]
    assert newer in docker.archives[container["Id"]]["opt/slotserve/ssh/authorized_keys"].decode()
    session = next(s for s in (await bob.get("/api/devenv/sessions")).json() if s["state"] == "running")
    assert session["ssh_port"] == 2200

    # the same project cannot run twice at once (here: on a second lane)
    lane2 = await admin.post("/api/admin/lanes", json={"name": "Second", "gpus": []})
    assert lane2.status_code == 201, lane2.text
    later = utcnow() + timedelta(minutes=30)
    r = await bob.post(
        "/api/reservations",
        json={
            "start_at": later.isoformat(),
            "end_at": (later + timedelta(hours=1)).isoformat(),
            "devenv_project_id": project["id"],
            "lane_id": lane2.json()["id"],
        },
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "devenv_project_busy"
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    assert not container["Running"]

    # a lane with other GPUs: the container is snapshotted and recreated from the snapshot
    lanes = (await admin.get("/api/lanes")).json()
    first = lanes[0]
    async with SessionLocal() as db:
        await db.execute(update(DevProject).values(container_devices="7"))
        await db.commit()
    booking = (
        await bob.post(
            "/api/reservations",
            json={
                "start_at": utcnow().isoformat(),
                "end_at": (utcnow() + timedelta(hours=1)).isoformat(),
                "devenv_project_id": project["id"],
                "lane_id": first["id"],
            },
        )
    ).json()[0]
    await _ticks(app)
    assert docker.commits and docker.commits[-1][0] == container["Id"]
    snapshot = docker.commits[-1][1]
    assert snapshot.startswith(f"slotserve/project-{project['id']}:")
    assert container["Id"] not in docker.containers
    [new_container] = [c for c in docker.containers.values() if c["Running"]]
    assert new_container["Config"]["Image"] == snapshot and new_container["Name"] == container["Name"]
    [project] = (await bob.get("/api/devenv/projects")).json()
    assert project["image"] == snapshot and project["state"] == "running"
    r = await bob.delete(f"/api/devenv/projects/{project['id']}")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_project_in_use"
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)

    # limits
    await admin.put("/api/admin/settings", json={"devenv_projects": 1})
    r = await bob.post("/api/devenv/projects", json={"name": "Second", "template_id": torch["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_project_limit"
    r = await bob.patch(f"/api/devenv/projects/{project['id']}", json={"name": "Thesis 2", "persist_paths": ["/data"]})
    assert r.json()["name"] == "Thesis 2" and r.json()["persist_paths"] == ["/data"]
    eve = await make_client("eve")
    assert (await eve.delete(f"/api/devenv/projects/{project['id']}")).status_code == 404

    # deleting it removes the container, the snapshot and (when asked) its kept data
    r = await bob.delete(f"/api/devenv/projects/{project['id']}?delete_data=true")
    assert r.status_code == 204
    assert not docker.containers and snapshot not in docker.images
    assert f"slotserve-project{project['id']}-root" in docker.removed_volumes
    assert f"slotserve-project{project['id']}-data" in docker.removed_volumes
    assert (await bob.get("/api/devenv/projects")).json() == []


async def test_project_cleanup(app, make_client):
    admin, bob = await _setup(app, make_client)
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    old = (await bob.post("/api/devenv/projects", json={"name": "Old", "template_id": torch["id"]})).json()
    fresh = (await bob.post("/api/devenv/projects", json={"name": "Fresh", "template_id": torch["id"]})).json()
    await admin.put("/api/admin/settings", json={"devenv_project_days": 7})
    async with SessionLocal() as db:
        await db.execute(
            update(DevProject).where(DevProject.id == old["id"]).values(last_used_at=utcnow() - timedelta(days=8))
        )
        await db.commit()
    app.state.devenv._housekept_at = None
    await _ticks(app, 1)
    assert [p["id"] for p in (await bob.get("/api/devenv/projects")).json()] == [fresh["id"]]


async def test_storage_dir(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    r = await admin.put("/api/admin/settings", json={"devenv_storage_dir": "relative/path"})
    assert r.status_code == 400
    assert (await admin.put("/api/admin/settings", json={"devenv_storage_dir": "/srv/slotserve/"})).status_code == 200
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    t = (
        await bob.post(
            "/api/devenv/templates",
            json={"name": "Conda", "image": "example/conda:1", "persist_paths": ["/opt/conda"]},
        )
    ).json()
    await _book(bob, t["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running", session
    uid = (await bob.get("/api/auth/me")).json()["id"]
    helper_name, helper = next((n, c) for n, c in docker.created if n.startswith("slotserve-prep-"))
    assert helper["HostConfig"]["Binds"] == ["/srv/slotserve:/slotserve-storage"]
    script = helper["Entrypoint"][2]
    assert f"/slotserve-storage/users/u{uid}/workspace" in script
    assert f'cp -a "/opt/conda/." "/slotserve-storage/users/u{uid}/t{t["id"]}/opt-conda/"' in script
    assert not any(c["Name"] == helper_name for c in docker.containers.values())  # removed after use
    [container] = docker.containers.values()
    mounts = container["Config"]["HostConfig"]["Mounts"]
    assert {
        "Type": "bind",
        "Source": f"/srv/slotserve/users/u{uid}/workspace",
        "Target": "/workspace",
        "ReadOnly": False,
    } in mounts
    assert any(m["Source"] == f"/srv/slotserve/users/u{uid}/t{t['id']}/opt-conda" for m in mounts)


async def test_storage_helper_failure(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    await admin.put("/api/admin/settings", json={"devenv_storage_dir": "/srv/slotserve"})
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    docker.wait_code = 1
    await _book(bob, torch["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "failed" and session["error"] == "devenv_storage_failed"


async def test_old_session_table_is_rebuilt(tmp_path):
    """A pre-release 0.9 database (NOT NULL booking columns) gets a devenv_sessions table shared sessions fit in."""
    import sqlite3

    from slotserve import db as dbmod

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE devenv_sessions (id INTEGER NOT NULL PRIMARY KEY, reservation_id INTEGER NOT NULL UNIQUE,
        user_id INTEGER NOT NULL, template_id INTEGER, lane_id INTEGER, image VARCHAR(255) NOT NULL,
        state VARCHAR(16) NOT NULL, detail TEXT, progress FLOAT, error TEXT, container_id VARCHAR(80),
        container_name VARCHAR(80), ssh_port INTEGER, created_at DATETIME NOT NULL, started_at DATETIME,
        stopped_at DATETIME)"""
    )
    con.execute("CREATE INDEX ix_devenv_sessions_user_id ON devenv_sessions (user_id)")
    con.execute(
        "INSERT INTO devenv_sessions (id, reservation_id, user_id, image, state, created_at) "
        "VALUES (5, 9, 1, 'img', 'stopped', '2026-01-01 00:00:00')"  # its booking is gone: not copied
    )
    con.commit()
    con.close()
    dbmod.prepare(path, tmp_path / "backups")
    dbmod.configure(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await dbmod.create_all()
        async with dbmod.SessionLocal() as db:
            assert await db.get(DevSession, 5) is None
            db.add(DevSession(kind="shared", image="x", state="waiting"))
            await db.commit()
            assert [s.kind for s in (await db.execute(select(DevSession))).scalars()] == ["shared"]
    finally:
        await dbmod.dispose()
