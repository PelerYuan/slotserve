"""Personal computers: owners, sharing hours, pause with a grace period, Wake-on-LAN, contribution statistics and
the one-line node installers (services/sharing.py, api/sharing.py, cluster/install.py)."""

import asyncio
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_cluster import _until, live, start_agent  # noqa: F401 - fixtures

from slotserve.cluster import system
from slotserve.db import SessionLocal, utcnow
from slotserve.orm import Node, Reservation, UsageSample
from slotserve.runtime.scheduler import compute_target
from slotserve.schemas import GpuOut
from slotserve.services import sharing

UTC_TZ = ZoneInfo("UTC")
NIGHTS_AND_WEEKENDS = [
    {"days": [0, 1, 2, 3, 4], "start": "23:00", "end": "09:00"},
    {"days": [5, 6], "start": "00:00", "end": "24:00"},
]


def _at(text: str, tz: ZoneInfo = UTC_TZ) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=tz).astimezone(UTC)


# ---- the weekly schedule ------------------------------------------------------------------------------------


def test_schedule_validation():
    assert sharing.validate_schedule(None) is None
    assert sharing.validate_schedule([]) == []
    ok = sharing.validate_schedule([{"days": [4, 0, 0], "start": "23:00", "end": "09:00"}])
    assert ok == [{"days": [0, 4], "start": "23:00", "end": "09:00"}]
    for bad in (
        "always",
        [{"days": [], "start": "00:00", "end": "01:00"}],
        [{"days": [7], "start": "00:00", "end": "01:00"}],
        [{"days": [True], "start": "00:00", "end": "01:00"}],
        [{"days": [1], "start": "24:00", "end": "01:00"}],
        [{"days": [1], "start": "8:00", "end": "09:00"}],
        [{"days": [1], "start": "10:00", "end": "10:00"}],
        [{"days": [1], "start": "00:00", "end": "25:00"}],
    ):
        with pytest.raises(ValueError):
            sharing.validate_schedule(bad)
    # A corrupt stored schedule is ignored (always shared) rather than locking the machine.
    assert sharing.load_schedule("{not json") is None


def test_overnight_windows_and_weekends():
    s = NIGHTS_AND_WEEKENDS
    shared = lambda text: sharing.in_schedule(s, UTC_TZ, _at(text))  # noqa: E731
    # 2026-10-19 is a Monday.
    assert shared("2026-10-19T23:30")  # Monday night
    assert shared("2026-10-20T08:59")  # ... into Tuesday morning
    assert not shared("2026-10-20T09:00")
    assert not shared("2026-10-20T12:00")
    assert not shared("2026-10-19T05:00")  # Sunday's whole day ended at Monday 00:00
    assert shared("2026-10-24T12:00")  # Saturday
    assert shared("2026-10-23T23:30")  # Friday night runs into Saturday
    # Friday 23:00 to Monday 00:00 is one stretch (Sunday's window ends at midnight, Monday's starts at 23:00).
    windows = sharing.shared_windows(s, UTC_TZ, _at("2026-10-23T00:00"), _at("2026-10-27T00:00"))
    assert windows == [
        (_at("2026-10-23T00:00"), _at("2026-10-23T09:00")),
        (_at("2026-10-23T23:00"), _at("2026-10-26T00:00")),
        (_at("2026-10-26T23:00"), _at("2026-10-27T00:00")),
    ]
    assert sharing.uncovered(s, UTC_TZ, _at("2026-10-20T08:00"), _at("2026-10-20T10:00")) == [
        (_at("2026-10-20T09:00"), _at("2026-10-20T10:00"))
    ]
    assert sharing.next_change(s, UTC_TZ, _at("2026-10-20T08:00")) == _at("2026-10-20T09:00")
    assert sharing.next_change(s, UTC_TZ, _at("2026-10-20T12:00")) == _at("2026-10-20T23:00")
    assert sharing.next_change(None, UTC_TZ, _at("2026-10-20T12:00")) is None
    assert sharing.shared_windows([], UTC_TZ, _at("2026-10-20T00:00"), _at("2026-10-21T00:00")) == []


def test_time_zones_and_daylight_saving():
    shanghai = ZoneInfo("Asia/Shanghai")
    instant = _at("2026-10-19T15:30")  # Monday 23:30 in Shanghai
    assert sharing.in_schedule(NIGHTS_AND_WEEKENDS, shanghai, instant)
    assert not sharing.in_schedule(NIGHTS_AND_WEEKENDS, UTC_TZ, instant)
    # Berlin leaves summer time on Sunday 2026-10-25: that whole day lasts 25 hours.
    berlin = ZoneInfo("Europe/Berlin")
    sunday = [{"days": [6], "start": "00:00", "end": "24:00"}]
    [(s, e)] = sharing.shared_windows(sunday, berlin, _at("2026-10-24T00:00"), _at("2026-10-27T00:00"))
    assert e - s == timedelta(hours=25)
    assert s == _at("2026-10-25T00:00", berlin)


# ---- helpers ------------------------------------------------------------------------------------------------------


def _events(app) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    original = app.state.notifier.emit

    def emit(event, **data):
        seen.append((event, data))
        original(event, **data)

    app.state.notifier.emit = emit
    return seen


async def _setup(make_client, gpus=(0,)):
    """An admin, the machine's owner alice, bob, and alice's (offline) PC with one lane."""
    admin = await make_client("root")
    alice = await make_client("alice")
    bob = await make_client("bob")
    users = {u["username"]: u["id"] for u in (await admin.get("/api/admin/users")).json()}
    created = (await admin.post("/api/admin/nodes", json={"name": "pc-alice"})).json()
    node_id = created["node"]["id"]
    r = await admin.patch(f"/api/admin/nodes/{node_id}", json={"owner_id": users["alice"]})
    assert r.status_code == 200, r.text
    assert r.json()["owner_name"] == "alice" and r.json()["shared_now"] is True
    lane = (await admin.post("/api/admin/lanes", json={"name": "Alice", "gpus": list(gpus), "node_id": node_id})).json()
    return admin, alice, bob, users, node_id, lane, created["token"]


def _window_around(now: datetime) -> tuple[list[dict], datetime]:
    """A daily 4-hour sharing window that starts 2-3 hours from now; returns it and its next start."""
    hour = (now.hour + 2) % 24
    schedule = [{"days": list(range(7)), "start": f"{hour:02d}:00", "end": f"{(hour + 4) % 24:02d}:00"}]
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    return schedule, start


async def _book(client, lane_id: int, start: datetime, end: datetime):
    return await client.post(
        "/api/reservations",
        json={"lane_id": lane_id, "model_id": None, "start_at": start.isoformat(), "end_at": end.isoformat()},
    )


# ---- owners, sharing hours and bookings --------------------------------------------------------------------------


async def test_sharing_hours_refuse_bookings_and_warn_on_conflicts(app, make_client):
    app.state.settings.timezone = "UTC"
    admin, alice, bob, users, node_id, lane, _token = await _setup(make_client)
    now = utcnow()

    # Only the owner (and admins) control the machine.
    assert [n["name"] for n in (await alice.get("/api/nodes/mine")).json()] == ["pc-alice"]
    assert (await bob.get("/api/nodes/mine")).json() == []
    assert (await bob.put(f"/api/nodes/{node_id}/sharing", json={"schedule": []})).status_code == 403
    assert (await bob.get(f"/api/nodes/{node_id}/running")).status_code == 403

    # Bob books before any schedule exists (always shared).
    early = await _book(bob, lane["id"], now + timedelta(minutes=10), now + timedelta(minutes=25))
    assert early.status_code == 201, early.text

    schedule, start = _window_around(now)
    events = _events(app)
    r = await alice.put(f"/api/nodes/{node_id}/sharing", json={"schedule": schedule})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node"]["sharing_schedule"] == schedule and body["node"]["shared_now"] is False
    assert body["node"]["sharing_next_change"] is not None
    # Bob's booking now lies outside the sharing hours: it stays, both are told.
    assert [c["id"] for c in body["conflicts"]] == [early.json()[0]["id"]]
    await asyncio.sleep(0)
    kinds = {(e, d.get("user_id")) for e, d in events}
    assert ("sharing_conflict", users["bob"]) in kinds and ("sharing_conflict_owner", users["alice"]) in kinds

    # New bookings by others must lie inside the sharing hours ...
    outside = await _book(bob, lane["id"], start - timedelta(hours=1), start + timedelta(minutes=30))
    assert outside.status_code == 400 and outside.json()["detail"]["code"] == "outside_sharing_hours"
    assert outside.json()["detail"]["params"]["node"] == "pc-alice"
    inside = await _book(bob, lane["id"], start + timedelta(minutes=30), start + timedelta(hours=2))
    assert inside.status_code == 201, inside.text
    # ... an extension too; the owner books her own machine whenever she likes.
    rid = inside.json()[0]["id"]
    longer = await bob.patch(f"/api/reservations/{rid}", json={"end_at": (start + timedelta(hours=5)).isoformat()})
    assert longer.status_code == 400 and longer.json()["detail"]["code"] == "outside_sharing_hours"
    own = await _book(alice, lane["id"], start - timedelta(minutes=30), start - timedelta(minutes=5))
    assert own.status_code == 201, own.text

    # The calendar shows the unavailable hours of each lane.
    r = await bob.get(
        "/api/lanes/unavailable",
        params={"start": now.isoformat(), "end": (now + timedelta(days=1)).isoformat()},
    )
    lanes = {x["lane_id"]: x for x in r.json()}
    mine = lanes[lane["id"]]
    assert mine["owner_id"] == users["alice"] and mine["blocks"]
    assert all(b["reason"] == "unshared" for b in mine["blocks"])
    total = sum(
        (datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds() for b in mine["blocks"]
    )
    assert total == pytest.approx(20 * 3600, abs=2)  # 24 hours minus the 4 shared ones
    assert all(not x["blocks"] for lane_id, x in lanes.items() if lane_id != lane["id"])

    # The admin node list carries the owner and sharing fields (also for the Machines page).
    nodes = {n["name"]: n for n in (await admin.get("/api/admin/nodes")).json()}
    assert nodes["pc-alice"]["owner_name"] == "alice" and nodes["pc-alice"]["sharing_schedule"] == schedule
    assert nodes["pc-alice"]["shared_now"] is False

    # Outside the sharing hours the lane runs no default model / shared environment, only bookings.
    async with SessionLocal() as db:
        idle = await compute_target(db, app.state.settings, start + timedelta(hours=5), None, lane_id=lane["id"])
        booked = await compute_target(db, app.state.settings, start - timedelta(minutes=15), None, lane_id=lane["id"])
        free = await compute_target(db, app.state.settings, start + timedelta(minutes=10), None, lane_id=lane["id"])
    assert idle.reason == "unshared"
    assert booked.reason == "exclusive"  # alice's own booking
    assert free.reason == "idle"

    # Back to always shared.
    r = await alice.put(f"/api/nodes/{node_id}/sharing", json={"schedule": None})
    assert r.json()["node"]["sharing_schedule"] is None and r.json()["conflicts"] == []
    bad = await alice.put(
        f"/api/nodes/{node_id}/sharing", json={"schedule": [{"days": [1], "start": "10:00", "end": "10:00"}]}
    )
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_schedule"


async def test_pause_warns_then_stops_after_the_grace_period(app, make_client):
    app.state.settings.sharing_grace_minutes = 10
    _admin, alice, bob, _users, node_id, lane, _token = await _setup(make_client)
    now = utcnow()
    running = await _book(bob, lane["id"], now, now + timedelta(hours=2))
    assert running.status_code == 201, running.text
    later = await _book(bob, lane["id"], now + timedelta(hours=3), now + timedelta(hours=4))
    rid, later_id = running.json()[0]["id"], later.json()[0]["id"]

    events = _events(app)
    r = await alice.post(f"/api/nodes/{node_id}/pause", json={"hours": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["affected"] == 2 and body["node"]["sharing_paused"] and not body["node"]["shared_now"]
    paused_at = datetime.fromisoformat(body["node"]["sharing_stops_at"]) - timedelta(minutes=10)
    await asyncio.sleep(0)
    told = {(e, d.get("reservation_id")) for e, d in events}
    assert ("sharing_paused_active", rid) in told and ("sharing_paused_upcoming_until", later_id) in told

    # While paused nobody else can book the machine.
    r = await _book(bob, lane["id"], now + timedelta(hours=1), now + timedelta(hours=2, minutes=30))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "node_sharing_paused"

    # During the grace period everything keeps running ...
    await app.state.sharing.tick(paused_at + timedelta(minutes=5))
    async with SessionLocal() as db:
        assert (await db.get(Reservation, rid)).released_at is None
        target = await compute_target(
            db, app.state.settings, paused_at + timedelta(minutes=5), None, lane_id=lane["id"]
        )
        assert target.reason == "exclusive"
    # ... afterwards the booking is ended (a kept dev environment would be paused like at any booking's end).
    stop_at = paused_at + timedelta(minutes=11)
    await app.state.sharing.tick(stop_at)
    async with SessionLocal() as db:
        r1 = await db.get(Reservation, rid)
        assert r1.released_at is not None and r1.end_at == stop_at and r1.status == "booked"
        target = await compute_target(db, app.state.settings, stop_at + timedelta(seconds=1), None, lane_id=lane["id"])
        assert target.reason == "unshared"
    await asyncio.sleep(0)
    assert ("booking_stopped_sharing", rid) in {(e, d.get("reservation_id")) for e, d in events}

    # The pause ends by itself.
    await app.state.sharing.tick(paused_at + timedelta(hours=5, seconds=1))
    async with SessionLocal() as db:
        node = await db.get(Node, node_id)
        assert node.sharing_paused_at is None
    # ... or when the owner resumes.
    await alice.post(f"/api/nodes/{node_id}/pause", json={})
    assert (await alice.get(f"/api/nodes/{node_id}/sharing")).json()["sharing_paused_until"] is None
    r = await alice.post(f"/api/nodes/{node_id}/resume")
    assert r.status_code == 200 and not r.json()["sharing_paused"]


async def test_owner_sees_and_stops_what_runs_on_the_machine(app, make_client):
    admin, alice, bob, _users, node_id, lane, _token = await _setup(make_client)
    now = utcnow()
    rid = (await _book(bob, lane["id"], now, now + timedelta(hours=1))).json()[0]["id"]
    upcoming = (await _book(bob, lane["id"], now + timedelta(hours=2), now + timedelta(hours=3))).json()[0]["id"]

    r = await alice.get(f"/api/nodes/{node_id}/running")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [la["name"] for la in body["lanes"]] == ["Alice"]
    booked = {b["id"]: b for b in body["bookings"]}
    assert booked[rid]["active"] and booked[rid]["username"] == "bob" and booked[rid]["kind"] == "exclusive"
    assert not booked[upcoming]["active"]

    events = _events(app)
    r = await alice.post(f"/api/nodes/{node_id}/stop", json={"reservation_id": rid})
    assert r.status_code == 200, r.text
    r = await alice.post(f"/api/nodes/{node_id}/stop", json={"reservation_id": upcoming})
    assert {b["id"] for b in r.json()["bookings"]} == set()
    async with SessionLocal() as db:
        assert (await db.get(Reservation, rid)).released_at is not None
        assert (await db.get(Reservation, upcoming)).status == "cancelled"
    await asyncio.sleep(0)
    assert [d["reservation_id"] for e, d in events if e == "booking_stopped_owner"] == [rid, upcoming]
    # Stopping what a lane runs while nobody books it: an unload until the next booking.
    r = await alice.post(f"/api/nodes/{node_id}/stop", json={"lane_id": lane["id"]})
    assert r.status_code == 200
    assert app.state.lanes.contexts[lane["id"]].scheduler.manual.model_id is None
    # Not someone else's lane.
    other = (await admin.get("/api/lanes")).json()[0]["id"]
    assert (await alice.post(f"/api/nodes/{node_id}/stop", json={"lane_id": other})).status_code == 404


# ---- Wake-on-LAN ------------------------------------------------------------------------------------------------


def test_magic_packet_goes_out():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(5)
        port = listener.getsockname()[1]
        assert system.send_magic(["AA-BB-CC-DD-EE-FF"], ["127.0.0.1"], port) == 1
        data, _ = listener.recvfrom(1024)
    assert len(data) == 102 and data[:6] == b"\xff" * 6 and data[6:12] == bytes.fromhex("aabbccddeeff")
    assert system.parse_macs("aa:bb:cc:dd:ee:ff, AABBCCDDEEFF;11-22-33-44-55-66 junk") == [
        "aa:bb:cc:dd:ee:ff",
        "11:22:33:44:55:66",
    ]
    assert not system.physical_mac("02:42:ac:11:00:02") and not system.physical_mac("00:15:5d:01:02:03")
    assert system.physical_mac("bc:38:98:98:0f:f8")


def test_platform_detection():
    wsl = system.detect_platform("windows", kernel="5.15.167.4-microsoft-standard-WSL2", os_name="linux")
    assert wsl["host_os"] == "windows" and wsl["runtime"] == "wsl2" and wsl["label"].startswith("Windows · WSL2")
    mac = system.detect_platform("macos", kernel="6.10.14-linuxkit", os_name="linux")
    assert mac["host_os"] == "macos" and mac["label"] == "macOS · Docker Desktop"
    assert system.detect_platform(kernel="6.8.0-45-generic", os_name="linux")["host_os"] == "linux"
    assert system.detect_platform(os_name="win32")["label"] == "Windows"


async def test_wake_on_lan_relayed_through_another_node(app, make_client, start_agent, monkeypatch):  # noqa: F811
    app.state.settings.wol_lead_minutes = 5
    admin, alice, bob, users, node_id, lane, _token = await _setup(make_client)
    sent: list[tuple[list[str], list[str], int]] = []

    def fake_send(macs, broadcasts, port=9):
        sent.append((macs, broadcasts, port))
        return len(macs) * len(broadcasts)

    monkeypatch.setattr(system, "send_magic", fake_send)

    # Nothing to wake it with yet.
    r = await alice.post(f"/api/nodes/{node_id}/wake")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "node_no_mac"
    r = await alice.put(f"/api/nodes/{node_id}/sharing", json={"mac_addresses": "AA-BB-CC-DD-EE-FF", "network": "lab"})
    assert r.status_code == 200 and r.json()["node"]["wol_macs"] == ["aa:bb:cc:dd:ee:ff"]
    assert (await alice.put(f"/api/nodes/{node_id}/sharing", json={"mac_addresses": "nope"})).status_code == 400
    r = await alice.post(f"/api/nodes/{node_id}/wake")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_wake_relay"

    # A second machine in the same network (an in-process agent) relays the magic packet.
    relay = (await admin.post("/api/admin/nodes", json={"name": "relay"})).json()
    await admin.put(f"/api/nodes/{relay['node']['id']}/sharing", json={"network": "lab"})
    await start_agent(relay["token"], name="relay")
    await _until(
        lambda: _online(admin, relay["node"]["id"]),
        what="relay online",
    )
    relay_node = next(n for n in (await admin.get("/api/admin/nodes")).json() if n["name"] == "relay")
    assert relay_node["platform"]  # what kind of machine the agent runs on
    r = await alice.post(f"/api/nodes/{node_id}/wake")
    assert r.status_code == 200, r.text
    assert r.json()["senders"] == ["relay"]
    assert sent[-1] == (["aa:bb:cc:dd:ee:ff"], ["255.255.255.255"], 9)
    # Bob may wake the machine only for a booking of his that starts soon.
    assert (await bob.post(f"/api/nodes/{node_id}/wake")).status_code == 403

    # A booking that starts within the lead time wakes the machine by itself (once) ...
    now = utcnow()
    rid = (await _book(bob, lane["id"], now + timedelta(minutes=3), now + timedelta(hours=1))).json()[0]["id"]
    assert (await bob.post(f"/api/nodes/{node_id}/wake")).status_code == 200
    count = len(sent)
    events = _events(app)
    await app.state.sharing.tick(now)
    await app.state.sharing.tick(now + timedelta(seconds=20))
    assert len(sent) == count + 1
    # ... and if it is still offline when the booking starts, the booker and the owner are told.
    await app.state.sharing.tick(now + timedelta(minutes=4))
    await asyncio.sleep(0)
    told = {(e, d.get("user_id")) for e, d in events if d.get("reservation_id") == rid}
    assert ("booking_node_asleep", users["bob"]) in told and ("booking_node_asleep_owner", users["alice"]) in told
    async with SessionLocal() as db:
        assert (await db.get(Node, node_id)).woken_at is not None


async def _online(admin, node_id: int) -> bool:
    nodes = (await admin.get("/api/admin/nodes")).json()
    return any(n["id"] == node_id and n["status"] == "online" for n in nodes)


# ---- contribution ------------------------------------------------------------------------------------------------


async def test_contribution_numbers(app, make_client, monkeypatch):
    async def eight_gpus():
        return [
            GpuOut(index=i, name="GPU", memory_used_mb=0, memory_total_mb=1, utilization_pct=0, temperature_c=0)
            for i in range(8)
        ]

    monkeypatch.setattr("slotserve.api.lanes.gpu_stats", eight_gpus)
    admin = await make_client("root")
    alice = await make_client("alice")
    await make_client("bob")
    users = {u["username"]: u["id"] for u in (await admin.get("/api/admin/users")).json()}
    local = next(n for n in (await admin.get("/api/admin/nodes")).json() if n["is_local"])
    # Alice's machine is the server's own one here (always online); it has a lane with two GPUs.
    await admin.patch(f"/api/admin/nodes/{local['id']}", json={"owner_id": users["alice"]})
    lane = (await admin.post("/api/admin/lanes", json={"name": "Two", "gpus": [6, 7]})).json()
    primary = (await admin.get("/api/lanes")).json()[0]
    gpus = 2 + len(primary["gpus"])

    service = app.state.sharing
    service._last_sample = None
    t0 = utcnow().replace(microsecond=0)
    await service.tick(t0)  # the first tick only starts the clock
    await service.tick(t0 + timedelta(seconds=300))
    await alice.post(f"/api/nodes/{local['id']}/pause", json={})
    await service.tick(t0 + timedelta(seconds=600))  # online, but not shared
    await alice.post(f"/api/nodes/{local['id']}/resume")
    await service.tick(t0 + timedelta(seconds=630))  # too soon: no sample

    async with SessionLocal() as db:
        db.add_all(
            [
                UsageSample(start=t0, seconds=3600, lane_id=lane["id"], gpu_index=6, kind="gpu", user_id=users["bob"]),
                UsageSample(
                    start=t0, seconds=1800, lane_id=lane["id"], gpu_index=7, kind="model", user_id=users["alice"]
                ),
                UsageSample(start=t0, seconds=900, lane_id=lane["id"], gpu_index=7, kind="idle_model", user_id=None),
            ]
        )
        await db.commit()

    r = await admin.get("/api/usage/contribution", params={"days": 7})
    assert r.status_code == 200, r.text
    rows = {row["username"]: row for row in r.json()["rows"]}
    a, b = rows["alice"], rows["bob"]
    assert a["nodes"] == [local["name"]]
    assert a["online_hours"] == round(600 / 3600, 2) and a["shared_hours"] == round(300 / 3600, 2)
    assert a["gpu_hours_shared"] == round(300 * gpus / 3600, 2)
    assert a["gpu_hours_used_by_others"] == 1.0 and a["gpu_hours_consumed"] == 0.5
    assert b["gpu_hours_consumed"] == 1.0 and b["shared_hours"] == 0 and b["nodes"] == []

    node = (await alice.get(f"/api/nodes/{local['id']}/contribution", params={"days": 7})).json()
    assert node["gpu_hours_used_by_others"] == 1.0 and node["gpu_hours_used_by_owner"] == 0.5
    assert node["online_hours"] == a["online_hours"]

    csv = await admin.get("/api/usage/contribution/export", params={"days": 7})
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    lines = csv.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("user,machines,online_hours") and any(line.startswith("alice,") for line in lines)


# ---- one-line installers ------------------------------------------------------------------------------------------


async def test_install_script_endpoints(app, make_client):
    admin = await make_client("root")
    anonymous = await make_client()
    created = (await admin.post("/api/admin/nodes", json={"name": "pc"})).json()
    token = created["token"]
    assert f"/install/node.sh?token={token}" in created["install_linux"] and "| bash" in created["install_linux"]
    assert f"/install/node.ps1?token={token}" in created["install_windows"] and "| iex" in created["install_windows"]

    r = await anonymous.get("/install/node.sh", params={"token": token})
    assert r.status_code == 200, r.text
    script = r.text
    assert token in script and "http://test" in script and "__SLOTSERVE_" not in script
    assert "\r" not in script and script.startswith("#!/usr/bin/env bash")
    assert "--restart unless-stopped" in script and "nvidia" in script.lower()
    bash = shutil.which("bash")
    if bash is not None:
        check = await asyncio.to_thread(
            subprocess.run, [bash, "-n"], input=script.encode(), capture_output=True, timeout=30
        )
        assert check.returncode == 0, check.stderr

    r = await anonymous.get("/install/node.ps1", params={"token": token})
    assert r.status_code == 200
    assert token in r.text and "\r\n" in r.text and "__SLOTSERVE_" not in r.text
    assert "WSL" in r.text and "--gpus" in r.text

    # Unknown or revoked tokens are refused; without a token the script asks for one.
    assert (await anonymous.get("/install/node.sh", params={"token": "ssn-" + "x" * 40})).status_code == 404
    assert (await anonymous.get("/install/node.sh", params={"token": "bad token"})).status_code == 404
    plain = await anonymous.get("/install/node.sh")
    assert plain.status_code == 200 and 'TOKEN="${SLOTSERVE_AGENT_TOKEN:-}"' in plain.text
    await admin.post(f"/api/admin/nodes/{created['node']['id']}/revoke")
    assert (await anonymous.get("/install/node.ps1", params={"token": token})).status_code == 404


async def test_waitlist_respects_sharing_hours(app, make_client):
    app.state.settings.timezone = "UTC"
    _admin, alice, _bob, users, node_id, lane, _token = await _setup(make_client)
    await alice.put(f"/api/nodes/{node_id}/sharing", json={"schedule": []})  # never shared
    now = utcnow()
    from slotserve.orm import WaitlistEntry
    from slotserve.services.housekeeper import process_waitlist

    async with SessionLocal() as db:
        db.add(
            WaitlistEntry(
                user_id=users["bob"],
                lane_id=lane["id"],
                start_at=now + timedelta(hours=1),
                end_at=now + timedelta(hours=2),
                auto_book=True,
            )
        )
        await db.commit()
        booked = await process_waitlist(db, app.state.settings, app.state.notifier, now)
        assert booked is False
        rows = (await db.execute(select(Reservation).where(Reservation.lane_id == lane["id"]))).scalars().all()
        assert rows == []
