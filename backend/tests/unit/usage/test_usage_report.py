"""``usage_report.build``: booked vs. measured GPU-hours, per person, lane, GPU and kind.

F16 accounting rule ("used <= booked + shared"): within one window, the GPU-hours measured as busy must not
exceed the GPU-hours booked plus the GPU-hours used in shared mode (IDLE_KINDS: the lane's default model or shared
environment while nobody books). Today the Usage page compares "used" (busy time of every kind) with "booked"
(calendar only), which is how pm2-2 saw 0.05 GPU-h used next to 0.01 booked. The tests below pin where the
current numbers keep the rule and, marked xfail(strict), where they break it, so a fix flips them to passing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from slotserve.orm import IDLE_KINDS
from slotserve.services.usage_report import Window, build

NOW = datetime(2026, 1, 7, 10, 7, tzinfo=UTC)  # window (1 day, UTC): Jan 6 12:00 .. now
M = timedelta(minutes=1)
H = timedelta(hours=1)
T0 = datetime(2026, 1, 7, 8, 0, tzinfo=UTC)


async def report(db, user_id=None, detected_gpus=1, names=None):
    return await build(db, Window(1, 0, NOW), names or {}, detected_gpus, user_id)


def f16_gap_seconds(rep) -> float:
    """used - (booked + shared), in GPU-seconds; the rule holds when this is <= 0."""
    shared = sum(rep.kinds[k].busy for k in IDLE_KINDS if k in rep.kinds)
    return rep.all.busy - (sum(rep.booked.values()) + shared)


async def minutes(rows, start, n, lane, kind, user=None, gpus=(0,), busy=True):
    for i in range(n):
        for g in gpus:
            await rows.sample(start + i * M, lane, kind, user, gpu=g, busy=60 if busy else 0, util=60 * 90.0)


# ---- aggregation -------------------------------------------------------------------


async def test_booked_gpu_hours_are_calendar_time_times_lane_gpus(db, rows):
    alice, bob = await rows.user("alice"), await rows.user("bob")
    two = await rows.lane("two", gpus="0,1")
    one = await rows.lane("one", gpus="2")
    await rows.booking(alice, T0, T0 + H, two)
    await rows.booking(bob, T0, T0 + 30 * M, one)
    await rows.booking(bob, T0 + H, T0 + 2 * H, one, status="cancelled")
    await rows.booking(bob, NOW + H, NOW + 2 * H, one)  # future: not booked time yet
    await rows.booking(alice, NOW - 10 * M, NOW + H, one)  # running: counts until now
    rep = await report(db)
    assert rep.booked[alice.id] == 2 * 3600 + 600
    assert rep.calendar[alice.id] == 3600 + 600
    assert rep.booked[bob.id] == 1800
    assert (rep.bookings[alice.id], rep.bookings[bob.id]) == (2, 1)
    lanes = {r.lane_name: r for r in rep.lane_rows()}
    assert (lanes["two"].gpus, lanes["two"].booked_gpu_hours, lanes["one"].booked_gpu_hours) == (2, 2.0, 0.67)


async def test_booking_before_the_window_is_clipped(db, rows):
    alice = await rows.user()
    lane = await rows.lane(gpus="0")
    since = Window(1, 0, NOW).since
    await rows.booking(alice, since - 2 * H, since + H, lane)
    rep = await report(db)
    assert rep.booked[alice.id] == 3600
    assert rep.person_series[alice.id][0] == 3600


async def test_samples_roll_up_per_person_lane_gpu_and_kind(db, rows):
    alice = await rows.user()
    lane = await rows.lane(gpus="0,1")
    await rows.booking(alice, T0, T0 + H, lane)
    await minutes(rows, T0, 30, lane, "model", alice, gpus=(0, 1))  # both GPUs busy 30 min
    await minutes(rows, T0 + 30 * M, 30, lane, "model", alice, gpus=(0, 1), busy=False)
    await minutes(rows, T0 + H, 15, lane, "idle_model", gpus=(0,))
    await rows.sample(T0, lane, "devenv", alice, gpu=-1, resource="cpu", metered=0, cpu_core_seconds=240.0)
    rep = await report(db)
    assert rep.people[alice.id].used_hours == 1.0  # 2 GPUs x 30 min
    assert rep.all.used_hours == 1.25
    kinds = {r.kind: r for r in rep.kind_rows()}
    assert (kinds["model"].gpu_hours, kinds["model"].used_gpu_hours, kinds["idle_model"].used_gpu_hours) == (
        2.0,
        1.0,
        0.25,
    )
    gpus = {r.gpu_index: r for r in rep.gpu_rows()}
    assert (gpus[0].gpu_hours, gpus[1].gpu_hours, gpus[0].avg_utilization) == (1.25, 1.0, 90.0)
    assert rep.cpu_core_seconds[alice.id] == 240.0
    assert rep.booking_acc.seconds == 2 * 3600  # samples taken during bookings only


async def test_scope_mine_only_sees_the_callers_rows(db, rows):
    alice, bob = await rows.user(), await rows.user()
    lane = await rows.lane(gpus="0")
    await rows.booking(alice, T0, T0 + H, lane)
    await rows.booking(bob, T0 + H, T0 + 2 * H, lane)
    await minutes(rows, T0, 10, lane, "model", alice)
    await minutes(rows, T0 + H, 20, lane, "model", bob)
    await minutes(rows, T0 + 2 * H, 5, lane, "idle_model")
    rep = await report(db, user_id=alice.id)
    assert set(rep.booked) == {alice.id}
    assert rep.all.busy == 600


# ---- F16: used <= booked + shared --------------------------------------------------


async def test_f16_holds_for_bookings_and_shared_use(db, rows):
    # pm2-2's shape: little booked time, the default model busy while nobody books. "used" > "booked" here,
    # which the page shows unexplained today, but used <= booked + shared holds.
    alice = await rows.user()
    lane = await rows.lane(gpus="0")
    await rows.booking(alice, T0, T0 + 5 * M, lane)
    await minutes(rows, T0, 5, lane, "model", alice)
    await minutes(rows, T0 + 10 * M, 15, lane, "idle_model")
    await minutes(rows, T0 + 30 * M, 10, lane, "idle_devenv")
    await minutes(rows, T0 + H, 30, lane, "free", busy=False)
    rep = await report(db)
    assert rep.all.used_hours > round(sum(rep.booked.values()) / 3600, 2)
    assert f16_gap_seconds(rep) <= 0
    assert rep.people[alice.id].busy <= rep.booked[alice.id]


@pytest.mark.xfail(
    strict=True,
    reason="F16: samples are whole minutes attributed to whoever holds the lane at sample time, while booked "
    "time is exact. A booking released 30 s after it began still gets a full busy minute (pm2-2: 0.01 GPU-h "
    "booked is about 36 s). Fix: clip sample seconds to the booking, or count booked time per sampled minute.",
)
async def test_f16_booking_released_mid_minute(db, rows):
    alice = await rows.user()
    lane = await rows.lane(gpus="0")
    await rows.booking(alice, T0, T0 + timedelta(seconds=30), lane, released_at=T0 + timedelta(seconds=30))
    await minutes(rows, T0, 1, lane, "model", alice)
    rep = await report(db)
    assert f16_gap_seconds(rep) <= 0


@pytest.mark.xfail(
    strict=True,
    reason="F16: a busy GPU while the lane is 'free' (work SlotServe did not start, or a model or container "
    "outliving its booking) counts as used but is neither booked nor shared. Fix: report it as its own "
    "'unattributed' figure and leave it out of used.",
)
async def test_f16_busy_while_free(db, rows):
    lane = await rows.lane(gpus="0")
    await minutes(rows, T0, 3, lane, "free")
    rep = await report(db)
    assert f16_gap_seconds(rep) <= 0
