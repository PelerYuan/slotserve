"""Dev environments without a GPU: CPU placement, limits, switching projects to and from bookings, usage."""

import asyncio
from datetime import timedelta

from sqlalchemy import delete, select
from test_v09_devenv import _book, _setup, _ticks, ed25519_key

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import AuditLog, DevProject, DevSession, UsageSample
from slotserve.services.devenv_cpu import cpu_ranges, layout_cores, parse_stats


def test_cpu_ranges_and_layout():
    assert cpu_ranges([]) == ""
    assert cpu_ranges([5, 0, 1, 2, 7, 8]) == "0-2,5,7-8"
    assert cpu_ranges([3]) == "3"

    # two lanes with 2 and 1 GPUs, 8 cores per GPU, 32 cores: 0-15, 16-23, pool 24-31
    lay = layout_cores(32, [(1, 2), (2, 1)], 8)
    assert lay.lanes == {1: list(range(16)), 2: list(range(16, 24))} and lay.pool == list(range(24, 32))
    # nothing reserved
    lay = layout_cores(16, [(1, 2)], 0)
    assert lay.lanes == {1: []} and lay.pool == list(range(16))
    # more wanted than there is: shares, and one core always stays in the pool
    lay = layout_cores(8, [(1, 2), (2, 2)], 8)
    assert len(lay.pool) == 1 and sorted(len(c) for c in lay.lanes.values()) == [3, 4]
    lay = layout_cores(4, [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1)], 8)
    assert len(lay.pool) == 1 and sum(len(c) for c in lay.lanes.values()) == 3
    assert all(len(lay.lanes[i]) == 1 for i in (1, 2, 3))
    assert layout_cores(0, [(1, 1)], 8).pool == []


def test_parse_stats():
    st = {
        "cpu_stats": {"cpu_usage": {"total_usage": 5_000_000_000}, "online_cpus": 8},
        "memory_stats": {"usage": 3 * 2**30, "limit": 16 * 2**30, "stats": {"inactive_file": 2**30}},
    }
    assert parse_stats(st) == (5_000_000_000, 2 * 2**30, 16 * 2**30, 8)
    v1 = {"cpu_stats": {"cpu_usage": {"total_usage": 1}}, "memory_stats": {"usage": 100, "stats": {"cache": 40}}}
    assert parse_stats(v1) == (1, 60, None, None)
    assert parse_stats({})[0] is None


async def _stop_sampler():
    tasks = [t for t in asyncio.all_tasks() if t.get_name() == "usage-sampler"]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    async with SessionLocal() as db:
        await db.execute(delete(UsageSample))
        await db.commit()


async def test_cpu_environment(app, make_client):
    docker = app.state.fake_docker
    await _stop_sampler()
    admin, bob = await _setup(app, make_client)
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")

    info = (await bob.get("/api/devenv/cpu")).json()
    assert info["enabled"] and info["per_user"] == 2 and info["running"] == 0
    # one lane that does not restrict its GPUs counts as one GPU: 8 of the 32 cores are its own
    assert info["host_cpus"] == 32 and info["pool"] == "8-31" and info["pool_cpus"] == 24
    assert [(x["cpus"], x["cpuset"]) for x in info["lanes"]] == [(8, "0-7")]

    # a way to log in is needed first
    r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ssh_login_required"
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    assert (await bob.post("/api/devenv/cpu/start", json={})).status_code == 400

    r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"]})
    assert r.status_code == 201, r.text
    started = r.json()
    assert started["kind"] == "cpu" and started["reservation_id"] is None and started["lane_id"] is None
    await _ticks(app)
    [session] = [s for s in (await bob.get("/api/devenv/sessions")).json() if s["id"] == started["id"]]
    assert session["state"] == "running" and session["ssh_command"].startswith("ssh -p 2200 root@")
    [container] = docker.containers.values()
    host = container["Config"]["HostConfig"]
    assert "DeviceRequests" not in host
    assert host["CpusetCpus"] == "8-31" and host["NanoCpus"] == 4 * 10**9 and host["CpuShares"] == 512
    assert host["Memory"] == 16 * 2**30
    assert "NVIDIA_VISIBLE_DEVICES=void" in container["Config"]["Env"]
    assert container["Config"]["Labels"]["slotserve.kind"] == "cpu"

    # limits: per person, and the memory pool
    await admin.put("/api/admin/settings", json={"devenv_cpu_per_user": 1})
    r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_limit"
    await admin.put("/api/admin/settings", json={"devenv_cpu_memory_pool_gb": 20})
    eve = await make_client("eve")
    await eve.post("/api/ssh-keys", json={"public_key": ed25519_key("eve")})
    r = await eve.post("/api/devenv/cpu/start", json={"template_id": torch["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_memory_full"
    assert r.json()["detail"]["params"] == {"free": "4", "need": "16"}

    # live use and usage rows: CPU time since the start counts
    docker.cpu_ns[container["Id"]] = 7200 * 10**9
    docker.mem_used[container["Id"]] = 3 * 2**30
    await app.state.devenv.cpu.poll()
    docker.cpu_ns[container["Id"]] += 20 * 10**9
    await app.state.devenv.cpu.poll()
    info = (await bob.get("/api/devenv/cpu")).json()
    [live] = info["sessions"]
    assert live["mine"] and live["state"] == "running" and live["live"]["memory"] == 3 * 2**30
    assert live["live"]["cpu"] > 0 and live["live"]["memory_limit"] == 16 * 2**30
    assert info["running"] == 1 and info["memory_claimed_gb"] == 16
    assert (await eve.get("/api/devenv/cpu")).json()["sessions"] == []
    assert len((await admin.get("/api/devenv/cpu")).json()["sessions"]) == 1

    await app.state.usage_sampler.sample(utcnow())
    async with SessionLocal() as db:
        [row] = (await db.execute(select(UsageSample).where(UsageSample.resource == "cpu"))).scalars().all()
    bob_id = (await bob.get("/api/auth/me")).json()["id"]
    assert row.kind == "cpu_devenv" and row.user_id == bob_id and row.lane_id is None and row.gpu_index == -1
    assert row.cpu_core_seconds == 7220 and row.mem_peak_mb == 3072 and row.mem_total_mb == 16384
    assert 0 < row.util_seconds <= 100 * row.seconds
    usage = (await admin.get("/api/usage", params={"days": 7})).json()
    assert usage["totals"]["cpu_core_hours"] == round(7220 / 3600, 2)
    [me] = [u for u in usage["users"] if u["username"] == "bob"]
    assert me["cpu_core_hours"] == round(7220 / 3600, 2)

    # someone else cannot pause it; an admin can (and it is audited); the container goes away
    assert (await eve.post(f"/api/devenv/cpu/sessions/{started['id']}/stop")).status_code == 404
    assert (await admin.post(f"/api/devenv/cpu/sessions/{started['id']}/stop")).status_code == 202
    assert not docker.containers
    async with SessionLocal() as db:
        assert (await db.get(DevSession, started["id"])).state == "stopped"
        audit = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "devenv.cpu_stop" in audit
    r = await bob.post(f"/api/devenv/cpu/sessions/{started['id']}/stop")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_not_running"
    assert (await bob.get("/api/devenv/cpu")).json()["running"] == 0

    # switched off
    await admin.put("/api/admin/settings", json={"devenv_cpu_enabled": False})
    r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_disabled"
    assert (await bob.get("/api/devenv/cpu")).json()["enabled"] is False


async def test_booking_gets_lane_cores(app, make_client):
    docker = app.state.fake_docker
    admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    await admin.put("/api/admin/settings", json={"devenv_cpu_cores_per_gpu": 4})
    app.state.devenv.cpu._host_at = None
    booking = (await _book(bob, torch["id"])).json()[0]
    await _ticks(app)
    [container] = docker.containers.values()
    host = container["Config"]["HostConfig"]
    assert host["CpusetCpus"] == "0-3" and host["DeviceRequests"] and "CpuShares" not in host
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 2)

    # Docker cannot say how many cores there are: no cpuset at all
    docker.ncpu = 0
    app.state.devenv.cpu._host_at = None
    await _book(bob, torch["id"])
    await _ticks(app)
    [container] = [c for c in docker.containers.values() if c["Running"]]
    assert "CpusetCpus" not in container["Config"]["HostConfig"]


async def test_project_switches_between_cpu_and_booking(app, make_client):
    docker = app.state.fake_docker
    _admin, bob = await _setup(app, make_client)
    await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
    torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
    project = (await bob.post("/api/devenv/projects", json={"name": "Thesis", "template_id": torch["id"]})).json()
    eve = await make_client("eve")
    assert (await eve.post("/api/devenv/cpu/start", json={"project_id": project["id"]})).status_code == 404

    # without a GPU first
    r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"]})
    assert r.status_code == 201, r.text
    cpu_id = r.json()["id"]
    await _ticks(app)
    [container] = docker.containers.values()
    assert (
        container["Name"] == f"slotserve-proj-{project['id']}"
        and "DeviceRequests" not in container["Config"]["HostConfig"]
    )
    async with SessionLocal() as db:
        p = await db.get(DevProject, project["id"])
        assert p.state == "running" and p.container_devices == "cpu"
    r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_project_running"

    # pausing keeps the container; continuing reuses it with up-to-date limits
    await bob.post(f"/api/devenv/cpu/sessions/{cpu_id}/stop")
    assert container["Id"] in docker.containers and not container["Running"]
    assert (await bob.get("/api/devenv/projects")).json()[0]["state"] == "paused"
    created = len(docker.created)
    cpu_id = (await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"]})).json()["id"]
    await _ticks(app)
    assert len(docker.created) == created and container["Running"]
    assert docker.updates and docker.updates[-1][0] == container["Id"]
    assert docker.updates[-1][1]["CpusetCpus"] == "8-31" and docker.updates[-1][1]["NanoCpus"] == 4 * 10**9

    # the booking starts: the CPU-only container is stopped and recreated with the lane's GPUs
    await bob.put(f"/api/devenv/cpu/projects/{project['id']}", json={"after_booking": True})
    assert (await bob.get("/api/devenv/cpu")).json()["after_booking"] == [project["id"]]
    now = utcnow()
    r = await bob.post(
        "/api/reservations",
        json={
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
            "devenv_project_id": project["id"],
        },
    )
    assert r.status_code == 201, r.text
    booking = r.json()[0]
    r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"]})
    assert r.status_code == 400  # running, and booked now
    await _ticks(app, 4)
    async with SessionLocal() as db:
        assert (await db.get(DevSession, cpu_id)).state == "stopped"
        gpu = (await db.execute(select(DevSession).where(DevSession.reservation_id == booking["id"]))).scalar_one()
        assert gpu.state == "running"
        p = await db.get(DevProject, project["id"])
        assert p.container_devices != "cpu" and p.image.startswith(f"slotserve/project-{project['id']}:")
    assert docker.commits and docker.commits[-1][0] == container["Id"]
    [running] = [c for c in docker.containers.values() if c["Running"]]
    assert running["Config"]["HostConfig"]["DeviceRequests"] and running["Config"]["HostConfig"]["CpusetCpus"] == "0-7"
    r = await bob.post("/api/devenv/cpu/start", json={"project_id": project["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_project_running"

    # the booking ends: the project continues without a GPU (its owner asked for that)
    await bob.delete(f"/api/reservations/{booking['id']}")
    await _ticks(app, 4)
    async with SessionLocal() as db:
        again = (
            await db.execute(select(DevSession).where(DevSession.kind == "cpu", DevSession.state == "running"))
        ).scalar_one()
        assert again.project_id == project["id"]
        assert (await db.get(DevProject, project["id"])).container_devices == "cpu"
    [running] = [c for c in docker.containers.values() if c["Running"]]
    assert "DeviceRequests" not in running["Config"]["HostConfig"]
    await bob.post(f"/api/devenv/cpu/sessions/{again.id}/stop")
    assert (await bob.get("/api/devenv/projects")).json()[0]["state"] == "paused"
