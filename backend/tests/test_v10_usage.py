"""Compute usage: GPU sampling, attribution by kind, hourly roll-up, the usage API, CSV and metrics."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, update

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import DevTemplate, Lane, Reservation, UsageSample
from slotserve.runtime.manager import RuntimeState
from slotserve.schemas import GpuOut
from slotserve.services import maintenance, usage_sampler
from slotserve.services.usage_report import Window
from slotserve.services.usage_sampler import Occupant, attribute


class FakeGpus:
    """Two GPUs whose readings tests set directly."""

    def __init__(self):
        self.util = {0: 0, 1: 0}
        self.mem = {0: 1000, 1: 500}
        self.power = {0: 100.0, 1: 50.0}

    async def __call__(self) -> list[GpuOut]:
        return [
            GpuOut(
                index=i,
                name="Fake GPU",
                memory_used_mb=self.mem[i],
                memory_total_mb=24000,
                utilization_pct=self.util[i],
                temperature_c=40,
                power_w=self.power[i],
            )
            for i in (0, 1)
        ]


@pytest.fixture
async def sampler(app):
    """The app's sampler with fake GPUs, no rows yet, and its loop and the retention job stopped."""
    stopped = [t for t in asyncio.all_tasks() if t.get_name() in ("usage-sampler", "housekeeper")]
    for task in stopped:
        task.cancel()
    await asyncio.gather(*stopped, return_exceptions=True)
    fake = FakeGpus()
    s = app.state.usage_sampler
    s._stats = fake
    s.fake = fake
    async with SessionLocal() as db:
        await db.execute(delete(UsageSample))
        await db.execute(update(Lane).values(gpu_indices="0,1"))
        await db.commit()
    await app.state.lanes.sync()
    return s


def _minute(ago_minutes: int):
    return (utcnow() - timedelta(minutes=ago_minutes)).replace(second=0, microsecond=0)


async def _book(user_id: int, start, end, **kw) -> int:
    async with SessionLocal() as db:
        lane_id = (await db.execute(select(Lane.id).limit(1))).scalar_one()
        r = Reservation(user_id=user_id, lane_id=lane_id, start_at=start, end_at=end, status="booked", **kw)
        db.add(r)
        await db.commit()
        return r.id


async def _rows() -> list[UsageSample]:
    async with SessionLocal() as db:
        return list(
            (await db.execute(select(UsageSample).order_by(UsageSample.start, UsageSample.gpu_index))).scalars()
        )


async def _uid(client) -> int:
    return (await client.get("/api/auth/me")).json()["id"]


async def test_sampler_records_each_gpu_once_per_minute(app, make_client, sampler):
    await make_client("root")
    bob = await make_client("bob")
    bob_id = await _uid(bob)
    start = _minute(120)
    rid = await _book(bob_id, start, start + timedelta(hours=1))
    sampler.fake.util = {0: 80, 1: 5}

    assert await sampler.sample(start + timedelta(seconds=10)) == 2
    assert await sampler.sample(start + timedelta(seconds=40)) == 0  # same minute: nothing new
    rows = await _rows()
    assert [(r.gpu_index, r.kind, r.user_id, r.reservation_id) for r in rows] == [
        (0, "gpu", bob_id, rid),
        (1, "gpu", bob_id, rid),
    ]
    g0, g1 = rows
    assert (g0.seconds, g0.metered_seconds, g0.busy_seconds, g1.busy_seconds) == (60, 60, 60, 0)  # busy >= 10 %
    assert g0.util_seconds == 80 * 60 and g0.energy_wh == pytest.approx(100 / 60)
    assert (g0.mem_peak_mb, g0.mem_total_mb) == (1000, 24000)

    # A short stall is caught up; a long gap (restart, clock jump) is not guessed.
    await sampler.sample(start + timedelta(minutes=3))
    await sampler.sample(start + timedelta(minutes=30))
    assert [r.seconds for r in await _rows() if r.gpu_index == 0] == [60, 180, 60]
    # The clock went back to a minute that was already recorded.
    assert await sampler.sample(start + timedelta(minutes=3, seconds=5)) == 0


async def test_lane_without_gpu_metrics_still_records_occupancy(app, make_client, sampler):

    async def none():
        return []

    sampler._stats = none
    await make_client("root")
    assert await sampler.sample(_minute(5)) == 1
    (row,) = await _rows()
    assert (row.gpu_index, row.kind, row.metered_seconds, row.mem_peak_mb) == (-1, "free", 0, None)


async def test_attribution_by_kind(app, make_client, sampler):
    await make_client("root")
    bob_id = await _uid(await make_client("bob"))
    async with SessionLocal() as db:
        template_id = (await db.execute(select(DevTemplate.id).limit(1))).scalar_one()
        lane_id = (await db.execute(select(Lane.id).limit(1))).scalar_one()
    stopped = SimpleNamespace(model_id=None, model_name=None, state=RuntimeState.STOPPED)
    serving = SimpleNamespace(model_id=1, model_name="chat", state=RuntimeState.READY)
    ctx = SimpleNamespace(id=lane_id, runtime=stopped)

    t = _minute(600)
    await _book(bob_id, t, t + timedelta(minutes=10), model_id=None)
    await _book(bob_id, t + timedelta(minutes=10), t + timedelta(minutes=20), devenv_template_id=template_id)
    async with SessionLocal() as db:
        assert (await attribute(db, ctx, t + timedelta(minutes=1))).kind == "gpu"
        occ = await attribute(db, ctx, t + timedelta(minutes=11))
        assert (occ.kind, occ.user_id) == ("devenv", bob_id)
        assert (await attribute(db, ctx, t + timedelta(minutes=30))) == Occupant("free")
        ctx.runtime = serving
        assert (await attribute(db, ctx, t + timedelta(minutes=30))) == Occupant("idle_model", model_name="chat")

        async def shared(db, lane, now):
            return Occupant("idle_devenv")

        assert (await attribute(db, ctx, t + timedelta(minutes=30), [shared])).kind == "idle_devenv"
        # A booking always wins over idle use.
        assert (await attribute(db, ctx, t + timedelta(minutes=1), [shared])).kind == "gpu"


async def test_roll_up_keeps_totals(app, make_client, sampler):
    admin = await make_client("root")
    old = (utcnow() - timedelta(days=40)).replace(minute=0, second=0, microsecond=0)
    sampler.fake.util = {0: 50, 1: 0}
    for m in range(0, 90, 1):  # 90 minutes over two hours
        await sampler.sample(old + timedelta(minutes=m))
    before = (await admin.get("/api/usage", params={"days": 90})).json()
    async with SessionLocal() as db:
        removed = await usage_sampler.roll_up(db, 30, utcnow())
        await db.commit()
    assert removed == 180
    rows = await _rows()
    assert all(r.hourly for r in rows) and len(rows) == 4  # 2 hours x 2 GPUs
    assert [r.seconds for r in rows if r.gpu_index == 0] == [3600, 1800]
    assert [r.busy_seconds for r in rows if r.gpu_index == 0] == [3600, 1800]
    after = (await admin.get("/api/usage", params={"days": 90})).json()
    assert after["totals"] == before["totals"]
    assert after["kinds"] == before["kinds"]
    assert after["totals"]["sampled_gpu_hours"] == 3.0
    assert after["totals"]["used_gpu_hours"] == 1.5

    # retention runs the roll-up with the configured age; recent rows stay as minutes
    await sampler.sample(_minute(2))
    async with SessionLocal() as db:
        stats = await maintenance.apply_retention(db, app.state.settings, utcnow())
    assert stats["usage_samples"] == 0
    assert sum(not r.hourly for r in await _rows()) == 2


async def test_usage_numbers_csv_and_scope(app, make_client, sampler):
    admin = await make_client("root")
    bob = await make_client("bob")
    bob_id = await _uid(bob)
    start = _minute(180)
    await _book(bob_id, start, start + timedelta(hours=1))
    # 4 sampled minutes during the booking: GPU 0 busy at 60 %, GPU 1 idle
    sampler.fake.util = {0: 60, 1: 0}
    sampler.fake.mem = {0: 9000, 1: 100}
    for m in range(4):
        await sampler.sample(start + timedelta(minutes=m))
    # and 2 minutes after it, nobody holding the lane
    for m in range(70, 72):
        await sampler.sample(start + timedelta(minutes=m))

    data = (await admin.get("/api/usage", params={"days": 7})).json()
    bob_row = next(u for u in data["users"] if u["username"] == "bob")
    assert bob_row["reserved_hours"] == 1.0
    assert bob_row["booked_gpu_hours"] == 2.0  # one hour x two GPUs
    assert bob_row["bookings"] == 1
    assert bob_row["used_gpu_hours"] == round(4 * 60 / 3600, 2)
    assert bob_row["avg_utilization"] == 30.0  # 60 % on one GPU, 0 % on the other
    assert bob_row["peak_memory_mb"] == 9000
    assert bob_row["energy_wh"] == pytest.approx(round(4 * 150 / 60, 1))
    totals = data["totals"]
    assert totals["booked_gpu_hours"] == 2.0
    assert totals["sampled_gpu_hours"] == round(12 * 60 / 3600, 2)
    assert totals["avg_utilization"] == 30.0  # only time inside bookings
    assert data["metered"] is True and data["busy_percent"] == 10
    kinds = {k["kind"]: k for k in data["kinds"]}
    assert set(kinds) == {"gpu", "free"}
    assert kinds["free"]["gpu_hours"] == round(4 * 60 / 3600, 2)
    (lane,) = data["lanes"]
    assert lane["gpus"] == 2 and lane["booked_gpu_hours"] == 2.0
    assert set(lane["occupancy"]) == {"gpu", "free"}
    assert [(g["gpu_index"], g["peak_memory_mb"]) for g in data["gpus"]] == [(0, 9000), (1, 100)]

    assert data["bucket_hours"] == 6 and len(data["buckets"]) == 28
    (person,) = data["hours_by_person"]
    assert person["name"] == "bob" and sum(person["values"]) == pytest.approx(2.0)
    by_kind = {s["key"]: sum(s["values"]) for s in data["hours_by_kind"]}
    assert by_kind["gpu"] == pytest.approx(8 * 60 / 3600, abs=0.01)
    (util,) = data["utilization_by_lane"]
    assert max(v for v in util["values"] if v is not None) > 0

    mine = (await admin.get("/api/usage", params={"days": 7, "scope": "mine"})).json()
    assert mine["scope"] == "mine" and mine["users"] == [] and mine["totals"]["booked_gpu_hours"] == 0
    bob_mine = (await bob.get("/api/usage", params={"days": 30, "scope": "mine"})).json()
    assert [u["username"] for u in bob_mine["users"]] == ["bob"]
    assert bob_mine["bucket_hours"] == 24 and len(bob_mine["buckets"]) == 30

    csv = (await bob.get("/api/usage/export", params={"days": 7, "kind": "people"})).text
    header, *lines = csv.lstrip("﻿").strip().splitlines()
    assert header.startswith("user,bookings,booked_hours,booked_gpu_hours,used_gpu_hours")
    assert any(line.startswith("bob,1,1.0,2.0,0.07,30.0,9000,") for line in lines)
    csv = (await admin.get("/api/usage/export", params={"days": 7, "kind": "compute"})).text
    header, *lines = csv.lstrip("﻿").strip().splitlines()
    assert header == (
        "date,node,lane,gpu,kind,user,gpu_hours,used_gpu_hours,avg_utilization_pct,peak_memory_mb,energy_wh"
    )
    gpu0 = next(line for line in lines if ",0,gpu,bob," in line)
    assert gpu0.endswith(",0.067,0.067,60.0,9000,6.7")
    assert any(",1,free,," in line for line in lines)
    assert all(line.split(",")[1] == "local" for line in lines)  # the machine each row ran on
    csv = (await admin.get("/api/usage/export", params={"days": 7, "kind": "nodes"})).text
    header, *lines = csv.lstrip("﻿").strip().splitlines()
    assert header.startswith("node,lanes,gpus,booked_gpu_hours,sampled_gpu_hours,used_gpu_hours")
    assert len(lines) == 1 and lines[0].startswith("local,1,")
    assert (await admin.get("/api/usage/export", params={"kind": "nope"})).status_code == 422


async def test_metrics_and_settings(app, make_client, sampler):
    admin = await make_client("root")
    bob_id = await _uid(await make_client("bob"))
    r = await admin.put("/api/admin/settings", json={"usage_busy_percent": 0})
    assert r.status_code == 400
    r = await admin.put(
        "/api/admin/settings", json={"metrics_token": "m", "usage_busy_percent": 50, "usage_sample_days": 7}
    )
    assert r.status_code == 200, r.text
    start = _minute(90)
    await _book(bob_id, start, start + timedelta(hours=1))
    sampler.fake.util = {0: 60, 1: 40}
    await sampler.sample(start)
    rows = await _rows()
    assert [r.busy_seconds for r in rows] == [60, 0]  # threshold now 50 %

    text = (await admin.get("/metrics", headers={"Authorization": "Bearer m"})).text
    lane = (await admin.get("/api/lanes")).json()[0]["name"]
    assert f'slotserve_gpu_utilization_ratio{{node="local",gpu="0",name="Fake GPU",lane="{lane}"}} 0.6' in text
    assert f'slotserve_booked_gpu_hours_total{{user="bob",lane="{lane}",kind="gpu"}} 2.0' in text
    assert f'slotserve_gpu_seconds_total{{node="local",lane="{lane}",gpu="0",kind="gpu"}} 60' in text
    assert f'slotserve_gpu_busy_seconds_total{{node="local",lane="{lane}",gpu="1",kind="gpu"}} 0' in text
    assert f'slotserve_gpu_energy_joules_total{{node="local",lane="{lane}",gpu="0"}} 6000.0' in text
    assert 'slotserve_user_gpu_busy_seconds_total{user="bob",kind="gpu"} 60' in text
    assert f'slotserve_lane_occupant{{node="local",lane="{lane}",kind=' in text
    async with SessionLocal() as db:
        assert (await db.execute(select(func.count(UsageSample.id)))).scalar_one() == 2


async def test_cpu_rows_count_as_core_hours_not_gpu_time(app, make_client, sampler):
    bob = await make_client("bob")
    bob_id = await _uid(bob)
    async with SessionLocal() as db:
        db.add(
            UsageSample(
                start=_minute(30),
                seconds=3600,
                resource="cpu",
                kind="cpu_devenv",
                user_id=bob_id,
                cpu_core_seconds=4 * 3600.0,
                mem_peak_mb=2048,
            )
        )
        await db.commit()
    data = (await bob.get("/api/usage", params={"days": 7, "scope": "mine"})).json()
    assert data["totals"]["cpu_core_hours"] == 4.0
    assert data["users"][0]["cpu_core_hours"] == 4.0
    assert data["totals"]["sampled_gpu_hours"] == 0 and data["kinds"] == []


def test_window_buckets():
    from datetime import UTC, datetime

    now = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)
    w = Window(7, 60, now)  # UTC+1
    assert w.bucket_hours == 6 and w.count == 28
    assert w.labels()[-1] == "2026-03-10T12:00"  # local 14:30 falls in the 12:00 bucket
    assert w.since == datetime(2026, 3, 3, 17, 0, tzinfo=UTC)  # local 18:00, 27 buckets earlier
    spread = list(w.spread(now - timedelta(hours=3), now))
    assert spread == [(26, 1800.0), (27, 9000.0)]
    w = Window(30, 0, now)
    assert w.bucket_hours == 24 and w.labels()[0] == "2026-02-09T00:00"
