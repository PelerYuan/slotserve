"""Engine argument governance (R1-04): members cannot run code in SlotServe through launch arguments or paths."""

import json

import pytest

from conftest import make_gguf
from slotserve.db import SessionLocal
from slotserve.orm import AuditLog, CatalogDeploy, User
from slotserve.runtime.engines import parse_flags
from slotserve.services.catalog_deploy import Deployer


@pytest.fixture
def emitted(app):
    """Events the API hands to the notifier."""
    events: list[tuple[str, dict]] = []
    original = app.state.notifier.emit

    def emit(event, **data):
        events.append((event, data))
        original(event, **data)

    app.state.notifier.emit = emit
    return events


async def _setup(make_client, models_dir):
    admin = await make_client("alice")  # the first account is the admin
    bob = await make_client("bob")
    make_gguf(models_dir, "bob.gguf")
    r = await bob.post("/api/models", json={"name": "bobs", "path": str(models_dir / "bob.gguf")})
    assert r.status_code == 201, r.text
    return admin, bob, r.json()


async def _actions() -> list[str]:
    async with SessionLocal() as db:
        return [a.action for a in (await db.execute(AuditLog.__table__.select())).all()]


@pytest.mark.parametrize(
    ("engine", "args", "flag"),
    [("vllm", "--trust-remote-code", "--trust-remote-code"), ("llama.cpp", "--log-file x", "--log-file")],
)
async def test_member_dangerous_args_are_refused(make_client, models_dir, app, engine, args, flag):
    _admin, bob, _model = await _setup(make_client, models_dir)
    path = str(models_dir / "bob.gguf") if engine == "llama.cpp" else "org/model"
    r = await bob.post("/api/models", json={"name": "m2", "engine": engine, "path": path, "args": args})
    assert r.status_code == 400
    assert r.json()["detail"] == {
        "code": "args_need_approval",
        "params": {"needs_approval": [flag], "can_request": True},
    }


async def test_member_paths_stay_in_the_model_library(make_client, models_dir, app):
    admin, bob, model = await _setup(make_client, models_dir)
    db_file = str(app.state.settings.data_dir / "slotserve.db")
    for path in ("/data/slotserve.db", db_file, str(models_dir / ".." / "data" / "slotserve.db")):
        r = await bob.post("/api/models", json={"name": "x", "engine": "vllm", "path": path})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "model_path_outside_library", path
        r = await bob.patch(f"/api/models/{model['id']}", json={"engine": "vllm", "path": path})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "model_path_outside_library", path
    # a repository id or a folder in the library is fine; admins may point anywhere
    assert (await bob.post("/api/models", json={"name": "repo", "engine": "vllm", "path": "Qwen/Qwen3-8B"})).is_success
    r = await admin.post("/api/models", json={"name": "anywhere", "engine": "vllm", "path": db_file})
    assert r.status_code == 201
    # a file argument outside the library needs an admin too
    r = await bob.patch(f"/api/models/{model['id']}", json={"args": f"-ngl 99 --mmproj {db_file}"})
    assert r.json()["detail"]["params"]["needs_approval"] == ["--mmproj"]
    mm = make_gguf(models_dir, "mmproj-bob.gguf")
    assert (await bob.patch(f"/api/models/{model['id']}", json={"args": f"-ngl 99 --mmproj {mm}"})).is_success


async def test_custom_engine_is_admin_only(make_client, models_dir, app):
    admin, bob, model = await _setup(make_client, models_dir)
    r = await bob.post("/api/models", json={"name": "c", "engine": "custom", "path": "org/x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "custom_engine_admin_only"
    r = await bob.patch(f"/api/models/{model['id']}", json={"engine": "custom", "path": model["file_path"]})
    assert r.status_code == 403
    assert (await admin.post("/api/models", json={"name": "c", "engine": "custom", "path": "org/x"})).status_code == 201


async def test_request_and_approval(make_client, models_dir, app, emitted):
    admin, bob, model = await _setup(make_client, models_dir)
    url = f"/api/models/{model['id']}"
    before = model["args"]

    r = await bob.patch(url, json={"args": "-ngl 99 --some-flag 1", "request_approval": True})
    assert r.status_code == 200
    out = r.json()
    assert out["args"] == before  # still the approved arguments
    assert out["args_pending"] == "-ngl 99 --some-flag 1"
    assert out["needs_approval"] == ["--some-flag"]
    assert ("args_approval_requested", {"user": "bob", "model": "bobs", "flags": "--some-flag"}) in emitted

    # sending the form back unchanged keeps the request
    assert (await bob.patch(url, json={"args": before, "description": "d"})).json()["args_pending"]
    # only admins approve
    assert (await bob.post(f"{url}/args/approve", json={})).status_code == 403
    r = await admin.post(f"{url}/args/approve", json={})
    assert r.status_code == 200
    assert r.json()["args"] == "-ngl 99 --some-flag 1" and r.json()["args_pending"] is None
    assert (await admin.post(f"{url}/args/approve", json={})).status_code == 409

    # approved flags are not asked about again when bob tunes the rest
    assert (await bob.patch(url, json={"args": "-ngl 50 --some-flag 1"})).is_success

    # a denied flag needs the admin's explicit confirmation when approving
    await bob.patch(url, json={"args": "--log-file /tmp/x", "request_approval": True})
    r = await admin.post(f"{url}/args/approve", json={})
    assert r.status_code == 400 and r.json()["detail"] == {
        "code": "args_confirm_required",
        "params": {"denied": ["--log-file"]},
    }
    # ... or rejects it
    r = await admin.delete(f"{url}/args/pending")
    assert r.status_code == 200 and r.json()["args_pending"] is None
    assert r.json()["args"] == "-ngl 50 --some-flag 1"

    actions = await _actions()
    for action in ("model.args_request", "model.args_update", "model.args_reject"):
        assert action in actions


async def test_member_create_with_request(make_client, models_dir, app):
    _admin, bob, _model = await _setup(make_client, models_dir)
    make_gguf(models_dir, "two.gguf")
    body = {"name": "two", "path": str(models_dir / "two.gguf"), "args": "-c 4096 --rpc host:1"}
    r = await bob.post("/api/models", json={**body, "request_approval": True})
    assert r.status_code == 201
    assert r.json()["args"] == app.state.settings.default_llama_args
    assert r.json()["args_pending"] == "-c 4096 --rpc host:1"
    assert r.json()["needs_approval"] == ["--rpc"]


async def test_admin_confirms_denied_flags(make_client, models_dir, app):
    admin, _bob, model = await _setup(make_client, models_dir)
    url = f"/api/models/{model['id']}"
    r = await admin.patch(url, json={"args": "-ngl 99 --slot-save-path /tmp/slots"})
    assert r.status_code == 400 and r.json()["detail"]["params"] == {"denied": ["--slot-save-path"]}
    r = await admin.patch(url, json={"args": "-ngl 99 --slot-save-path /tmp/slots", "confirm_args": True})
    assert r.status_code == 200
    # admins set anything else without a confirmation, and an accepted denied flag is not asked about again
    assert (await admin.patch(url, json={"args": "-ngl 10 --anything 1 --slot-save-path /tmp/slots"})).is_success
    async with SessionLocal() as db:
        rows = (await db.execute(AuditLog.__table__.select().where(AuditLog.action == "model.args_update"))).all()
    assert any(json.loads(row.detail)["denied"] == ["--slot-save-path"] for row in rows)


async def test_presets_follow_the_policy(make_client, models_dir, app):
    admin, bob, model = await _setup(make_client, models_dir)
    url = f"/api/models/{model['id']}/presets"
    r = await bob.post(url, json={"name": "bad", "args": "--log-file x"})
    assert r.status_code == 400
    assert r.json()["detail"]["params"] == {"needs_approval": ["--log-file"], "can_request": False}
    r = await bob.post(url, json={"name": "long", "args": "-c 32768"})
    assert r.status_code == 201
    preset = r.json()
    r = await admin.patch(f"{url}/{preset['id']}", json={"name": "long", "args": "--log-file x"})
    assert r.json()["detail"]["code"] == "args_confirm_required"
    r = await admin.patch(f"{url}/{preset['id']}", json={"name": "long", "args": "--log-file x", "confirm_args": True})
    assert r.is_success
    # bob may rename the preset an admin configured without touching its arguments
    assert (await bob.patch(f"{url}/{preset['id']}", json={"name": "renamed", "args": "--log-file x"})).is_success


async def test_custom_catalog_extras_wait_for_approval(make_client, models_dir, app):
    """Built-in catalog arguments are all allowed (unit test); a custom entry's extras may not be."""
    _admin, _bob, _model = await _setup(make_client, models_dir)
    async with SessionLocal() as db:
        bob = (await db.execute(User.__table__.select().where(User.username == "bob"))).first()
        alice = (await db.execute(User.__table__.select().where(User.username == "alice"))).first()
        deployer = Deployer(app.state.settings)
        args = f"-ngl 99 -c 4096 --mmproj {models_dir / 'mm.gguf'} --log-file /tmp/x"
        row = dict(entry_id="e", variant_id="v", engine="llama.cpp", model_name="m", args=args, target_path="x")
        kept, pending = await deployer.governed_args(db, CatalogDeploy(user_id=bob.id, **row))
        assert pending == args
        assert parse_flags(kept) == parse_flags(f"-ngl 99 -c 4096 --mmproj {models_dir / 'mm.gguf'}")
        assert await deployer.governed_args(db, CatalogDeploy(user_id=alice.id, **row)) == (args, None)
