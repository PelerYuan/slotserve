"""/api/health with checks[], alerts on level changes, the in-app inbox and the metrics gauges (A05, R1-15)."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from slotserve.db import SessionLocal, utcnow
from slotserve.orm import Notification, User


async def _inbox(client, **params):
    r = await client.get("/api/me/notifications", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _wait_inbox(client, event: str, timeout: float = 5.0):
    """The inbox item for ``event``, within ``timeout`` seconds (the acceptance limit for admins)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        items = [i for i in (await _inbox(client))["items"] if i["event"] == event]
        if items:
            return items
        await asyncio.sleep(0.1)
    raise AssertionError(f"{event} never reached the inbox")


async def test_health_needs_the_metrics_token_or_an_admin(app, make_client):
    admin = await make_client("root")
    member = await make_client("bob")
    assert (await (await make_client()).get("/api/health")).status_code == 401
    assert (await member.get("/api/health")).status_code == 401
    body = (await admin.get("/api/health")).json()
    assert {"status", "version", "problems", "checks", "nodes", "lanes", "downloads", "node_list"} <= set(body)
    ids = {c["id"] for c in body["checks"]}
    assert {"database", "disk", "engine", "docker", "backup", "chat", "tasks", "secrets"} <= ids
    check = next(c for c in body["checks"] if c["id"] == "database")
    assert check["level"] == "ok" and check["title"] and check["group"] == "system"
    chat = next(c for c in body["checks"] if c["id"] == "chat")
    assert chat["title_key"] == "chat_none" and chat["onboarding"] is True  # alerts only in the inbox
    await admin.put("/api/admin/settings", json={"metrics_token": "m-token-1"})
    bearer = {"authorization": "Bearer m-token-1"}
    anon = await make_client()
    assert (await anon.get("/api/health", headers=bearer)).status_code == 200
    assert (await anon.get("/api/health", headers={"authorization": "Bearer nope"})).status_code == 401


async def test_strict_answers_503_while_a_check_is_at_error(app, make_client):
    admin = await make_client("root")
    r = await admin.get("/api/health", params={"strict": "true"})
    assert r.status_code == 200, r.json()["problems"]
    app.state.settings.llama_server_cmd = "/nonexistent/llama-server-xyz"
    await admin.post("/api/health/checks/engine/run")
    r = await admin.get("/api/health", params={"strict": "true"})
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert any("llama-server-xyz" in p for p in body["problems"])
    engine = next(c for c in body["checks"] if c["id"] == "engine")
    assert engine["level"] == "error" and engine["title_key"] == "engine_missing"
    assert (await admin.get("/api/health")).status_code == 200  # not strict: always 200


async def test_docker_down_alerts_once_after_two_minutes_and_recovers(app, make_client):
    admin = await make_client("root")
    member = await make_client("bob")
    monitor = app.state.health
    app.state.settings.devenv_enabled = True
    healthy = app.state.devenv.refresh_status

    async def down(force=False):
        return {
            "ok": False,
            "error": "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?",
            "version": None,
        }

    app.state.devenv.refresh_status = down
    await monitor.run_checks(force=True, ids={"docker"})
    assert monitor.results["docker"].level == "error"
    alerts = await _admin_rows(app, "check_alert")
    assert not alerts  # not before two minutes
    monitor.states["docker"].since = utcnow() - timedelta(minutes=2, seconds=5)
    for _ in range(3):
        await monitor.run_checks(force=True, ids={"docker"})
    items = await _wait_inbox(admin, "check_alert")
    assert len(items) == 1 and items[0]["category"] == "admin" and items[0]["level"] == "error"
    assert "Docker" in items[0]["message"] and items[0]["params"]["check"] == "docker"
    assert len(await _admin_rows(app, "check_alert")) == 1  # once, however often it runs
    body = (await admin.get("/api/health")).json()
    docker = next(c for c in body["checks"] if c["id"] == "docker")
    assert docker["level"] == "error" and docker["alerted"] and "daemon" in docker["evidence"]
    assert docker["diagnosis"]["id"] == "docker_unreachable"  # the error pattern library (services/diagnostics)

    app.state.devenv.refresh_status = healthy
    await monitor.run_checks(force=True, ids={"docker"})
    assert (await _wait_inbox(admin, "check_recovered"))[0]["level"] == "success"
    assert not [i for i in (await _inbox(member))["items"] if i["category"] == "admin"]


async def _admin_rows(app, event: str) -> list[Notification]:
    await asyncio.sleep(0.2)  # the notifier queue
    async with SessionLocal() as db:
        return list((await db.execute(select(Notification).where(Notification.event == event))).scalars())


async def test_inbox_without_any_chat_platform(app, make_client):
    admin = await make_client("root")
    member = await make_client("bob")
    notifier = app.state.notifier
    notifier.emit("load_failed", model="qwen", error="CUDA out of memory")
    notifier.emit("storage_down", storage="nas", error="mount error(13)")
    await _wait_inbox(admin, "load_failed")
    await _wait_inbox(admin, "storage_down")
    async with SessionLocal() as db:
        bob = (await db.execute(select(User).where(User.username == "bob"))).scalar_one()
    notifier.emit("waitlist_free", user_id=bob.id, when="10-22 10:00 – 11:00")
    mine = await _wait_inbox(member, "waitlist_free")
    assert mine[0]["category"] == "personal" and mine[0]["link"] == "/schedule"
    member_items = (await _inbox(member))["items"]
    assert {i["event"] for i in member_items} == {"waitlist_free"}  # no system alerts for members
    inbox = await _inbox(admin)
    assert inbox["unread"]["admin"] >= 2 and inbox["unread"]["total"] == inbox["unread"]["admin"]
    load = next(i for i in inbox["items"] if i["event"] == "load_failed")
    assert load["level"] == "error" and load["link"] == "/admin?tab=runtime" and "qwen" in load["message"]
    assert load["params"]["model"] == "qwen" and load["read_at"] is None
    r = await admin.post("/api/me/notifications/read", json={"ids": [load["id"]]})
    assert r.json()["admin"] == inbox["unread"]["admin"] - 1
    # someone else's ids are ignored
    r = await member.post("/api/me/notifications/read", json={"ids": [i["id"] for i in inbox["items"]]})
    assert r.json()["total"] == 1
    r = await admin.post("/api/me/notifications/read", json={"all": True, "category": "admin"})
    assert r.json() == {"total": 0, "personal": 0, "admin": 0}
    assert (await admin.get("/api/me/notifications/unread")).json()["total"] == 0
    assert len((await _inbox(admin, unread="true"))["items"]) == 0
    assert (await (await make_client()).get("/api/me/notifications")).status_code == 401
    # CsrfGuard: cookie writes need the header
    r = await member.post("/api/me/notifications/read", json={"all": True}, headers={"x-slotserve": ""})
    assert r.status_code == 403


async def test_approval_request_reaches_the_admin_inbox_and_the_result_the_member(app, make_client):
    admin = await make_client("root")
    member = await make_client("bob")
    await admin.put("/api/admin/settings", json={"approval_mode": "all"})
    start = (utcnow() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    r = await member.post(
        "/api/reservations",
        json={"start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(), "model_id": None},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"] if isinstance(r.json(), dict) else r.json()[0]["id"]
    request = (await _wait_inbox(admin, "approval_requested"))[0]
    assert request["category"] == "admin" and request["link"] == "/admin?tab=reservations"
    assert (await admin.post(f"/api/admin/reservations/{rid}/approve", json={})).status_code == 200
    approved = (await _wait_inbox(member, "booking_approved"))[0]
    assert approved["category"] == "personal" and approved["link"] == f"/schedule?reservation={rid}"


async def test_dismiss_needs_a_reason_and_is_audited(app, make_client):
    admin = await make_client("root")
    member = await make_client("bob")
    await admin.post("/api/health/run")
    chat = next(c for c in (await admin.get("/api/health")).json()["checks"] if c["id"] == "chat")
    assert chat["dismissible"]
    assert (await member.post("/api/health/checks/chat/dismiss", json={"reason": "x"})).status_code == 403
    assert (await admin.post("/api/health/checks/chat/dismiss", json={"reason": ""})).status_code == 422
    r = await admin.post("/api/health/checks/chat/dismiss", json={"reason": "we only use the inbox"})
    assert r.status_code == 200 and r.json()["dismissed"]["reason"] == "we only use the inbox"
    assert (await admin.post("/api/health/checks/database/dismiss", json={"reason": "x"})).status_code == 409
    audit = (await admin.get("/api/admin/audit")).json()
    rows = audit["items"] if isinstance(audit, dict) else audit
    assert any(a["action"] == "health.dismiss" and a["target"] == "chat" for a in rows)
    r = await admin.delete("/api/health/checks/chat/dismiss")
    assert r.status_code == 200 and r.json()["dismissed"] is None


async def test_metrics_expose_check_levels_and_backup_age(app, make_client):
    admin = await make_client("root")
    await admin.put("/api/admin/settings", json={"metrics_token": "m-token-2"})
    text = (await (await make_client()).get("/metrics", headers={"authorization": "Bearer m-token-2"})).text
    assert 'slotserve_check_status{check="database",group="system"} 0' in text
    assert "slotserve_backup_age_seconds " in text
