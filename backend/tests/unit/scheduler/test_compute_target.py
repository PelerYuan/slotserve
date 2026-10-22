"""``compute_target``: which model a lane should run at a given moment.

Defaults used throughout: a 1.5 GB model loads in 1.5e9 / 150 MB/s + 15 s = 25 s, so the preload lead is
max(25 s + 60 s buffer, 3 min) = 180 s, and an exclusive booking frees the GPU stop_timeout (20 s) + buffer (60 s)
= 80 s early.
"""

from datetime import UTC, datetime, timedelta

import pytest

from slotserve.orm import ModelLoadEvent
from slotserve.runtime.scheduler import ManualOverride, compute_target, manual_key

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
LEAD = 180
EXCLUSIVE_LEAD = 80


def s(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture
async def world(rows):
    """One lane serving a default model, one other model and one user."""
    default = await rows.model("default")
    other = await rows.model("other")
    lane = await rows.lane("main", default_model_id=default.id)
    user = await rows.user("alice")
    return rows, lane, default, other, user


async def target(db, settings, current=None, manual=None, lane=None, **overrides):
    return await compute_target(db, settings(**overrides), NOW, current, manual, lane.id if lane else None)


async def test_no_lane_and_no_model_is_idle(db, settings):
    t = await target(db, settings)
    assert (t.model, t.reason, t.est_load_seconds) == (None, "idle", 0.0)


async def test_unbooked_lane_serves_its_default_model(db, settings, world):
    _, lane, default, _, _ = world
    t = await target(db, settings, lane=lane)
    assert (t.spec, t.reason, t.est_load_seconds) == ((default.id, None), "default", 25.0)


async def test_running_booking_wins_with_its_preset(db, settings, world):
    rows, lane, _, other, user = world
    preset = await rows.preset(other)
    r = await rows.booking(user, s(-60), s(3600), lane, other, preset_id=preset.id)
    t = await target(db, settings, lane=lane)
    assert (t.spec, t.reason, t.reservation.id) == ((other.id, preset.id), "reservation", r.id)


async def test_preset_of_another_model_is_ignored(db, settings, world):
    rows, lane, default, other, user = world
    foreign = await rows.preset(default)
    await rows.booking(user, s(-60), s(3600), lane, other, preset_id=foreign.id)
    t = await target(db, settings, lane=lane)
    assert t.spec == (other.id, None)


async def test_exclusive_booking_unloads(db, settings, world):
    rows, lane, _, _, user = world
    await rows.booking(user, s(-60), s(3600), lane)
    t = await target(db, settings, lane=lane)
    assert (t.model, t.reason) == (None, "exclusive")


async def test_booking_of_a_deleted_model_falls_back_to_the_default(db, settings, world):
    rows, lane, default, other, user = world
    other.deleted_at = NOW
    await rows.booking(user, s(-60), s(3600), lane, other)
    t = await target(db, settings, lane=lane)
    assert (t.spec, t.reason) == ((default.id, None), "default")


@pytest.mark.parametrize(("gap", "reason"), [(LEAD, "preload"), (LEAD + 1, "default")])
async def test_preload_starts_one_lead_before_the_booking(db, settings, world, gap, reason):
    rows, lane, default, other, user = world
    await rows.booking(user, s(gap), s(gap + 3600), lane, other)
    t = await target(db, settings, current=(default.id, None), lane=lane)
    assert t.reason == reason


async def test_preload_lead_follows_the_measured_load_time(db, settings, world):
    rows, lane, default, other, user = world
    for took in (290, 310):  # measured loads average 300 s: lead = 300 + 60
        db.add(ModelLoadEvent(model_id=other.id, started_at=s(-1000), finished_at=s(-1000 + took), success=True))
    await rows.booking(user, s(350), s(4000), lane, other)
    t = await target(db, settings, current=(default.id, None), lane=lane)
    assert (t.reason, t.est_load_seconds) == ("preload", 300.0)


@pytest.mark.parametrize(("current", "reason"), [(None, "preload-early"), ("default", "default")])
async def test_no_switch_back_to_the_default_just_before_another_switch(db, settings, world, current, reason):
    # Gap 240 s: past the lead, but within default load (25) + lead (180) + buffer (60) = 265 s.
    rows, lane, default, other, user = world
    await rows.booking(user, s(240), s(3600), lane, other)
    spec = (default.id, None) if current == "default" else None
    t = await target(db, settings, current=spec, lane=lane)
    assert t.reason == reason


async def test_upcoming_booking_of_the_default_model_needs_no_preload(db, settings, world):
    rows, lane, default, _, user = world
    await rows.booking(user, s(30), s(3600), lane, default)
    t = await target(db, settings, lane=lane)
    assert (t.spec, t.reason) == ((default.id, None), "default")


@pytest.mark.parametrize(("gap", "reason"), [(EXCLUSIVE_LEAD, "preload-exclusive"), (EXCLUSIVE_LEAD + 1, "default")])
async def test_exclusive_booking_frees_the_gpu_early(db, settings, world, gap, reason):
    rows, lane, _, _, user = world
    await rows.booking(user, s(gap), s(gap + 3600), lane)
    t = await target(db, settings, lane=lane)
    assert t.reason == reason


async def test_manual_override_beats_the_default_but_not_a_booking(db, settings, world):
    rows, lane, _, other, user = world
    manual = ManualOverride(other.id, None, None, "admin", NOW)
    t = await target(db, settings, manual=manual, lane=lane)
    assert (t.spec, t.reason) == ((other.id, None), "manual")
    await rows.booking(user, s(-60), s(3600), lane)
    t = await target(db, settings, manual=manual, lane=lane)
    assert t.reason == "exclusive"


async def test_manual_unload_and_expired_override(db, settings, world):
    _, lane, default, _, _ = world
    unload = ManualOverride(None, None, s(600), "admin", NOW)
    t = await target(db, settings, manual=unload, lane=lane)
    assert (t.model, t.reason) == (None, "manual")
    expired = ManualOverride(None, None, NOW, "admin", s(-600))
    t = await target(db, settings, manual=expired, lane=lane)
    assert (t.spec, t.reason) == ((default.id, None), "default")


@pytest.mark.parametrize("mode", ["devenv", "none"])
async def test_lane_without_idle_model_runs_nothing(db, settings, rows, mode):
    lane = await rows.lane(idle_mode=mode)
    t = await target(db, settings, lane=lane)
    assert (t.model, t.reason) == (None, f"idle-{mode}")


async def test_shared_environment_lane_preloads_earlier(db, settings, world):
    # The shared environment must stop first: the lead grows by stop_timeout (20 s) to 200 s.
    rows, _, _, other, user = world
    lane = await rows.lane(idle_mode="devenv")
    await rows.booking(user, s(LEAD + 10), s(3600), lane, other)
    t = await target(db, settings, lane=lane)
    assert t.reason == "preload"


async def test_bookings_on_other_lanes_do_not_count(db, settings, world):
    rows, lane, default, other, user = world
    second = await rows.lane("second")
    await rows.booking(user, s(-60), s(3600), second, other)
    t = await target(db, settings, lane=lane)
    assert (t.spec, t.reason) == ((default.id, None), "default")
    t = await target(db, settings, lane=second)
    assert (t.spec, t.reason) == ((other.id, None), "reservation")


def test_manual_override_round_trips_and_keys_per_lane():
    override = ManualOverride(3, 4, s(60), "admin", NOW)
    assert ManualOverride.from_json(override.to_json()) == override
    assert ManualOverride.from_json(ManualOverride(None, None, None, "a", NOW).to_json()).until is None
    assert (manual_key(None), manual_key(1), manual_key(2)) == (
        "manual_override",
        "manual_override",
        "manual_override:2",
    )
