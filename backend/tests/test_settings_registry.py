"""Settings registry (A04, F07 backend, W1-03): metadata, change preview, before/after audit, undo, sudo for
root-equivalent settings, per-person extra hours, audit filters and the generated configuration docs."""

import csv
import io
from datetime import datetime, timedelta

from conftest import make_gguf
from slotserve.db import utcnow
from slotserve.security import sudo
from slotserve.services import app_settings


def iso(dt: datetime) -> str:
    return dt.isoformat()


def _hours(h: float) -> datetime:
    return utcnow() + timedelta(hours=h)


async def _setup(make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    alice = await make_client("alice")
    make_gguf(models_dir, "small.gguf")
    await admin.post("/api/models/scan")
    model = (await admin.get("/api/models")).json()[0]["id"]
    users = {u["username"]: u["id"] for u in (await admin.get("/api/admin/users")).json()}
    return admin, bob, alice, model, users


async def _book(client, start, end, model_id, **extra):
    return await client.post(
        "/api/reservations", json={"model_id": model_id, "start_at": iso(start), "end_at": iso(end), **extra}
    )


def _week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of this week (the test server runs in UTC)."""
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


async def _two_hour_window(offset_h: float = 0.0) -> tuple[datetime, datetime]:
    """A 2 h window in the current quota week, at least 30 min ahead (or in next week's start if too late)."""
    now = utcnow()
    start = now + timedelta(hours=1 + offset_h)
    week_end = _week_start(now) + timedelta(days=7)
    if start + timedelta(hours=2) > week_end:  # too close to the end of the week: use the next week
        start = week_end + timedelta(hours=1 + offset_h)
    return start, start + timedelta(hours=2)


# ---- metadata --------------------------------------------------------------------------------------------------


async def test_settings_carry_registry_metadata(make_client):
    admin = await make_client("root")
    view = {s["key"]: s for s in (await admin.get("/api/admin/settings")).json()}
    assert set(view) == set(app_settings.EDITABLE)
    quota = view["quota_weekly_hours"]
    assert quota["env_var"] == "SLOTSERVE_QUOTA_WEEKLY_HOURS"
    assert quota["tier"] == "basic" and quota["card"] == "fairness" and quota["has_impact"]
    assert quota["group"] == "booking" and quota["source"] == "default"
    assert view["custom_engine_cmd"]["danger"] == "root" and view["custom_engine_cmd"]["tier"] == "expert"
    assert view["devenv_cpu_per_user"]["depends_on"] == "devenv_cpu_enabled"
    assert view["approval_hours"]["depends_on"] == "approval_mode=long"
    assert view["invite_code"]["secret"] and view["invite_code"]["source"] == "env"
    assert [s["order"] for s in view.values()] == sorted(s["order"] for s in view.values())
    assert not any(s["restart_required"] for s in view.values())

    full = {s["key"]: s for s in (await admin.get("/api/admin/settings?include_deployment=true")).json()}
    assert full["port"]["restart_required"] and full["llama_port"]["source"] == "env"
    assert full["secret_key"]["value"] == "" and full["secret_key"]["has_value"]
    masked = {s["key"]: s for s in (await admin.get("/api/admin/settings?mask_secrets=true")).json()}
    assert masked["invite_code"]["value"] == "" and masked["invite_code"]["has_value"]
    assert not masked["hf_token"]["has_value"]

    r = await admin.put("/api/admin/settings", json={"port": 9000})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_setting"


def test_registry_covers_every_setting():
    from slotserve.config import Settings

    assert set(app_settings.REGISTRY) == set(Settings.model_fields)
    groups = {g for g, *_ in app_settings.GROUPS}
    cards = {c for c, *_ in app_settings.CARDS}
    for key, spec in app_settings.REGISTRY.items():
        assert spec.group in groups, key
        assert spec.card is None or spec.card in cards, key
        assert spec.doc[0] and spec.doc[1], f"{key}: needs an English and a Chinese description"
        parent = (spec.depends_on or "").split("=")[0]
        assert not parent or parent in app_settings.EDITABLE, key
        assert not (spec.card and spec.tier != "basic"), key
    assert {"vllm_cmd", "sglang_cmd", "custom_engine_cmd", "devenv_storage_dir"} <= app_settings.DANGER_KEYS


def test_configuration_docs_are_generated_from_the_registry():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("gen_settings_doc", root / "tools" / "gen_settings_doc.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.main(["--check"]) == 0, "run python tools/gen_settings_doc.py"
    text = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "## First admin and setup code" in text and "## Requests from other pages" in text
    assert "`SLOTSERVE_QUOTA_WEEKLY_HOURS`" in text


# ---- audit before/after, undo, history -------------------------------------------------------------------------


async def test_audit_keeps_before_and_after_and_undo_restores(make_client):
    admin = await make_client("root")
    r = await admin.put("/api/admin/settings", json={"quota_weekly_hours": 6, "invite_code": "new-code"})
    assert r.status_code == 200
    entry = (await admin.get("/api/admin/audit?action=settings.update")).json()["items"][0]
    assert entry["detail"]["before"]["quota_weekly_hours"] == 0
    assert entry["detail"]["after"]["quota_weekly_hours"] == 6
    assert set(entry["detail"]["was_default"]) == {"quota_weekly_hours", "invite_code"}
    # secrets only as fingerprints
    assert entry["detail"]["after"]["invite_code"].startswith("fp:")
    assert "new-code" not in str(entry) and "test-invite" not in str(entry)
    assert entry["ip"]

    history = (await admin.get("/api/admin/settings/quota_weekly_hours/history")).json()
    assert history[0]["before"] == 0 and history[0]["after"] == 6 and history[0]["undoable"]
    assert not (await admin.get("/api/admin/settings/invite_code/history")).json()[0]["undoable"]

    preview = (await admin.post(f"/api/admin/audit/{entry['id']}/undo?dry_run=true")).json()
    assert preview["dry_run"] and [c["key"] for c in preview["changes"]] == ["quota_weekly_hours"]
    view = {s["key"]: s for s in (await admin.post(f"/api/admin/audit/{entry['id']}/undo")).json()}
    assert view["quota_weekly_hours"]["value"] == 0 and not view["quota_weekly_hours"]["overridden"]
    assert view["invite_code"]["value"] == "new-code"  # secrets are not undone: they are entered again
    undo = (await admin.get("/api/admin/audit")).json()["items"][0]
    assert undo["action"] == "settings.undo" and undo["detail"]["undo_of"] == entry["id"]
    assert undo["detail"]["reset"] == ["quota_weekly_hours"]

    r = await admin.post(f"/api/admin/audit/{undo['id'] + 100}/undo")
    assert r.status_code == 404
    login = (await admin.get("/api/admin/audit?action=user.*,auth.*")).json()
    r = await admin.post(f"/api/admin/audit/{login['items'][-1]['id']}/undo") if login["items"] else None
    assert r is None or r.json()["detail"]["code"] == "not_undoable"


async def test_unchanged_values_write_no_audit_entry(make_client):
    admin = await make_client("root")
    before = (await admin.get("/api/admin/audit")).json()["total"]
    await admin.put("/api/admin/settings", json={"max_reservation_hours": 8})  # the default, not overridden
    assert (await admin.get("/api/admin/audit")).json()["total"] == before


# ---- root-equivalent settings ----------------------------------------------------------------------------------


async def test_danger_settings_are_sensitive_and_need_sudo(make_client, app, monkeypatch):
    assert app_settings.ENFORCE_SUDO_FOR_DANGER  # the web UI asks for the password (W1-07)
    admin = await make_client("root")
    r = await admin.put("/api/admin/settings", json={"custom_engine_cmd": "my-engine --port {port}"})
    assert r.status_code == 200  # signing up (or in) counts as entering the password
    entry = (await admin.get("/api/admin/audit")).json()["items"][0]
    assert entry["action"] == "settings.sensitive_update"
    assert entry["detail"]["after"] == {"custom_engine_cmd": "my-engine --port {port}"}

    real_now = sudo._now
    monkeypatch.setattr(sudo, "_now", lambda: real_now() + 10 * 60)  # sudo mode of the sign-up has expired
    r = await admin.put("/api/admin/settings", json={"custom_engine_cmd": ""})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "sudo_required"
    preview = (await admin.put("/api/admin/settings?dry_run=true", json={"custom_engine_cmd": ""})).json()
    assert preview["sudo_required"] is True
    # ordinary settings are not affected
    assert (await admin.put("/api/admin/settings", json={"slot_minutes": 15})).status_code == 200
    r = await admin.post("/api/auth/sudo", json={"password": "secret123"})
    assert r.status_code == 200
    assert (await admin.put("/api/admin/settings", json={"custom_engine_cmd": ""})).status_code == 200


async def test_storage_dir_refuses_system_directories(make_client):
    admin = await make_client("root")
    for path in ("/etc", "/etc/ssl", "/var/lib/docker/volumes", "/proc", "/lib64", "/root/", "//usr//local"):
        r = await admin.put("/api/admin/settings", json={"devenv_storage_dir": path})
        assert r.status_code == 400, (path, r.text)
        assert r.json()["detail"]["params"]["key"] == "devenv_storage_dir", r.text
    assert (await admin.put("/api/admin/settings", json={"devenv_storage_dir": "/srv/slotserve"})).status_code == 200
    assert (await admin.put("/api/admin/settings", json={"devenv_storage_dir": "/library"})).status_code == 200


# ---- change preview --------------------------------------------------------------------------------------------


async def test_quota_preview_lists_people_and_keeps_bookings(make_client, models_dir, app):
    admin, bob, alice, model, users = await _setup(make_client, models_dir)
    start, end = await _two_hour_window()
    assert (await _book(bob, start, end, model)).status_code == 201
    assert (await _book(bob, end, end + timedelta(hours=2), model)).status_code == 201
    assert (await _book(alice, end + timedelta(hours=2), end + timedelta(hours=3), model)).status_code == 201
    assert (await _book(admin, end + timedelta(hours=5), end + timedelta(hours=11), model)).status_code == 201

    r = await admin.put("/api/admin/settings?dry_run=true", json={"quota_weekly_hours": 3})
    preview = r.json()
    assert preview["dry_run"] and preview["changes"][0] == {
        "key": "quota_weekly_hours",
        "before": 0,
        "after": 3,
        "reset": False,
        "secret": False,
        "danger": None,
    }
    impact = preview["impacts"][0]
    assert impact["text_key"] == "settings.impact.quota_weekly_hours"
    people = {u["username"]: u for u in impact["users"]}
    assert set(people) == {"bob", "alice"}  # admins are exempt
    assert people["bob"]["over"] and not people["alice"]["over"]
    assert impact["params"]["over"] == 1
    assert {res["username"] for res in impact["reservations"]} == {"bob"}
    assert preview["notify_user_ids"] == sorted([users["bob"], users["alice"]])
    # nothing was stored
    assert (await admin.get("/api/config")).json()["quota_weekly_hours"] == 0

    r = await admin.put("/api/admin/settings?notify=true", json={"quota_weekly_hours": 3})
    assert r.status_code == 200
    mine = (await bob.get("/api/reservations?mine=true")).json()
    assert len(mine) == 2 and all(x["status"] == "booked" for x in mine)  # never silently cancelled
    week_end = _week_start(utcnow()) + timedelta(days=7)
    if end + timedelta(hours=4) < week_end:
        r = await _book(bob, end + timedelta(hours=3), end + timedelta(hours=4), model)
        assert r.json()["detail"]["code"] == "quota_weekly"


async def test_preview_of_length_advance_and_approval(make_client, models_dir):
    admin, bob, _alice, model, _users = await _setup(make_client, models_dir)
    assert (await _book(bob, _hours(30), _hours(36), model)).status_code == 201
    assert (await _book(bob, _hours(24 * 20), _hours(24 * 20 + 1), model)).status_code == 201

    impacts = {
        i["key"]: i
        for i in (
            await admin.put(
                "/api/admin/settings?dry_run=true",
                json={"max_reservation_hours": 4, "max_advance_days": 10, "approval_mode": "long", "approval_hours": 5},
            )
        ).json()["impacts"]
    }
    assert [r["reason"] for r in impacts["max_reservation_hours"]["reservations"]] == ["too_long"]
    assert [r["reason"] for r in impacts["max_advance_days"]["reservations"]] == ["too_far"]
    approval = impacts["approval_mode"]
    assert approval["params"]["mode"] == "long" and approval["params"]["hours"] == 5
    assert [r["reason"] for r in approval["reservations"]] == ["would_need_approval"]

    await admin.put("/api/admin/settings", json={"quota_upcoming": 0})
    upcoming = (await admin.put("/api/admin/settings?dry_run=true", json={"quota_upcoming": 1})).json()["impacts"][0]
    assert upcoming["users"][0]["username"] == "bob" and upcoming["users"][0]["open_bookings"] == 2
    assert len(upcoming["reservations"]) == 1


# ---- per-person extra hours ------------------------------------------------------------------------------------


async def test_extra_hours_let_one_person_book_beyond_the_quota(make_client, models_dir):
    admin, bob, alice, model, users = await _setup(make_client, models_dir)
    await admin.put("/api/admin/settings", json={"quota_weekly_hours": 2})
    start, end = await _two_hour_window()
    assert (await _book(bob, start, end, model)).status_code == 201
    r = await _book(bob, end, end + timedelta(hours=2), model)
    assert r.json()["detail"]["code"] == "quota_weekly"

    week_end = _week_start(utcnow()) + timedelta(days=14)
    r = await admin.post(
        f"/api/admin/users/{users['bob']}/quota-grants",
        json={"extra_hours": 10, "until": iso(week_end), "reason": "thesis defence"},
    )
    assert r.status_code == 201, r.text
    grant = r.json()
    assert grant["active"] and grant["by"] == "root" and grant["extra_hours"] == 10
    assert (await _book(bob, end, end + timedelta(hours=2), model)).status_code == 201
    # only bob
    r = await _book(alice, end + timedelta(hours=3), end + timedelta(hours=6), model)
    assert r.json()["detail"]["code"] == "quota_weekly"

    policy = (await admin.get(f"/api/admin/users/{users['bob']}/policy")).json()
    assert policy["base_weekly_hours"] == 2 and policy["open_bookings"] == 2
    assert len(policy["grants"]) == 1
    audit = (await admin.get("/api/admin/audit?action=user.quota_grant")).json()["items"][0]
    assert audit["target"] == "bob" and audit["detail"]["reason"] == "thesis defence"

    r = await admin.delete(f"/api/admin/users/{users['bob']}/quota-grants/{grant['id']}")
    assert r.status_code == 204
    policy = (await admin.get(f"/api/admin/users/{users['bob']}/policy")).json()
    assert policy["grants"][0]["revoked_by"] == "root" and not policy["grants"][0]["active"]
    assert (await admin.delete(f"/api/admin/users/{users['bob']}/quota-grants/{grant['id']}")).status_code == 404
    r = await admin.post(
        f"/api/admin/users/{users['bob']}/quota-grants", json={"extra_hours": 1, "until": iso(_hours(-1))}
    )
    assert r.json()["detail"]["code"] == "grant_until_past"


async def test_expired_grant_no_longer_counts(make_client, app):
    from sqlalchemy import select

    from slotserve.db import SessionLocal
    from slotserve.orm import User
    from slotserve.services import policies

    admin = await make_client("root")
    await make_client("bob")
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == "bob"))).scalar_one()
        root = (await db.execute(select(User).where(User.username == "root"))).scalar_one()
        now = utcnow()
        await policies.add_grant(db, user, 5, now - timedelta(days=20), "old", root, now - timedelta(days=30))
        await db.commit()
        items = await policies.grants(db, user.id)
        assert policies.extra_hours(items, now, now + timedelta(days=7)) == 0
        assert policies.extra_hours(items, now - timedelta(days=25), now - timedelta(days=18)) == 5
    assert admin


# ---- audit actions, filters, CSV -------------------------------------------------------------------------------


async def test_logins_keys_and_backup_downloads_are_audited_with_ip(make_client):
    admin = await make_client("root")
    other = await make_client()
    assert (await other.post("/api/auth/login", json={"username": "root", "password": "nope"})).status_code == 401
    assert (await other.post("/api/auth/login", json={"username": "root", "password": "secret123"})).status_code == 200
    assert (await other.post("/api/keys", json={"name": "laptop"})).status_code == 201
    backup = (await admin.post("/api/admin/backups")).json()["name"]
    assert (await admin.get(f"/api/admin/backups/{backup}")).status_code == 200

    query = "auth.login,auth.login_failed,key.create,backup.download"
    items = (await admin.get(f"/api/admin/audit?action={query}")).json()["items"]
    actions = [i["action"] for i in reversed(items)]
    assert actions == ["auth.login_failed", "auth.login", "key.create", "backup.download"]
    assert all(i["ip"] for i in items)
    assert "_ip" not in items[0]["detail"]

    me = (await admin.get("/api/auth/me")).json()["id"]
    mine = (await admin.get(f"/api/admin/audit?actor_id={me}&target=laptop")).json()
    assert [i["action"] for i in mine["items"]] == ["key.create"] and mine["total"] == 1

    r = await admin.get("/api/admin/audit.csv?action=auth.*")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "time", "actor", "action", "target", "ip", "detail"]
    assert {row[3] for row in rows[1:]} == {"auth.first_admin", "auth.login", "auth.login_failed"}

    names = (await admin.get("/api/admin/audit/actions")).json()
    assert "settings.sensitive_update" in names and "backup.download" in names


def test_every_recorded_action_is_listed():
    import re
    from pathlib import Path

    from slotserve.services.audit import AUDIT_ACTIONS

    pkg = Path(__file__).resolve().parents[1] / "slotserve"
    recorded = set()
    for path in pkg.rglob("*.py"):
        for call in re.findall(r"\brecord\((.*?)\)\n", path.read_text(encoding="utf-8"), re.S):
            recorded |= set(re.findall(r"\"([a-z_]+\.[a-z_]+)\"", call))
    assert recorded - AUDIT_ACTIONS == set()
