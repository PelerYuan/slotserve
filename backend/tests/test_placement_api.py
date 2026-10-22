"""POST /api/placement/fit: QuickBook's automatic choice, booked as offered."""

from datetime import UTC, datetime, timedelta

H = timedelta(hours=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def test_booking_the_best_candidate_is_accepted(make_client):
    admin = await make_client("admin")
    member = await make_client("member")
    lanes = (await admin.get("/api/lanes")).json()
    second = (await admin.post("/api/admin/lanes", json={"name": "Second", "gpus": []})).json()
    now = now_utc()
    # someone holds the first lane right now
    r = await admin.post(
        "/api/reservations",
        json={
            "model_id": None,
            "lane_id": lanes[0]["id"],
            "start_at": now.isoformat(),
            "end_at": (now + H).isoformat(),
        },
    )
    assert r.status_code == 201, r.text

    body = {"kind": "exclusive", "start_at": now.isoformat(), "end_at": (now + 2 * H).isoformat()}
    r = await member.post("/api/placement/fit", json=body)
    assert r.status_code == 200, r.text
    fit = r.json()
    assert fit["best"]["lane_id"] == second["id"]
    assert fit["best"]["fits"] and fit["best"]["node_id"] is None
    busy = next(c for c in fit["candidates"] if c["lane_id"] == lanes[0]["id"])
    assert busy["reasons_no"][0]["code"] == "reservation_conflict"
    assert busy["reasons_no"][0]["params"]["username"] == "admin"
    assert fit["next_free"] is None  # something fits

    booked = await member.post(
        "/api/reservations",
        json={
            "model_id": None,
            "lane_id": fit["best"]["lane_id"],
            "start_at": fit["start_at"],
            "end_at": fit["end_at"],
        },
    )
    assert booked.status_code == 201, booked.text
    assert booked.json()[0]["lane_id"] == second["id"]

    # nothing is free now: the earliest window of the same length that fits is offered instead
    r = await member.post("/api/placement/fit", json=body)
    fit = r.json()
    assert fit["best"] is None
    assert fit["next_free"] is not None
    assert datetime.fromisoformat(fit["next_free"]["start_at"]) >= now + H - timedelta(seconds=1)


async def test_refusals_come_with_reasons(make_client, app):
    await make_client("admin")
    member = await make_client("member")
    now = now_utc()
    app.state.settings.quota_weekly_hours = 1
    try:
        r = await member.post(
            "/api/placement/fit",
            json={"kind": "exclusive", "start_at": now.isoformat(), "end_at": (now + 2 * H).isoformat()},
        )
        fit = r.json()
        assert fit["best"] is None
        assert [x["code"] for x in fit["reasons_no"]] == ["quota_weekly"]
        assert {"code": "quota_left", "params": {"hours": 1.0, "limit": 1}} in fit["reasons_ok"]
    finally:
        app.state.settings.quota_weekly_hours = 0


async def test_invalid_requests(make_client):
    member = await make_client("member")
    now = now_utc()
    window = {"start_at": now.isoformat(), "end_at": (now + H).isoformat()}
    assert (await member.post("/api/placement/fit", json={"kind": "model", **window})).status_code == 422
    assert (await member.post("/api/placement/fit", json={"kind": "devenv", **window})).status_code == 422
    backwards = {"kind": "exclusive", "start_at": window["end_at"], "end_at": window["start_at"]}
    assert (await member.post("/api/placement/fit", json=backwards)).status_code == 422
    anonymous = await make_client()
    assert (await anonymous.post("/api/placement/fit", json={"kind": "exclusive", **window})).status_code == 401
