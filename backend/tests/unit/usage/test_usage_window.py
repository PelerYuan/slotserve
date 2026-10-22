"""``usage_report.Window`` buckets and ``Acc`` sums (no database)."""

from datetime import UTC, datetime, timedelta

import pytest

from slotserve.services.usage_report import Acc, Window

NOW = datetime(2026, 1, 7, 10, 7, tzinfo=UTC)
H = timedelta(hours=1)


@pytest.mark.parametrize(("days", "bucket_hours", "count"), [(1, 6, 4), (7, 6, 28), (8, 24, 8), (30, 24, 30)])
def test_bucket_size_follows_the_window_length(days, bucket_hours, count):
    w = Window(days, 0, NOW)
    assert (w.bucket_hours, w.count, len(w.labels())) == (bucket_hours, count, count)


def test_window_starts_on_a_local_bucket_edge():
    # 10:07 UTC lies in the 06:00 bucket; the window is that bucket and the three before it.
    w = Window(1, 0, NOW)
    assert w.since == datetime(2026, 1, 6, 12, tzinfo=UTC)
    assert w.labels() == ["2026-01-06T12:00", "2026-01-06T18:00", "2026-01-07T00:00", "2026-01-07T06:00"]
    # UTC+8: 10:07 UTC is 18:07 local; buckets (and labels) follow local time.
    shanghai = Window(1, 480, NOW)
    assert shanghai.since == datetime(2026, 1, 6, 16, tzinfo=UTC)
    assert shanghai.labels() == ["2026-01-07T00:00", "2026-01-07T06:00", "2026-01-07T12:00", "2026-01-07T18:00"]


def test_spread_splits_at_bucket_edges_and_clips_to_the_window():
    w = Window(1, 0, NOW)  # buckets from Jan 6 12:00: 12-18, 18-00, 00-06, 06-12 (until now = 10:07)
    since = w.since
    parts = list(w.spread(since - 2 * H, since + 7 * H))  # starts before the window
    assert parts == [(0, 6 * 3600.0), (1, 3600.0)]
    parts = list(w.spread(NOW - H, NOW + 5 * H))  # ends after now: only the past counts
    assert parts == [(3, 3600.0)]
    assert list(w.spread(since - 5 * H, since - H)) == []


def test_acc_sums_and_averages():
    acc = Acc()
    acc.add("model", 60, 60, 60 * 80.0, 60, 2.0, 5000, 8188)
    acc.add("idle_model", 60, 60, 60 * 20.0, 0, 1.0, 7000, 8188)
    acc.add("free", 60, 0, 0.0, 0, 0.0, None, None)  # no metrics for this minute
    assert (acc.seconds, acc.metered, acc.avg_util, acc.used_hours) == (180, 120, 50.0, 0.02)
    assert (acc.peak, acc.total, round(acc.wh, 1)) == (7000, 8188, 3.0)
    assert dict(acc.by_kind) == {"model": 60, "idle_model": 60, "free": 60}
    assert Acc().avg_util is None
