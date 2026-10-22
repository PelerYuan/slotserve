"""Booking rules: window validation, series, approval and per-person quotas."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from slotserve.errors import ApiError
from slotserve.services import booking
from slotserve.services.booking import (
    _week_start,
    check_quota,
    check_window,
    needs_approval,
    series_windows,
)

NOW = datetime(2026, 1, 7, 10, 7, tzinfo=UTC)  # a Wednesday
H = timedelta(hours=1)


def code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.detail["code"]


# ---- check_window --------------------------------------------------------------


def test_start_inside_the_current_slot_snaps_to_now(settings):
    start, end = check_window(settings(), NOW - timedelta(minutes=7), NOW + H, NOW)
    assert (start, end) == (NOW, NOW + H)


def test_naive_times_are_taken_as_utc(settings):
    start, _ = check_window(settings(), (NOW + H).replace(tzinfo=None), (NOW + 2 * H).replace(tzinfo=None), NOW)
    assert start == NOW + H and start.tzinfo is not None


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (NOW - timedelta(minutes=31), NOW + H, "reservation_in_past"),
        (NOW + H, NOW + H + timedelta(minutes=5), "reservation_too_short"),
        (NOW + H, NOW + 9 * H + timedelta(minutes=3), "reservation_too_long"),
        (NOW + timedelta(days=31), NOW + timedelta(days=31) + H, "reservation_too_far"),
    ],
)
def test_invalid_windows(settings, start, end, expected):
    with pytest.raises(ApiError) as exc:
        check_window(settings(), start, end, NOW)
    assert code(exc) == expected


def test_max_length_allows_clock_skew(settings):
    start, end = check_window(settings(), NOW + H, NOW + 9 * H + timedelta(minutes=2), NOW)
    assert end - start == 8 * H + timedelta(minutes=2)


# ---- series and approval -------------------------------------------------------


def test_weekly_series_repeats_the_window():
    windows = series_windows(NOW, NOW + H, 2)
    assert windows == [(NOW + timedelta(weeks=i), NOW + H + timedelta(weeks=i)) for i in range(3)]


@pytest.mark.parametrize(
    ("mode", "admin", "hours", "expected"),
    [
        ("off", False, 10, False),
        ("all", False, 1, True),
        ("all", True, 1, False),
        ("long", False, 4, False),
        ("long", False, 5, True),
        ("long", True, 5, False),
    ],
)
def test_needs_approval(settings, mode, admin, hours, expected):
    actor = SimpleNamespace(is_admin=admin)
    assert needs_approval(settings(approval_mode=mode, approval_hours=4), actor, [(NOW, NOW + hours * H)]) is expected


def test_week_starts_on_local_monday_midnight():
    shanghai = ZoneInfo("Asia/Shanghai")
    # Monday 01:00 in Shanghai is still Sunday in UTC; the week began at Monday 00:00 local = Sunday 16:00 UTC.
    monday_early = datetime(2026, 1, 4, 17, 0, tzinfo=UTC)
    assert _week_start(monday_early, shanghai) == datetime(2026, 1, 4, 16, 0, tzinfo=UTC)
    assert _week_start(NOW, ZoneInfo("UTC")) == datetime(2026, 1, 5, tzinfo=UTC)


# ---- quotas ----------------------------------------------------------------------


async def test_admins_are_exempt_from_quotas(db, rows, settings):
    admin = await rows.user(is_admin=True)
    await check_quota(db, settings(quota_upcoming=1, quota_weekly_hours=1), admin, [(NOW, NOW + 8 * H)] * 3)


async def test_upcoming_quota_counts_holding_bookings_only(db, rows, settings, monkeypatch):
    monkeypatch.setattr(booking, "utcnow", lambda: NOW)
    user = await rows.user()
    kept = await rows.booking(user, NOW + H, NOW + 2 * H)
    await rows.booking(user, NOW + H, NOW + 2 * H, status="pending")
    await rows.booking(user, NOW + H, NOW + 2 * H, status="cancelled")
    await rows.booking(user, NOW - 3 * H, NOW - 2 * H)  # finished
    new = [(NOW + 5 * H, NOW + 6 * H)]
    with pytest.raises(ApiError) as exc:
        await check_quota(db, settings(quota_upcoming=2), user, new)
    assert (code(exc), exc.value.detail["params"]) == ("quota_upcoming", {"limit": 2})
    await check_quota(db, settings(quota_upcoming=2), user, new, exclude={kept.id})  # editing an existing one


async def test_weekly_quota_counts_only_the_part_inside_the_week(db, rows, settings):
    user = await rows.user()
    sunday_night = datetime(2026, 1, 4, 22, 0, tzinfo=UTC)
    await rows.booking(user, sunday_night, sunday_night + 4 * H)  # 2 h of it fall into the week of Jan 5
    await rows.booking(user, NOW, NOW + 3 * H)
    s = settings(quota_weekly_hours=8, timezone="UTC")
    await check_quota(db, s, user, [(NOW + 24 * H, NOW + 27 * H)])  # 2 + 3 + 3 = 8
    with pytest.raises(ApiError) as exc:
        await check_quota(db, s, user, [(NOW + 24 * H, NOW + 28 * H)])  # 9
    assert code(exc) == "quota_weekly"


async def test_weekly_quota_sums_new_windows_per_week(db, rows, settings):
    user = await rows.user()
    s = settings(quota_weekly_hours=8, timezone="not/a-zone")  # an invalid zone falls back to UTC
    await check_quota(db, s, user, series_windows(NOW, NOW + 8 * H, 3))  # one per week: fine
    with pytest.raises(ApiError):
        await check_quota(db, s, user, [(NOW, NOW + 5 * H), (NOW + 24 * H, NOW + 29 * H)])
