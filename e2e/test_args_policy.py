"""Launch-argument policy in the UI: a member's request for an admin, the admin's confirmation and approval.

Runs against its own SlotServe process so the "first account is the admin" rule does not depend on test order.
"""

import os
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import Page, expect

from conftest import INVITE, PASSWORD, ROOT, _free_port, sign_in


@pytest.fixture(scope="module")
def own_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("slotserve-args")
    models = tmp / "models"
    models.mkdir()
    (models / "member-model.gguf").write_bytes(b"GGUF" + b"\0" * 2048)
    port = _free_port()
    env = {
        **os.environ,
        "SLOTSERVE_PORT": str(port),
        "SLOTSERVE_HOST": "127.0.0.1",
        "SLOTSERVE_DATA_DIR": str(tmp / "data"),
        "SLOTSERVE_MODELS_DIR": str(models),
        "SLOTSERVE_INVITE_CODE": INVITE,
        "SLOTSERVE_SECRET_KEY": "e2e-secret",
        "SLOTSERVE_LLAMA_SERVER_CMD": f'"{sys.executable}" "{ROOT / "tools" / "mock_llama_server.py"}"',
        "SLOTSERVE_LLAMA_PORT": str(_free_port()),
        "SLOTSERVE_DEVENV_WEB_PORT": str(_free_port()),
    }
    log = (tmp / "server.log").open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "slotserve"], cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                log.close()
                raise RuntimeError((tmp / "server.log").read_text(errors="replace")[-4000:]) from None
            time.sleep(0.3)
    yield base, models
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def _register(page: Page, base: str, username: str) -> None:
    resp = page.request.post(
        f"{base}/api/auth/register", data={"username": username, "password": PASSWORD, "invite_code": INVITE}
    )
    assert resp.ok, resp.text()
    page.request.post(f"{base}/api/auth/logout")


def test_member_requests_and_admin_approves(page: Page, own_server):
    base, models = own_server
    _register(page, base, "boss")  # first account: admin
    _register(page, base, "member")

    sign_in(page, base, "member")
    page.goto(f"{base}/models")
    page.get_by_role("button", name="Add model").click()
    dialog = page.get_by_role("dialog")
    dialog.locator("input").nth(0).fill("member-model")
    dialog.get_by_placeholder("/models/Qwen3-8B-Q4_K_M.gguf").fill(str(models / "member-model.gguf"))
    dialog.locator("textarea").first.fill("-ngl 99 --log-file /tmp/x.log")
    dialog.get_by_role("button", name="Save").click()
    # the arguments need an admin: the form offers to send a request
    expect(page.get_by_text("before the model uses them: --log-file")).to_be_visible()
    page.get_by_role("button", name="Ask an admin").click()
    expect(page.get_by_text("Arguments awaiting approval")).to_be_visible()
    model = next(m for m in page.request.get(f"{base}/api/models").json() if m["name"] == "member-model")
    assert model["args_pending"] == "-ngl 99 --log-file /tmp/x.log"
    assert model["needs_approval"] == ["--log-file"]

    page.request.post(f"{base}/api/auth/logout")
    sign_in(page, base, "boss")
    page.goto(f"{base}/models")
    row = page.locator("li").filter(has_text="member-model")
    row.get_by_role("button").last.click()
    page.get_by_text("Approve requested arguments").click()
    # --log-file writes files: the admin confirms explicitly
    page.get_by_role("button", name="Use them").click()
    expect(page.get_by_text("Arguments awaiting approval")).to_have_count(0)
    model = next(m for m in page.request.get(f"{base}/api/models").json() if m["name"] == "member-model")
    assert model["args"] == "-ngl 99 --log-file /tmp/x.log" and model["args_pending"] is None
    assert not page.errors  # type: ignore[attr-defined]
