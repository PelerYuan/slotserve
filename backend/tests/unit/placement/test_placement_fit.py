"""placement.fit on one machine: which lane is chosen, and the reasons given for and against."""

from datetime import timedelta

from sqlalchemy import func, select

from slotserve import orm
from slotserve.db import utcnow
from slotserve.services import placement
from slotserve.services.placement import Change, Request

H = timedelta(hours=1)


def codes(reasons) -> set[str]:
    return {r.code for r in reasons}


async def test_a_busy_lane_is_passed_over_with_the_reason(db, rows, settings):
    now = utcnow()
    alice, bob = await rows.user("alice"), await rows.user("bob")
    gpu0, gpu1 = await rows.lane("GPU 0", "0", position=0), await rows.lane("GPU 1", "1", position=1)
    await rows.booking(alice, now - H, now + H, lane=gpu0)
    result = await placement.evaluate(db, Request(user_id=bob.id), (now, now + 2 * H), now, settings=settings())
    assert result.best.lane_id == gpu1.id
    busy = next(c for c in result.candidates if c.lane_id == gpu0.id)
    assert not busy.fits
    assert busy.reasons_no[0].code == "reservation_conflict"
    assert busy.reasons_no[0].params["username"] == "alice"
    assert [c.lane_id for c in result.candidates] == [gpu1.id, gpu0.id]


async def test_free_until_the_next_booking(db, rows, settings):
    now = utcnow()
    alice = await rows.user()
    lane = await rows.lane("GPU 0", "0")
    later = await rows.booking(alice, now + 5 * H, now + 6 * H, lane=lane)
    await rows.booking(alice, now + 4 * H, now + 5 * H, lane=lane, status="cancelled")
    cand = (await placement.fit(db, Request(user_id=alice.id), (now, now + 2 * H), now, settings=settings()))[0]
    assert cand.fits
    assert cand.free_until == later.start_at
    assert codes(cand.reasons_ok) == {"free_until"}
    nothing_ahead = (
        await placement.fit(db, Request(user_id=alice.id), (now + 7 * H, now + 8 * H), now, settings=settings())
    )[0]
    assert nothing_ahead.free_until is None and "free_ahead" in codes(nothing_ahead.reasons_ok)


async def test_the_lane_that_has_the_model_loaded_is_preferred(db, rows, settings):
    now = utcnow()
    user = await rows.user()
    model = await rows.model()
    await rows.lane("A", "0", position=0)
    b = await rows.lane("B", "1", position=1, default_model_id=model.id)
    req = Request(user_id=user.id, kind="model", model_id=model.id)
    result = await placement.evaluate(db, req, (now, now + H), now, settings=settings())
    assert result.best.lane_id == b.id
    assert "model_is_default" in codes(result.best.reasons_ok)


async def test_quota_refuses_and_says_how_much_is_left(db, rows, settings):
    now = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
    if now.weekday() == 6:
        now -= timedelta(days=1)  # keep both bookings in one week
    user = await rows.user()
    lane = await rows.lane()
    await rows.booking(user, now + 2 * H, now + 5 * H, lane=lane)
    s = settings(quota_weekly_hours=4, timezone="UTC")
    result = await placement.evaluate(db, Request(user_id=user.id), (now + 6 * H, now + 8 * H), now, settings=s)
    assert result.best is None
    assert codes(result.reasons_no) == {"quota_weekly"}
    left = next(r for r in result.reasons_ok if r.code == "quota_left")
    assert left.params == {"hours": 1.0, "limit": 4}
    # within what is left it fits
    result = await placement.evaluate(db, Request(user_id=user.id), (now + 6 * H, now + 7 * H), now, settings=s)
    assert result.best is not None


async def test_admins_are_exempt_and_approval_is_flagged(db, rows, settings):
    now = utcnow()
    admin, member = await rows.user(is_admin=True), await rows.user()
    await rows.lane()
    s = settings(quota_weekly_hours=1, approval_mode="all")
    mine = await placement.evaluate(db, Request(user_id=admin.id), (now, now + 3 * H), now, settings=s)
    assert mine.best is not None and not mine.needs_approval
    assert "quota_left" not in codes(mine.reasons_ok)
    theirs = await placement.evaluate(db, Request(user_id=member.id), (now, now + H), now, settings=s)
    assert theirs.best.needs_approval and "needs_approval" in codes(theirs.best.reasons_ok)


async def test_window_is_returned_as_it_will_be_booked(db, rows, settings):
    now = utcnow()
    user = await rows.user()
    await rows.lane()
    result = await placement.evaluate(
        db, Request(user_id=user.id), (now - timedelta(minutes=5), now + H), now, settings=settings()
    )
    assert result.window == (now, now + H)
    too_long = await placement.evaluate(db, Request(user_id=user.id), (now, now + 20 * H), now, settings=settings())
    assert codes(too_long.reasons_no) == {"reservation_too_long"}
    assert all(not c.fits for c in too_long.candidates)


async def test_only_the_asked_lane_and_gpu_count(db, rows, settings):
    now = utcnow()
    user = await rows.user()
    await rows.lane("A", "0")
    two = await rows.lane("B", "1,2")
    only = await placement.fit(db, Request(user_id=user.id, lane_id=two.id), (now, now + H), now, settings=settings())
    assert [c.lane_id for c in only] == [two.id]
    result = await placement.evaluate(db, Request(user_id=user.id, gpu=2), (now, now + H), now, settings=settings())
    assert result.best.lane_id == two.id
    assert codes(result.candidates[1].reasons_no) == {"gpu_count_short"}


async def test_a_new_kept_environment_is_not_created_by_fit(db, rows, settings):
    now = utcnow()
    user = await rows.user()
    await rows.lane()
    db.add(orm.SshKey(user_id=user.id, name="k", public_key="ssh-ed25519 AAAA", fingerprint="fp"))
    tpl = orm.DevTemplate(name="pytorch", image="pt", kind="builtin", enabled=True)
    db.add(tpl)
    await db.flush()
    req = Request(user_id=user.id, kind="devenv", devenv_template_id=tpl.id, devenv_project_name="thesis")
    s = settings(devenv_enabled=True, devenv_projects=1)
    assert (await placement.evaluate(db, req, (now, now + H), now, settings=s)).best is not None
    assert (await db.execute(select(func.count(orm.DevProject.id)))).scalar_one() == 0
    full = settings(devenv_enabled=True, devenv_projects=0)
    assert codes((await placement.evaluate(db, req, (now, now + H), now, settings=full)).reasons_no) == {
        "devenv_project_limit"
    }


async def test_next_free_window_after_a_conflict(db, rows, settings):
    now = utcnow()
    alice, bob = await rows.user(), await rows.user()
    lane = await rows.lane()
    await rows.booking(alice, now - H, now + 3 * H, lane=lane)
    s = settings()
    assert (await placement.evaluate(db, Request(user_id=bob.id), (now, now + 2 * H), now, settings=s)).best is None
    (start, end), cand = await placement.next_free(db, Request(user_id=bob.id), (now, now + 2 * H), now, settings=s)
    assert (start, end) == (now + 3 * H, now + 5 * H)
    assert cand.lane_id == lane.id and cand.fits


async def test_dry_run_under_other_limits(db, rows, settings, monkeypatch):
    now = utcnow()
    user = await rows.user()
    await rows.lane()
    monkeypatch.setattr(placement, "get_settings", lambda: settings())
    change = Change(request=Request(user_id=user.id), window=(now, now + 3 * H))
    assert (await placement.dry_run(db, change, now)).admitted
    change.limits = {"max_reservation_hours": 2}
    impact = await placement.dry_run(db, change, now)
    assert not impact.admitted and codes(impact.reasons_no) == {"reservation_too_long"}


async def test_policy_for_reads_the_global_limits(rows, settings):
    user = await rows.user()
    limits = placement.policy_for(user, settings(quota_weekly_hours=6, approval_mode="long"))
    assert limits.quota_weekly_hours == 6 and limits.approval_mode == "long" and not limits.exempt
