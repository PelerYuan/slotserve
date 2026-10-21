"""Machines: hardware inventory (also CPU-only nodes), availability per machine, "Pick for me" and the APIs."""

import asyncio
import contextlib
import sys
from datetime import timedelta

from sqlalchemy import update
from test_cluster import _add_node, _free_port, _until, live  # noqa: F401  (fixture)
from test_v09_devenv import _setup, ed25519_key

from conftest import MOCK_SERVER
from slotserve.agent.node import NodeAgent
from slotserve.cluster import system
from slotserve.cluster.machines import (
    CapacityOut,
    MachineGpuOut,
    MachineLaneOut,
    MachineModelOut,
    MachineOut,
    cpu_capacity,
    day_window,
    lane_gpu_memory,
)
from slotserve.cluster.recommend import Need, best_per_machine, recommend
from slotserve.config import Settings
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import Lane, Reservation, User
from slotserve.schemas import GpuOut
from slotserve.services.devenv_cpu import layout_cores

# ---- inventory parsing ---------------------------------------------------------------------------------------------

CPUINFO_X86 = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Example CPU @ 3.00GHz
physical id\t: 0
core id\t: 0

processor\t: 1
model name\t: Example CPU @ 3.00GHz
physical id\t: 0
core id\t: 0

processor\t: 2
model name\t: Example CPU @ 3.00GHz
physical id\t: 0
core id\t: 1

processor\t: 3
model name\t: Example CPU @ 3.00GHz
physical id\t: 0
core id\t: 1
"""

CPUINFO_ARM = """processor\t: 0
BogoMIPS\t: 50.00
Features\t: fp asimd

processor\t: 1
BogoMIPS\t: 50.00

Hardware\t: Example Board
"""


def test_parse_cpuinfo():
    assert system.parse_cpuinfo(CPUINFO_X86) == {
        "model": "Example CPU @ 3.00GHz",
        "physical_cores": 2,
        "logical_cores": 4,
    }
    # no physical/core ids (many ARM boards): physical cores unknown
    assert system.parse_cpuinfo(CPUINFO_ARM) == {"model": "Example Board", "physical_cores": None, "logical_cores": 2}
    assert system.parse_cpuinfo("") == {"model": None, "physical_cores": None, "logical_cores": None}


def test_parse_meminfo_net_and_cuda():
    meminfo = "MemTotal:       65536000 kB\nMemFree:  1000 kB\nMemAvailable:   32768000 kB\n"
    assert system.parse_meminfo(meminfo) == (32000, 64000)
    assert system.parse_meminfo("MemTotal: 2048000 kB\nMemFree: 1024000 kB\n") == (1000, 2000)
    assert system.parse_meminfo("garbage") == (None, None)

    net = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls\n"
        "    lo: 9999 10 0 0 0 0 0 0 9999 10 0 0 0 0 0 0\n"
        "  eth0: 1000 10 0 0 0 0 0 0 2000 10 0 0 0 0 0 0\n"
        "  wlan0: 500 10 0 0 0 0 0 0 30 10 0 0 0 0 0 0\n"
    )
    assert system.parse_net_dev(net) == (1500, 2030)

    header = "| NVIDIA-SMI 550.00       Driver Version: 550.00       CUDA Version: 12.4     |"
    assert system.parse_cuda_version(header) == "12.4"
    assert system.parse_cuda_version("no driver") is None


def test_docker_summary():
    assert system.docker_summary(None) == {"available": False}
    info = {
        "ServerVersion": "27.1.0",
        "DockerRootDir": "/var/lib/docker",
        "Runtimes": {"runc": {}, "nvidia": {}},
        "DefaultRuntime": "runc",
        "NCPU": 16,
        "MemTotal": 64 * 2**30,
    }
    out = system.docker_summary(info)
    assert out["available"] and out["gpu_runtime"] and out["runtimes"] == ["nvidia", "runc"]
    assert out["version"] == "27.1.0" and out["root_dir"] == "/var/lib/docker"
    assert not system.docker_summary({"ServerVersion": "27", "Runtimes": {"runc": {}}})["gpu_runtime"]


async def test_hardware_without_gpu_or_docker(monkeypatch, tmp_path):
    """A CPU-only machine without Docker access: no nvidia-smi is needed, everything degrades cleanly."""
    monkeypatch.setattr(system.shutil, "which", lambda _name: None)
    monkeypatch.setattr(system, "_driver_cache", None)

    async def no_docker():
        raise OSError("no docker socket")

    hw = await system.hardware(tmp_path / "models-not-created-yet", no_docker)
    assert hw["gpu_driver"] is None
    assert hw["docker"] == {"available": False}
    assert hw["cpu_logical_cores"] and hw["cpu_logical_cores"] >= 1
    assert hw["os"] and hw["arch"] is not None
    [models] = hw["disks"]  # the directory does not exist: its parent's file system
    assert models["label"] == "models" and models["total_bytes"] > 0

    async def docker():
        return {"ServerVersion": "27.0", "DockerRootDir": str(tmp_path), "Runtimes": {"runc": {}}}

    hw = await system.hardware(tmp_path, docker)
    assert hw["docker"]["available"] and not hw["docker"]["gpu_runtime"]
    assert [d["label"] for d in hw["disks"]] == ["models", "docker"]

    probe = system.LoadProbe()
    reading = probe.read(hw["disks"])
    assert set(reading) >= {"cpu_percent", "memory_used_mb", "memory_total_mb", "net_rx_bps", "disks"}


# ---- availability (pure) -------------------------------------------------------------------------------------------


def test_cpu_capacity():
    # 32 cores, lanes with 2 and 1 GPUs at 8 cores per GPU: 24 reserved, pool of 8; two environments of 2 cores
    cap = cpu_capacity(32, [2, 1], 8, [2.0, 2.0], 64000, 10000, [16384, 16384])
    assert cap["cpu_cores_lanes"] == 24 and cap["cpu_cores_envs"] == 4.0 and cap["cpu_cores_free"] == 4.0
    assert cap["memory_reserved_mb"] == 32768 and cap["memory_free_mb"] == 64000 - 32768
    # memory in use counts when it is more than what environments claim
    assert cpu_capacity(32, [], 8, [], 64000, 50000, [1000])["memory_free_mb"] == 14000
    # a CPU-only machine: nothing reserved for lanes
    cap = cpu_capacity(8, [], 8, [], 16000, None, [])
    assert cap["cpu_cores_lanes"] == 0 and cap["cpu_cores_free"] == 8 and cap["memory_free_mb"] == 16000
    # unknown hardware
    cap = cpu_capacity(None, [1], 8, [1.0], None, None, [])
    assert cap["cpu_cores_free"] is None and cap["memory_free_mb"] is None
    # never negative
    assert cpu_capacity(4, [1], 8, [4.0], 1000, 2000, [])["cpu_cores_free"] == 0
    assert layout_cores(4, [(0, 1)], 8).pool == [3]


def test_lane_gpu_memory_and_day_window():
    gpus = [
        GpuOut(index=i, name="g", memory_used_mb=100 * i, memory_total_mb=8000, utilization_pct=0, temperature_c=0)
        for i in range(3)
    ]
    assert lane_gpu_memory([0, 2], gpus) == (16000, 200)
    assert lane_gpu_memory(None, gpus) == (24000, 300)
    assert lane_gpu_memory([5], gpus) == (None, None)
    now = utcnow().replace(hour=1, minute=30)
    start, end = day_window(now, 120)  # UTC+2: local 03:30, today began at 22:00 UTC the day before
    assert start == now.replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=1)
    assert end - start == timedelta(days=2)


# ---- "Pick for me" -------------------------------------------------------------------------------------------------


def _cap(**kw) -> CapacityOut:
    base = dict(
        gpus=0,
        gpus_free=0,
        lanes=0,
        lanes_free=0,
        gpu_memory_total_mb=0,
        gpu_memory_free_mb=None,
        max_lane_gpu_memory_mb=None,
        cpu_cores=16,
        cpu_cores_lanes=0,
        cpu_cores_envs=0.0,
        cpu_cores_free=16.0,
        memory_total_mb=64000,
        memory_used_mb=8000,
        memory_reserved_mb=0,
        memory_free_mb=56000,
        cpu_envs_running=0,
        cpu_envs=True,
    )
    base.update(kw)
    return CapacityOut(**base)


def _lane(lane_id: int, memory: int, free: bool = True, used: int = 0) -> MachineLaneOut:
    return MachineLaneOut(
        id=lane_id,
        name=f"lane-{lane_id}",
        gpus=[lane_id],
        gpu_memory_mb=memory,
        gpu_memory_used_mb=used,
        state="ready",
        model_name=None,
        free_now=free,
    )


def _machine(node_id: int, name: str, lanes=(), online=True, cpu=10.0, models=(), **cap) -> MachineOut:
    gpus = [
        MachineGpuOut(
            index=lane.id,
            name="GPU",
            memory_total_mb=lane.gpu_memory_mb,
            memory_used_mb=lane.gpu_memory_used_mb,
            utilization_pct=0,
            lane_id=lane.id,
        )
        for lane in lanes
    ]
    return MachineOut(
        id=node_id,
        name=name,
        is_local=node_id == 1,
        status="online" if online else "offline",
        online=online,
        last_seen_at=None,
        kind="gpu" if lanes else "cpu",
        cpu_percent=cpu if online else None,
        gpus=gpus,
        lanes=list(lanes),
        capacity=_cap(**cap),
        models=[MachineModelOut(id=m, name=f"m{m}", size_bytes=1) for m in models],
    )


def _fleet() -> list[MachineOut]:
    return [
        _machine(1, "big", [_lane(11, 48000, free=False, used=40000), _lane(12, 24000)], models=[7]),
        _machine(2, "small", [_lane(21, 16000)], models=[7, 8]),
        _machine(3, "cpu-box", cpu_cores=32, cpu_cores_free=30.0, memory_total_mb=128000, memory_free_mb=120000),
        _machine(4, "away", [_lane(41, 80000)], online=False, models=[7]),
    ]


def test_recommend_gpu():
    fleet = _fleet()
    # 20 GB of GPU memory, now: the free 24 GB lane of "big"; the busy 48 GB lane and the 16 GB lane lose
    best, options = recommend(fleet, Need(gpu=True, min_gpu_memory_mb=20000))
    assert (best.node_name, best.lane_id) == ("big", 12)
    by_lane = {c.lane_id: c for c in options}
    assert not by_lane[21].fits and "gpu_memory_short" in by_lane[21].reasons
    assert "lane_busy" in by_lane[11].reasons
    assert "offline" in by_lane[41].reasons and by_lane[41].score < by_lane[12].score
    assert any(c.node_name == "cpu-box" and "no_gpu" in c.reasons and not c.fits for c in options)

    # a booking for later: lane 12 is booked in that window, the offline machine's lane is the only big one left
    best, options = recommend(fleet, Need(gpu=True, min_gpu_memory_mb=20000, purpose="booking", taken_lanes={12}))
    assert best.lane_id == 11  # free in the window; the offline machine sinks
    assert "lane_booked" in next(c for c in options if c.lane_id == 12).reasons

    # the model must be on the machine
    best, options = recommend(fleet, Need(gpu=True, model_id=8))
    assert best.node_name == "small"
    assert all("model_missing" in c.reasons for c in options if c.node_name == "big")

    # nothing fits: no best, but every option with reasons
    best, options = recommend(fleet, Need(gpu=True, min_gpu_memory_mb=200000))
    assert best is None and options and not any(c.fits for c in options)
    assert [c.node_name for c in best_per_machine(options)].count("big") == 1


def test_recommend_cpu():
    fleet = _fleet()
    # CPU work prefers the CPU-only machine (most free cores; GPU machines keep their cores for GPU users)
    best, _ = recommend(fleet, Need(gpu=False, cores=8, memory_mb=32000, purpose="cpu_env"))
    assert best.node_name == "cpu-box" and best.lane_id is None
    # too much asked
    best, options = recommend(fleet, Need(gpu=False, cores=64, purpose="cpu_env"))
    assert best is None and all("cores_short" in c.reasons for c in options if c.node_name != "away")
    assert "offline" in next(c for c in options if c.node_name == "away").reasons
    # where CPU-only environments cannot start, a cpu_env request does not fit
    fleet[2] = _machine(3, "cpu-box", cpu_cores=32, cpu_cores_free=30.0, cpu_envs=False)
    best, options = recommend(fleet, Need(gpu=False, cores=2, purpose="cpu_env"))
    assert best.node_name != "cpu-box"
    assert "cpu_envs_unsupported" in next(c for c in options if c.node_name == "cpu-box").reasons
    # a busy machine is avoided
    fleet[2] = _machine(3, "cpu-box", cpu=97.0, cpu_cores=32, cpu_cores_free=30.0)
    best, options = recommend(fleet, Need(gpu=False, cores=2))
    busy = next(c for c in options if c.node_name == "cpu-box")
    assert "busy" in busy.reasons and best.node_name != "cpu-box"
    # paused sharing (set by the owner) takes a machine out
    fleet[2] = _machine(3, "cpu-box", cpu_cores=32, cpu_cores_free=30.0)
    fleet[2].sharing_paused = True
    best, options = recommend(fleet, Need(gpu=False, cores=2))
    assert best.node_name != "cpu-box" and "sharing_paused" in next(c for c in options if c.node_id == 3).reasons


def test_recommend_either():
    best, options = recommend(_fleet(), Need(gpu=None, cores=2))
    assert best is not None and best.fits
    assert {c.lane_id is None for c in options} == {True, False}  # GPU lanes and CPU options both offered


# ---- APIs ----------------------------------------------------------------------------------------------------------


def _fake_gpus(memory: int = 24000):
    async def stats() -> list[GpuOut]:
        return [
            GpuOut(
                index=i,
                name="Test GPU",
                memory_used_mb=1000 * (i + 1),
                memory_total_mb=memory,
                utilization_pct=10 * (i + 1),
                temperature_c=40,
                power_w=100.0,
            )
            for i in range(2)
        ]

    return stats


async def _two_lanes(app) -> tuple[int, int]:
    async with SessionLocal() as db:
        await db.execute(update(Lane).values(gpu_indices="0"))
        lane = Lane(name="second", gpu_indices="1", position=5)
        db.add(lane)
        await db.commit()
        first = (await db.execute(Lane.__table__.select().order_by(Lane.id))).first().id
        second = lane.id
    await app.state.lanes.sync()
    return first, second


async def test_machines_api_local(app, make_client, monkeypatch):
    monkeypatch.setattr("slotserve.runtime.gpu.gpu_stats", _fake_gpus())
    admin = await make_client("root")
    bob = await make_client("bob")
    alice = await make_client("alice")
    first, second = await _two_lanes(app)
    now = utcnow()
    async with SessionLocal() as db:
        bob_id = (await db.execute(User.__table__.select().where(User.username == "bob"))).first().id
        db.add(
            Reservation(
                user_id=bob_id, lane_id=first, start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=1)
            )
        )
        db.add(
            Reservation(
                user_id=bob_id, lane_id=second, start_at=now + timedelta(hours=3), end_at=now + timedelta(hours=4)
            )
        )
        await db.commit()

    r = await alice.get("/api/machines", params={"tz_offset": 0})
    assert r.status_code == 200, r.text
    [local] = r.json()
    assert local["is_local"] and local["online"] and local["status"] == "online" and local["kind"] == "gpu"
    assert local["cpu_logical_cores"] and local["memory_total_mb"]
    assert [g["index"] for g in local["gpus"]] == [0, 1]
    assert [g["lane_id"] for g in local["gpus"]] == [first, second]
    assert local["docker"]["available"] and local["docker"]["version"] == "27.0-fake"  # the fake Docker's /info
    lanes = {lane["id"]: lane for lane in local["lanes"]}
    assert not lanes[first]["free_now"] and lanes[first]["holder"] == "bob" and not lanes[first]["holder_mine"]
    assert lanes[second]["free_now"] and lanes[second]["holder"] is None
    assert lanes[first]["gpu_memory_mb"] == 24000 and lanes[first]["gpu_memory_used_mb"] == 1000
    assert lanes[second]["next_start_at"] is not None
    assert any(b["username"] == "bob" and b["kind"] == "exclusive" for b in lanes[second]["bookings"])
    cap = local["capacity"]
    assert cap["gpus"] == 2 and cap["gpus_free"] == 1 and cap["lanes"] == 2 and cap["lanes_free"] == 1
    assert cap["max_lane_gpu_memory_mb"] == 24000 and cap["gpu_memory_free_mb"] == 48000 - 3000
    cores = local["cpu_logical_cores"]
    layout = layout_cores(cores, [(0, 1), (1, 1)], app.state.settings.devenv_cpu_cores_per_gpu)
    assert cap["cpu_cores_lanes"] == sum(len(c) for c in layout.lanes.values())
    assert cap["cpu_cores_free"] == len(layout.pool)
    assert local["owner"] is None and local["sharing_paused"] is False  # hooks for sharing schedules
    assert (await bob.get("/api/machines")).json()[0]["lanes"][0]["holder_mine"]

    one = await alice.get(f"/api/machines/{local['id']}")
    assert one.status_code == 200 and one.json()["name"] == local["name"]
    assert (await alice.get("/api/machines/999")).status_code == 404

    # Pick for me: a GPU with 20 GB now -> the free lane
    r = await alice.post("/api/machines/recommend", json={"gpu": True, "min_gpu_memory_mb": 20000})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["best"]["lane_id"] == second and data["best"]["node_id"] == local["id"]
    assert len(data["machines"]) == 1 and len(data["options"]) == 2
    # a booking window overlapping bob's later booking on the second lane -> the first lane (free by then)
    window = {"start_at": (now + timedelta(hours=3)).isoformat(), "end_at": (now + timedelta(hours=5)).isoformat()}
    data = (await alice.post("/api/machines/recommend", json={"gpu": True, "purpose": "booking", **window})).json()
    assert data["best"]["lane_id"] == first
    assert "lane_booked" in next(o for o in data["options"] if o["lane_id"] == second)["reasons"]
    # too much GPU memory: nothing fits
    data = (await alice.post("/api/machines/recommend", json={"gpu": True, "min_gpu_memory_mb": 10**6})).json()
    assert data["best"] is None and all("gpu_memory_short" in o["reasons"] for o in data["options"])
    bad = await alice.post("/api/machines/recommend", json={"start_at": now.isoformat()})
    assert bad.status_code == 422
    # CPU-only environments are off by default (dev environments disabled)
    data = (await alice.post("/api/machines/recommend", json={"gpu": False, "purpose": "cpu_env"})).json()
    assert data["best"] is None and "cpu_envs_unsupported" in data["options"][0]["reasons"]

    # load history: one point per sample, and 24 hours of lane GPU utilization
    await app.state.machines.sample()
    hist = (await alice.get(f"/api/machines/{local['id']}/history")).json()
    assert hist["points"] and hist["points"][-1]["gpu_util_pct"] == 15.0  # (the background loop samples too)
    assert len(hist["gpu_hours"]) == 24 and hist["interval_seconds"] == 60

    # the usage page can be narrowed to one machine
    r = await admin.get("/api/usage", params={"node_id": local["id"]})
    assert r.status_code == 200 and {lane["lane_id"] for lane in r.json()["lanes"]} <= {first, second}
    assert (await admin.get("/api/usage", params={"node_id": 999})).json()["lanes"] == []


async def test_cpu_only_node_and_cpu_start_choice(app, make_client, live, tmp_path, monkeypatch):  # noqa: F811
    """A node agent without GPUs: reports CPU, memory, disks and Docker, has no lanes and offers CPU compute."""
    monkeypatch.setattr("slotserve.runtime.gpu.gpu_stats", _fake_gpus())
    monkeypatch.setattr(system, "gpu_driver", lambda: None)  # the node has no GPU driver
    admin, bob = await _setup(app, make_client)
    created = await _add_node(admin, "cpu-box")
    root = tmp_path / "node-cpu"
    (root / "models").mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        data_dir=root / "data",
        models_dir=root / "models",
        llama_server_cmd=f'"{sys.executable}" "{MOCK_SERVER}"',
        llama_port=_free_port(),
        agent_model_host="127.0.0.1",
    )

    async def no_gpus() -> list[GpuOut]:
        return []

    async def docker_info() -> dict:
        return {"ServerVersion": "27.0", "DockerRootDir": str(root), "Runtimes": {"runc": {}}, "NCPU": 4}

    agent = NodeAgent(settings, server=live, token=created["token"], stats=no_gpus, docker_info=docker_info)
    task = asyncio.create_task(agent.run())
    try:
        await asyncio.wait_for(agent.connected.wait(), 15)
        node_id = created["node"]["id"]

        async def remote():
            machines = (await bob.get("/api/machines")).json()
            found = next((x for x in machines if x["id"] == node_id), None)
            return found if found and found["memory_used_mb"] is not None else None

        m = await _until(remote, what="CPU-only node with metrics")
        assert m["kind"] == "cpu" and m["gpus"] == [] and m["lanes"] == [] and m["online"]
        assert m["cpu_logical_cores"] and m["memory_total_mb"] and m["gpu_driver"] is None
        assert m["docker"] == {"available": True, "version": "27.0", "runtimes": ["runc"], "gpu_runtime": False}
        assert {d["label"] for d in m["disks"]} == {"models", "docker"}
        cap = m["capacity"]
        assert cap["gpus"] == 0 and cap["lanes"] == 0 and cap["cpu_cores_lanes"] == 0
        assert cap["cpu_cores_free"] == m["cpu_logical_cores"] and cap["memory_free_mb"] is not None
        assert cap["cpu_envs"] is False  # CPU-only environments start on the local node for now
        local = next(x for x in (await bob.get("/api/machines")).json() if x["is_local"])
        assert local["capacity"]["cpu_envs"] is True

        # Pick for me, CPU work in general: both machines are options; for a CPU-only environment only local fits
        data = (await bob.post("/api/machines/recommend", json={"gpu": False, "cores": 1})).json()
        assert {o["node_id"] for o in data["options"] if o["fits"]} == {local["id"], node_id}
        data = (await bob.post("/api/machines/recommend", json={"gpu": False, "purpose": "cpu_env"})).json()
        assert data["best"]["node_id"] == local["id"]
        # a GPU request: the CPU-only machine has no GPU
        data = (await bob.post("/api/machines/recommend", json={"gpu": True})).json()
        assert "no_gpu" in next(o for o in data["options"] if o["node_id"] == node_id)["reasons"]

        # Starting without a GPU on a chosen machine
        await bob.post("/api/ssh-keys", json={"public_key": ed25519_key()})
        torch = next(t for t in (await bob.get("/api/devenv/templates")).json() if t["builtin_key"] == "pytorch")
        r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"], "node_id": node_id})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "devenv_cpu_node_unsupported"
        r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"], "node_id": 999})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "node_invalid"
        r = await bob.post("/api/devenv/cpu/start", json={"template_id": torch["id"], "node_id": local["id"]})
        assert r.status_code == 201, r.text
        info = (await bob.get("/api/devenv/cpu")).json()
        assert [s["node_id"] for s in info["sessions"]] == [None]  # the local node is stored as NULL
        after = next(x for x in (await bob.get("/api/machines")).json() if x["is_local"])
        assert after["capacity"]["cpu_envs_running"] == 1
        assert after["capacity"]["cpu_cores_envs"] == app.state.settings.devenv_cpu_cores
    finally:
        await agent.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
