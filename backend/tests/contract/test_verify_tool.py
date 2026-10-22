"""tools/verify (the L4 real-hardware check) run against the test app with the fake Docker and the mock llama.cpp
server: proves the tool drives the public API correctly. SSH and web interfaces need a real host and are off."""

import sys
from pathlib import Path

import httpx
from sqlalchemy import update

from slotserve.db import SessionLocal
from slotserve.orm import Lane
from slotserve.services.devenv import SshBundle

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.verify.checks import BUILTIN_TEMPLATES, Options, Verifier
from tools.verify.report import FAIL, MANUAL, PASS, SKIP


async def test_verify_tool_against_the_test_app(app, make_client, models_dir, tmp_path):
    app.state.devenv._bundle = SshBundle(b"\x7fELFfake", b"hostkey", "SHA256:fakehostkey")
    admin = await make_client("root")
    r = await admin.put("/api/admin/settings", json={"devenv_enabled": True, "devenv_cpu_enabled": True})
    assert r.status_code == 200, r.text
    # Without nvidia-smi (CI) the default lane names no GPUs and GPU environments refuse it: give it GPU 0.
    async with SessionLocal() as db:
        await db.execute(update(Lane).where(Lane.gpu_indices == "").values(gpu_indices="0"))
        await db.commit()
    builtins = {t["builtin_key"] for t in (await admin.get("/api/devenv/templates")).json()}
    assert builtins == set(BUILTIN_TEMPLATES)  # the tool knows every built-in template
    (models_dir / "tiny.gguf").write_bytes(b"GGUF" + b"\0" * 1020)
    assert (await admin.post("/api/models/scan")).status_code == 200
    # what the storage check's helper container prints (df, write test) on a real host
    app.state.fake_docker.helper_output["==DF=="] = "==DF==\n/dev/sdb1 1000000 250000 750000 25% /s\n==WRITE== ok\n"

    opts = Options(
        url="http://test",
        templates=("pytorch", "vscode"),  # each takes a few devenv ticks; all six run on real hardware
        admin_user="root",
        admin_password="secret123",
        storage_host_path="/srv/verify-shared",
        ssh=False,
        web=False,
        poll=0.3,
        load_timeout=60,
        devenv_timeout=60,
        workdir=tmp_path,
    )
    report = await Verifier(opts, transport=httpx.ASGITransport(app=app), echo=lambda line: None).run()
    statuses = {r.id: r.status for r in report.results}
    details = {r.id: r.detail for r in report.results}
    assert report.server_version not in ("", "?")
    for check in (
        "server",
        "account",
        "security.csrf",
        "gpu.booking_llm",
        "storage.check",
        "devenv.template.pytorch",
        "devenv.template.vscode",
        "devenv.cpu_only",
    ):
        assert statuses.get(check) == PASS, (check, statuses.get(check), details.get(check))
    assert statuses["security.csrf_browser"] == MANUAL and statuses["security.web_origin_browser"] == MANUAL
    # need SSH or a browser: skipped here, run on real hardware
    for check in ("security.container_hardening", "security.web_origin", "storage.mount", "security.forkbomb"):
        assert statuses[check] == SKIP, (check, details[check])
    assert FAIL not in statuses.values()
    assert "cleanup" not in statuses  # the account and the storage entry are gone again
    users = (await admin.get("/api/admin/users")).json()
    assert [u["username"] for u in users] == ["root"]
    assert (await admin.get("/api/admin/storage")).json() == []

    md, js = report.write(tmp_path / "out")
    assert md.name.startswith("verify-report-test-") and "| `gpu.booking_llm`" in md.read_text(encoding="utf-8")
    assert '"counts"' in js.read_text(encoding="utf-8")
    assert list(tmp_path.glob("csrf-poc-*.html"))
