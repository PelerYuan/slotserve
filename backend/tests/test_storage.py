"""Storage managed in the panel: host directories, NFS and SMB shares for shared data, personal spaces, project
data and model libraries."""

from datetime import timedelta

from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import User
from slotserve.services import storage as svc

DF = "==DF==\nserver:/export 1000000 250000 750000 25% /s\n==WRITE== ok\n"


def _events(app) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    app.state.notifier.emit = lambda event, **data: events.append((event, data))
    return events


async def _add(admin, **body):
    r = await admin.post("/api/admin/storage", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _code(r) -> str:
    return r.json()["detail"]["code"]


async def test_validation_and_secrets(app, make_client):
    admin, bob = await _setup(app, make_client)
    assert (await bob.get("/api/admin/storage")).status_code == 403
    assert (
        await bob.post("/api/admin/storage", json={"name": "x", "kind": "host", "purpose": "shared"})
    ).status_code == 403
    bad = [
        (
            {"kind": "host", "purpose": "shared", "host_path": "relative", "mount_path": "/data/x"},
            "storage_path_invalid",
        ),
        ({"kind": "host", "purpose": "shared", "host_path": "/etc/x", "mount_path": "/data/x"}, "storage_path_invalid"),
        (
            {"kind": "host", "purpose": "shared", "host_path": "/srv/a/../b", "mount_path": "/data"},
            "storage_path_invalid",
        ),
        ({"kind": "host", "purpose": "shared", "host_path": "/srv/a", "mount_path": "/usr/x"}, "storage_mount_invalid"),
        (
            {"kind": "host", "purpose": "shared", "host_path": "/srv/a", "mount_path": "/workspace"},
            "storage_mount_invalid",
        ),
        ({"kind": "host", "purpose": "shared", "host_path": "/srv/a", "mount_path": "/opt"}, "storage_mount_invalid"),
        ({"kind": "host", "purpose": "shared", "host_path": "/srv/a"}, "storage_mount_invalid"),
        ({"kind": "nfs", "purpose": "personal", "server": "bad host", "remote_path": "/x"}, "storage_server_invalid"),
        ({"kind": "nfs", "purpose": "personal", "server": "nas", "remote_path": "x"}, "storage_remote_invalid"),
        (
            {"kind": "nfs", "purpose": "personal", "server": "nas", "remote_path": "/x", "options": "addr=evil"},
            "storage_options_invalid",
        ),
        (
            {"kind": "nfs", "purpose": "personal", "server": "nas", "remote_path": "/x", "options": "a b"},
            "storage_options_invalid",
        ),
        (
            {
                "kind": "smb",
                "purpose": "personal",
                "server": "nas",
                "remote_path": "home",
                "username": "u",
                "password": "a,b",
            },
            "storage_password_invalid",
        ),
        ({"kind": "smb", "purpose": "models", "server": "nas", "remote_path": "models"}, "storage_local_required"),
        ({"kind": "host", "purpose": "personal", "host_path": "/srv/h", "access": "users"}, "storage_users_invalid"),
        (
            {"kind": "host", "purpose": "personal", "host_path": "/srv/h", "access": "users", "user_ids": [999]},
            "storage_users_invalid",
        ),
    ]
    for body, code in bad:
        r = await admin.post("/api/admin/storage", json={"name": "bad", **body})
        assert r.status_code == 400 and await _code(r) == code, (body, r.text)

    smb = await _add(
        admin,
        name="Team share",
        kind="smb",
        purpose="shared",
        remote_path="//nas.local/team/data",
        username="studio",
        password="s3cret-pass",
        options="vers=3.0,uid=0",
        mount_path="/data/team",
        read_only=True,
    )
    assert (
        smb["server"] == "nas.local" and smb["remote_path"] == "team/data" and smb["source"] == "//nas.local/team/data"
    )
    assert smb["has_password"] and smb["read_only"] and smb["container_path"] == "/data/team"
    listing = (await admin.get("/api/admin/storage")).text
    assert "s3cret-pass" not in listing and "password_enc" not in listing
    # same name, nested mount paths
    r = await admin.post(
        "/api/admin/storage",
        json={
            "name": "Team share",
            "kind": "host",
            "purpose": "shared",
            "host_path": "/srv/x",
            "mount_path": "/data/y",
        },
    )
    assert r.status_code == 409 and await _code(r) == "storage_name_taken"
    r = await admin.post(
        "/api/admin/storage",
        json={"name": "Other", "kind": "host", "purpose": "shared", "host_path": "/srv/x", "mount_path": "/data"},
    )
    assert r.status_code == 409 and await _code(r) == "storage_mount_taken"
    # the password is kept when not sent, removed with ""
    body = {
        k: smb[k] for k in ("name", "kind", "purpose", "server", "remote_path", "username", "mount_path", "options")
    }
    r = await admin.put(f"/api/admin/storage/{smb['id']}", json=body)
    assert r.status_code == 200 and r.json()["has_password"]
    r = await admin.put(f"/api/admin/storage/{smb['id']}", json={**body, "password": ""})
    assert not r.json()["has_password"]
    audit = (await admin.get("/api/admin/audit")).text
    assert "storage.create" in audit and "s3cret-pass" not in audit
    assert (await admin.delete(f"/api/admin/storage/{smb['id']}")).status_code == 204
    assert (await admin.get("/api/admin/storage")).json() == []


async def test_check_usage_and_quota(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    events = _events(app)
    uid = (await bob.get("/api/auth/me")).json()["id"]
    st = await _add(admin, name="Homes", kind="nfs", purpose="personal", server="10.0.0.5", remote_path="/export/homes")
    assert st["container_path"] == "/workspace" and st["status"] is None
    docker.helper_output["==DF=="] = DF + f"==DU==\n2500000\tu{uid}\n10\tu1\n"
    r = await admin.post(f"/api/admin/storage/{st['id']}/check")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == {
        "ok": True,
        "error": None,
        "total": 1000000 * 1024,
        "used": 250000 * 1024,
        "free": 750000 * 1024,
        "writable": True,
    }
    assert out["usage_total"] == (2500000 + 10) * 1024 and out["usage_people"] == 2
    # the helper mounted an NFS volume of the local driver
    [volume] = docker.volumes.values()
    assert volume["Options"] == {"type": "nfs", "o": "addr=10.0.0.5,nfsvers=4", "device": ":/export/homes"}
    assert volume["Labels"]["slotserve.storage"] == str(st["id"])
    name, helper = next((n, c) for n, c in docker.created if n.startswith("slotserve-storage-check-"))
    assert helper["Image"] == "busybox:1.37" and "busybox:1.37" in docker.images
    assert helper["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": volume["Name"], "Target": "/s", "ReadOnly": False}
    ]
    assert "DU=1" in helper["Env"] and not any(c["Name"] == name for c in docker.containers.values())
    usage = (await admin.get(f"/api/admin/storage/{st['id']}/usage")).json()
    assert usage[0] == {"user_id": uid, "username": "bob", "bytes": 2500000 * 1024, "over": False}
    assert events == []

    # a soft limit: bob (2.56 GB) is over 2 GB; told once, admins too
    body = {"name": "Homes", "kind": "nfs", "purpose": "personal", "server": "10.0.0.5", "remote_path": "/export/homes"}
    r = await admin.put(f"/api/admin/storage/{st['id']}", json={**body, "quota_gb": 2})
    assert r.json()["status"] is not None  # same mount: the last check stays
    await admin.post(f"/api/admin/storage/{st['id']}/check")
    assert [e for e, _ in events] == ["storage_quota", "storage_quota_admin"]
    assert events[0][1]["user_id"] == uid and events[1][1]["user"] == "bob" and events[0][1]["quota"] == "2.0 GB"
    await admin.post(f"/api/admin/storage/{st['id']}/check")
    assert len(events) == 2
    mine = (await bob.get("/api/storage")).json()
    assert mine == [
        {
            "id": st["id"],
            "name": "Homes",
            "purpose": "personal",
            "path": "/workspace",
            "read_only": False,
            "ok": True,
            "used": 2500000 * 1024,
            "quota_gb": 2.0,
            "over": True,
        }
    ]

    # the share goes away: the check fails, admins hear about it
    events.clear()
    docker.mount_errors[volume["Name"]] = "error while mounting volume: mount.nfs: Connection timed out"
    out = (await admin.post(f"/api/admin/storage/{st['id']}/check")).json()
    assert out["status"]["ok"] is False and "Connection timed out" in out["status"]["error"]
    assert [e for e, _ in events] == ["storage_down"]

    # other settings: a new volume; the old one is removed
    r = await admin.put(f"/api/admin/storage/{st['id']}", json={**body, "server": "10.0.0.6"})
    assert r.json()["status"] is None
    assert volume["Name"] in docker.removed_volumes and volume["Name"] not in docker.volumes


async def test_failed_helper_and_host_check(app, make_client):
    docker = app.state.fake_docker
    admin, _ = await _setup(app, make_client)
    st = await _add(
        admin, name="Data", kind="host", purpose="shared", host_path="/srv/data/", mount_path="/data", read_only=True
    )
    assert st["host_path"] == "/srv/data"
    docker.helper_output["==DF=="] = "the storage is not a directory"
    docker.wait_code = 3
    out = (await admin.post(f"/api/admin/storage/{st['id']}/check")).json()
    assert out["status"]["ok"] is False and "the storage is not a directory" in out["status"]["error"]
    docker.wait_code = None
    docker.helper_output["==DF=="] = "==DF==\n/dev/sdb1 2000 1000 1000 50% /s\n"
    out = (await admin.post(f"/api/admin/storage/{st['id']}/check")).json()
    assert out["status"]["ok"] and out["status"]["writable"] is None and out["usage_total"] is None
    helper = [c for n, c in docker.created if n.startswith("slotserve-storage-check-")][-1]
    assert helper["HostConfig"]["Mounts"] == [{"Type": "bind", "Source": "/srv/data", "Target": "/s", "ReadOnly": True}]
    assert "W=0" in helper["Env"] and not docker.volumes


async def test_environment_mounts(app, make_client):
    docker = app.state.fake_docker
    # SlotServe's own models directory is mounted at /models only on Linux hosts; leave it out so the mounts
    # below are the same on every platform
    app.state.devenv._models_source = ""
    admin, bob = await _setup(app, make_client)
    carol = await make_client("carol")
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    uid = (await bob.get("/api/auth/me")).json()["id"]
    carol_id = (await carol.get("/api/auth/me")).json()["id"]
    homes = await _add(admin, name="Homes", kind="nfs", purpose="personal", server="nas", remote_path="/homes")
    projects = await _add(admin, name="Projects", kind="host", purpose="projects", host_path="/srv/projects")
    data = await _add(
        admin, name="Data", kind="host", purpose="shared", host_path="/srv/data", mount_path="/data", read_only=True
    )
    models = await _add(
        admin,
        name="Models",
        kind="smb",
        purpose="models",
        server="nas",
        remote_path="models",
        username="svc",
        password="pw",
        mount_path="/mnt/models",
        read_only=True,
        local_path="/mnt/nas-models",
    )
    await _add(
        admin,
        name="Admins",
        kind="host",
        purpose="shared",
        host_path="/srv/admin",
        mount_path="/admin",
        access="admins",
    )
    await _add(
        admin,
        name="Carol",
        kind="host",
        purpose="shared",
        host_path="/srv/carol",
        mount_path="/carol",
        access="users",
        user_ids=[carol_id],
    )
    t = (
        await bob.post(
            "/api/devenv/templates", json={"name": "Conda", "image": "example/conda:1", "persist_paths": ["/opt/conda"]}
        )
    ).json()
    await _book(bob, t["id"])
    await _ticks(app)
    [session] = (await bob.get("/api/devenv/sessions")).json()
    assert session["state"] == "running", session
    container = next(c for c in docker.containers.values() if c["Name"].startswith("slotserve-dev-"))
    mounts = {m["Target"]: m for m in container["Config"]["HostConfig"]["Mounts"]}
    assert set(mounts) == {"/workspace", "/opt/conda", "/data", "/mnt/models"}  # not /admin, not /carol
    ws = docker.volumes[mounts["/workspace"]["Source"]]
    assert ws["Options"] == {"type": "nfs", "o": "addr=nas,nfsvers=4", "device": f":/homes/users/u{uid}"}
    assert mounts["/opt/conda"] == {
        "Type": "bind",
        "Source": f"/srv/projects/templates/u{uid}-t{t['id']}/opt-conda",
        "Target": "/opt/conda",
        "ReadOnly": False,
    }
    assert mounts["/data"] == {"Type": "bind", "Source": "/srv/data", "Target": "/data", "ReadOnly": True}
    lib = docker.volumes[mounts["/mnt/models"]["Source"]]
    assert lib["Options"] == {
        "type": "cifs",
        "o": "addr=nas,username=svc,password=pw,vers=3.0",
        "device": "//nas/models",
    }
    assert mounts["/mnt/models"]["ReadOnly"] is True
    # folders were created (and seeded from the image) by helpers on each storage's root
    preps = {n: c for n, c in docker.created if n.startswith(f"slotserve-prep-{session['id']}-st")}
    assert set(preps) == {
        f"slotserve-prep-{session['id']}-st{homes['id']}",
        f"slotserve-prep-{session['id']}-st{projects['id']}",
    }
    home_prep = preps[f"slotserve-prep-{session['id']}-st{homes['id']}"]
    assert home_prep["Image"] == "example/conda:1"
    script = home_prep["Entrypoint"][2]
    assert f"mkdir -p '/slotserve-storage/users/u{uid}'" in script and "cp -a '/workspace/.'" in script
    assert "chown" not in script
    base = home_prep["HostConfig"]["Mounts"][0]
    assert base["Target"] == "/slotserve-storage" and docker.volumes[base["Source"]]["Options"]["device"] == ":/homes"
    proj_prep = preps[f"slotserve-prep-{session['id']}-st{projects['id']}"]
    assert proj_prep["HostConfig"]["Mounts"][0]["Source"] == "/srv/projects"

    # a person's own view
    mine = {s["name"]: s for s in (await bob.get("/api/storage")).json()}
    assert set(mine) == {"Homes", "Projects", "Data", "Models"}
    assert mine["Data"]["path"] == "/data" and mine["Projects"]["path"] is None
    assert {s["name"] for s in (await carol.get("/api/storage")).json()} == {
        "Homes",
        "Projects",
        "Data",
        "Models",
        "Carol",
    }
    assert models["local_path"] == "/mnt/nas-models" and data["read_only"]


async def test_shared_environment_plan(app, make_client):
    admin, _ = await _setup(app, make_client)
    await _add(admin, name="Homes", kind="host", purpose="personal", host_path="/srv/homes")
    await _add(admin, name="Data", kind="host", purpose="shared", host_path="/srv/data", mount_path="/data")
    await _add(
        admin, name="Admins", kind="host", purpose="shared", host_path="/srv/a", mount_path="/a", access="admins"
    )
    async with SessionLocal() as db:
        plan = await svc.plan_for(db, "k", None, shared=True)
        users = {u.username: u.id for u in (await db.execute(User.__table__.select())).all()}
    targets = {m["Target"]: m["Source"] for m in plan.mounts}
    assert targets == {"/workspaces": "/srv/homes", "/data": "/srv/data"}
    preps = [(p.rel, p.owner) for preps in plan.preps.values() for p in preps]
    assert (f"users/u{users['bob']}", 20000 + users["bob"]) in preps and len(preps) == 2
    assert plan.signature and plan.personal is None
    docker = app.state.fake_docker
    docker.images.add("example/shared:1")
    await svc.prepare(app.state.devenv.docker, "example/shared:1", plan, "slotserve-prep-x")
    [helper] = [c for n, c in docker.created if n.startswith("slotserve-prep-x-st")]
    script = helper["Entrypoint"][2]
    bob = users["bob"]
    assert f"chown {20000 + bob}:{20000 + bob} '/slotserve-storage/users/u{bob}' && chmod 700" in script
    assert helper["HostConfig"]["Mounts"][0] == {
        "Type": "bind",
        "Source": "/srv/homes",
        "Target": "/slotserve-storage",
        "ReadOnly": False,
    }


async def test_kept_environment_recreated_when_storage_changes(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")

    async def book(**extra):
        start = utcnow()
        r = await bob.post(
            "/api/reservations",
            json={
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "devenv_template_id": torch["id"],
                **extra,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()[0]

    booking = await book(devenv_project_name="Thesis")
    await _ticks(app)
    [project] = (await bob.get("/api/devenv/projects")).json()
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    assert not docker.commits
    # the same storages: the container is started again; a new shared storage: snapshot and recreate
    await _add(admin, name="Data", kind="host", purpose="shared", host_path="/srv/data", mount_path="/data")
    booking = await book(devenv_project_id=project["id"])
    await _ticks(app)
    assert len(docker.commits) == 1
    container = next(c for c in docker.containers.values() if c["Name"] == f"slotserve-proj-{project['id']}")
    assert any(m["Target"] == "/data" for m in container["Config"]["HostConfig"]["Mounts"])
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)
    commits = len(docker.commits)
    await book(devenv_project_id=project["id"])
    await _ticks(app)
    assert len(docker.commits) == commits and container["Running"]


async def test_model_library(app, make_client, tmp_path):
    from conftest import make_gguf

    admin, bob = await _setup(app, make_client)
    lib = tmp_path / "library"
    make_gguf(lib, "tiny-lib.gguf")
    st = await _add(
        admin, name="Library", kind="nfs", purpose="models", server="nas", remote_path="/models", local_path=str(lib)
    )
    assert st["mount_path"] is None and st["container_path"] is None
    r = await admin.post("/api/models/scan")
    assert r.status_code == 200 and "tiny-lib" in r.text
    # downloads can go into a library people may use
    r = await bob.post("/api/downloads", json={"url": "https://example.com/x.gguf", "storage_id": st["id"]})
    assert r.status_code == 201 and r.json()["storage_id"] == st["id"]
    assert app.state.downloader.dest_path("x.gguf", lib) == lib / "x.gguf"
    r = await bob.post("/api/downloads", json={"url": "https://example.com/tiny-lib.gguf", "storage_id": st["id"]})
    assert r.status_code == 409
    restricted = await _add(
        admin, name="Admin lib", kind="host", purpose="models", host_path="/srv/lib", access="admins"
    )
    r = await bob.post("/api/downloads", json={"url": "https://example.com/y.gguf", "storage_id": restricted["id"]})
    assert r.status_code == 400 and await _code(r) == "storage_library_invalid"
