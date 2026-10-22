"""Dev environments page and environment card (W1-08: F03, F04, F10, F11 storage).

Runs a server of its own with the fake Docker API (backend/tests/docker_mock.py) and a fake nvidia-smi with two GPUs,
so environments really reach "running": the browser-first start without credentials, the SSH key step inline, the
card as a workbench (time left, +1 h, End in two steps, unique SSH alias, host key), shared environments with Join,
the confirmation before an app without a login of its own goes direct, and the admin confirmation of web-open.
"""

import base64
import os
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Browser, Page, expect

from conftest import ROOT, _free_port
from helpers import INVITE, PASSWORD, sign_in

GPU_ROWS = ["0, NVIDIA Test GPU, 1024, 24564, 37, 50, 120.0", "1, NVIDIA Test GPU, 2048, 24564, 63, 55, 180.0"]


def _ssh_key(comment: str) -> str:
    blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + os.urandom(32)
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def _fake_nvidia_smi(folder) -> None:
    folder.mkdir()
    if os.name == "nt":
        lines = "".join(f"echo {row}\r\n" for row in GPU_ROWS)
        (folder / "nvidia-smi.cmd").write_text(f"@echo off\r\n{lines}")
    else:
        script = folder / "nvidia-smi"
        script.write_text("#!/bin/sh\n" + "".join(f"echo '{row}'\n" for row in GPU_ROWS))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _wait(url: str, proc: subprocess.Popen, log_path) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # it answers
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(log_path.read_text(errors="replace")[-4000:]) from None
            time.sleep(0.3)


def _stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def _call(ctx, base: str, method: str, path: str, data=None):
    resp = ctx.request.fetch(f"{base}{path}", method=method, data=data, headers={"X-SlotServe": "1"})
    assert resp.ok, f"{method} {path}: {resp.status} {resp.text()}"
    return resp.json() if resp.status != 204 else None


@pytest.fixture(scope="module")
def server(tmp_path_factory, browser: Browser):
    """A server with dev environments on the fake Docker API: admin, bob (with an SSH key) and two GPU lanes."""
    tmp = tmp_path_factory.mktemp("slotserve-devenv")
    models = tmp / "models"
    models.mkdir()
    (models / "chat-small.gguf").write_bytes(b"GGUF" + b"\0" * 2048)
    (tmp / "data" / "devenv").mkdir(parents=True)
    (tmp / "dropbearmulti").write_bytes(b"fake")
    _fake_nvidia_smi(tmp / "bin")
    docker_port = _free_port()
    fake = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys, uvicorn; sys.path.insert(0, r'{ROOT / 'backend' / 'tests'}'); "
            "from docker_mock import FakeDocker; d = FakeDocker(); "
            f"uvicorn.run(d.app, host='127.0.0.1', port={docker_port}, log_level='warning')",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    port = _free_port()
    env = {
        **os.environ,
        "PATH": str(tmp / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "SLOTSERVE_PORT": str(port),
        "SLOTSERVE_HOST": "127.0.0.1",
        "SLOTSERVE_DATA_DIR": str(tmp / "data"),
        "SLOTSERVE_MODELS_DIR": str(models),
        "SLOTSERVE_INVITE_CODE": INVITE,
        "SLOTSERVE_SECRET_KEY": "e2e-secret",
        "SLOTSERVE_LLAMA_SERVER_CMD": f'"{sys.executable}" "{ROOT / "tools" / "mock_llama_server.py"}"',
        "SLOTSERVE_LLAMA_PORT": str(_free_port()),
        "SLOTSERVE_DEVENV_WEB_PORT": str(_free_port()),
        "SLOTSERVE_DOCKER_HOST": f"tcp://127.0.0.1:{docker_port}",
        "SLOTSERVE_DEVENV_ENABLED": "true",
        "SLOTSERVE_DROPBEAR_PATH": str(tmp / "dropbearmulti"),
        "SLOTSERVE_DEVENV_SSH_HOST": "gpu.lab.test",
        "SLOTSERVE_SCHEDULER_INTERVAL_SECONDS": "0.5",
        "MOCK_LOAD_SECONDS": "1",
        "MOCK_TOKEN_DELAY": "0",
    }
    log_path = tmp / "server.log"
    log = log_path.open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "slotserve"], cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait(f"http://127.0.0.1:{docker_port}/version", fake, log_path)
        _wait(f"{base}/healthz", proc, log_path)
        admin = browser.new_context()
        code = (tmp / "data" / "setup_code").read_text().strip()
        body = {"username": "admin", "password": PASSWORD, "setup_code": code}
        _call(admin, base, "POST", "/api/auth/register", body)
        first = _call(admin, base, "GET", "/api/lanes")[0]
        _call(admin, base, "PATCH", f"/api/admin/lanes/{first['id']}", {"name": "Lane A", "gpus": [0]})
        _call(admin, base, "POST", "/api/admin/lanes", {"name": "Lane B", "gpus": [1]})
        admin.close()
        for name, key in (("bob", True), ("carol", False), ("dave", False)):
            ctx = browser.new_context()
            body = {"username": name, "password": PASSWORD, "invite_code": INVITE}
            _call(ctx, base, "POST", "/api/auth/register", body)
            if key:
                _call(ctx, base, "POST", "/api/ssh-keys", {"public_key": _ssh_key(f"{name}@laptop")})
            ctx.close()
        yield base
    finally:
        _stop(proc)
        _stop(fake)
        log.close()


def _templates(page: Page, base: str) -> dict:
    return {t["builtin_key"]: t["id"] for t in _call(page, base, "GET", "/api/devenv/templates") if t["builtin_key"]}


def _lanes(page: Page, base: str) -> dict:
    return {lane["name"]: lane["id"] for lane in _call(page, base, "GET", "/api/lanes")}


def _stop_all(page: Page, base: str) -> None:
    for s in _call(page, base, "GET", "/api/devenv/sessions"):
        if s["mine"] and s["kind"] == "cpu" and s["state"] != "stopped":
            _call(page, base, "POST", f"/api/devenv/cpu/sessions/{s['id']}/stop")
        elif s["mine"] and s["kind"] == "booking" and s["reservation_id"] and s["state"] != "stopped":
            _call(page, base, "DELETE", f"/api/reservations/{s['reservation_id']}")


def test_browser_first_without_credentials(page: Page, server: str):
    """A new member without any SSH key or password opens VS Code in the browser with the default choices (F03)."""
    sign_in(page, server, "carol")
    page.goto(f"{server}/devenv")
    card = page.locator("[data-test=start-card]")
    expect(card).to_be_visible()
    # 1440x900-ish: "Choose a tool" and "Open" are visible without scrolling
    page.set_viewport_size({"width": 1440, "height": 900})
    open_button = card.locator("[data-test=start-open]")
    box = open_button.bounding_box()
    assert box and box["y"] + box["height"] <= 900
    expect(card.locator("[data-tool=vscode]")).to_have_attribute("aria-checked", "true")
    expect(page.get_by_text("Add an SSH public key or set an environment password first.")).to_have_count(0)
    expect(card.locator("[data-test=devenv-preview]")).to_contain_text("VS Code")
    open_button.click()
    env = page.locator("[data-test=env-card]").first
    expect(env.get_by_role("button", name="Open VS Code")).to_be_visible(timeout=20_000)
    expect(env.locator("[data-test=time-left]")).to_have_count(0)  # no booking: no clock
    _stop_all(page, server)
    assert not page.errors  # type: ignore[attr-defined]


def test_ssh_path_asks_for_a_key_inline(page: Page, server: str):
    """Terminal / SSH asks for a public key inside the card; once added, Open works without reloading the page."""
    sign_in(page, server, "dave")
    page.goto(f"{server}/devenv")
    card = page.locator("[data-test=start-card]")
    card.locator("[data-tool=ssh]").click()
    page.locator(".n-dropdown-option", has_text="PyTorch").first.click()
    creds = card.locator("[data-test=ssh-credentials]")
    expect(creds).to_be_visible()
    expect(card.locator("[data-test=start-open]")).to_be_disabled()
    creds.locator("summary").click()
    expect(creds.get_by_text("ssh-keygen -t ed25519")).to_be_visible()
    creds.locator("[data-test=ssh-key-input] textarea").fill(_ssh_key("dave@laptop"))
    creds.locator("[data-test=ssh-key-save]").click()
    expect(card.locator("[data-test=ssh-credentials]")).to_have_count(0, timeout=10_000)
    expect(card.locator("[data-test=start-open]")).to_be_enabled()
    card.locator("[data-test=start-open]").click()
    env = page.locator("[data-test=env-card]").first
    expect(env.locator("[data-test=devenv-connect]")).to_be_visible(timeout=20_000)
    _stop_all(page, server)
    assert not page.errors  # type: ignore[attr-defined]


def test_workbench_card(page: Page, server: str):
    """A GPU environment: time left, +1 h, GPU use, own Host alias and host key; End asks first (F04, F10)."""
    sign_in(page, server, "bob")
    templates, lanes = _templates(page, server), _lanes(page, server)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    booking = _call(
        page,
        server,
        "POST",
        "/api/reservations",
        {
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=2)).isoformat(),
            "devenv_template_id": templates["pytorch"],
            "lane_id": lanes["Lane B"],
            "note": "thesis-train",
        },
    )[0]
    _call(page, server, "POST", "/api/devenv/cpu/start", {"template_id": templates["pytorch"]})
    page.goto(f"{server}/devenv")
    gpu = page.locator("[data-test=env-card][data-kind=booking]")
    expect(gpu.locator(".name")).to_have_text("thesis-train", timeout=20_000)
    expect(gpu.get_by_text("Running")).to_be_visible(timeout=20_000)
    expect(gpu.locator("[data-test=time-left]")).to_contain_text("left")
    expect(gpu.locator("[data-test=gpu-live]")).to_contain_text("GPU 1 63%")
    expect(gpu.locator("[data-test=host-key]")).to_contain_text("SHA256:")

    # every environment has its own Host alias (no shared "slotserve-dev")
    cpu = page.locator("[data-test=env-card][data-kind=cpu]")
    expect(cpu.locator("[data-test=devenv-connect]")).to_be_visible(timeout=20_000)
    configs = [card.locator("details.cfg summary").inner_text() for card in (gpu, cpu)]
    assert "slotserve-thesis-train" in configs[0] and "slotserve-pytorch" in configs[1], configs
    assert "slotserve-dev" not in page.content()

    # +1 h moves the end of the booking
    gpu.locator("[data-test=extend]").click()
    expect(page.get_by_text("Extended by 1 h.")).to_be_visible()
    after = next(r for r in _call(page, server, "GET", "/api/reservations?mine=true") if r["id"] == booking["id"])
    assert datetime.fromisoformat(after["end_at"]) - datetime.fromisoformat(booking["end_at"]) == timedelta(hours=1)

    # End: two steps, with "Save as image" offered first
    gpu.locator("[data-test=end]").click()
    end = page.locator("[data-test=end-dialog]")
    expect(end).to_contain_text("/workspace stays")
    expect(end.locator("[data-test=end-save]")).to_be_visible()
    end.get_by_role("button", name="Cancel").click()
    expect(gpu).to_be_visible()
    gpu.locator("[data-test=end]").click()
    page.locator("[data-test=end-dialog] [data-test=end-confirm]").click()
    expect(page.locator("[data-test=env-card][data-kind=booking]")).to_have_count(0, timeout=20_000)
    _stop_all(page, server)
    assert not page.errors  # type: ignore[attr-defined]


def test_direct_port_needs_confirmation(page: Page, server: str):
    """Opening VS Code directly (no SlotServe login in front) asks first; cancelling keeps the login (R1-08)."""
    sign_in(page, server, "bob")
    templates = _templates(page, server)
    s = _call(page, server, "POST", "/api/devenv/cpu/start", {"template_id": templates["vscode"]})
    page.goto(f"{server}/devenv")
    card = page.locator(f"[data-test=env-card][data-session='{s['id']}']")
    expect(card.get_by_role("button", name="Open VS Code")).to_be_visible(timeout=20_000)
    card.locator("summary", has_text="Ports").click()
    port = card.locator(".port[data-port='8080']")
    port.get_by_text("Direct", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("without the SlotServe login")
    dialog.get_by_role("button", name="Cancel").click()
    ports = _call(page, server, "GET", f"/api/devenv/sessions/{s['id']}/ports")
    assert [p["mode"] for p in ports["ports"] if p["port"] == 8080] == ["login"]
    port.get_by_text("Direct", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Open it directly").click()
    expect(port.get_by_text("SlotServe login")).to_be_visible()
    for _ in range(40):
        ports = _call(page, server, "GET", f"/api/devenv/sessions/{s['id']}/ports")
        if [p["mode"] for p in ports["ports"] if p["port"] == 8080] == ["direct"]:
            break
        time.sleep(0.25)
    else:
        raise AssertionError(ports)
    _call(page, server, "DELETE", f"/api/devenv/sessions/{s['id']}/ports/8080")
    _stop_all(page, server)
    assert not page.errors  # type: ignore[attr-defined]


def test_admin_opening_someone_elses_environment_confirms(page: Page, server: str, browser: Browser):
    """web-open: an admin opening bob's environment is asked first (and it is audited, W1-02)."""
    bob = browser.new_context()
    _call(bob, server, "POST", "/api/auth/login", {"username": "bob", "password": PASSWORD})
    rows = _call(bob, server, "GET", "/api/devenv/templates")
    templates = {t["builtin_key"]: t["id"] for t in rows if t["builtin_key"]}
    s = _call(bob, server, "POST", "/api/devenv/cpu/start", {"template_id": templates["jupyter-pytorch"]})
    try:
        sign_in(page, server, "admin")
        page.goto(f"{server}/devenv")
        page.locator("[data-test=my-environments] .toggle .n-switch").click()
        card = page.locator(f"[data-test=env-card][data-session='{s['id']}']")
        open_button = card.get_by_role("button", name="Open JupyterLab")
        expect(open_button).to_be_visible(timeout=20_000)
        with page.context.expect_page() as blank:
            open_button.click()
        blank.value.wait_for_load_state()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("This environment belongs to bob")
        with page.context.expect_page() as opened:
            dialog.get_by_role("button", name="Open as admin").click()
        opened.value.wait_for_load_state()
        audit = _call(page, server, "GET", "/api/admin/audit?action=devenv.open_as_admin")
        assert audit["total"] >= 1
    finally:
        _call(bob, server, "POST", f"/api/devenv/cpu/sessions/{s['id']}/stop")
        bob.close()


def test_shared_environment_join(page: Page, server: str):
    """A shared idle environment: Join without a key asks for one inline, then you are a member (W1-01 gap)."""
    sign_in(page, server, "admin")
    lanes, templates = _lanes(page, server), _templates(page, server)
    body = {"name": "Lane A", "gpus": [0], "idle_mode": "devenv", "idle_template_id": templates["pytorch"]}
    _call(page, server, "PATCH", f"/api/admin/lanes/{lanes['Lane A']}", body)
    try:
        page.context.clear_cookies()
        sign_in(page, server, "carol")
        page.goto(f"{server}/devenv")
        shared = page.locator("[data-test=shared-env]")
        expect(shared).to_be_visible(timeout=30_000)
        expect(shared.locator("[data-test=devenv-connect]")).to_have_count(0)
        shared.locator("[data-test=shared-join]").click()
        creds = shared.locator("[data-test=ssh-credentials]")
        expect(creds).to_be_visible()
        creds.locator("[data-test=ssh-key-input] textarea").fill(_ssh_key("carol@laptop"))
        creds.locator("[data-test=ssh-key-save]").click()
        expect(shared.get_by_text("joined", exact=True)).to_be_visible(timeout=10_000)
        expect(shared.locator("[data-test=shared-leave]")).to_be_visible()
        joined = [s for s in _call(page, server, "GET", "/api/devenv/sessions") if s["kind"] == "shared"]
        assert joined and joined[0]["joined"] is True
        shared.locator("[data-test=shared-leave]").click()
        page.get_by_role("dialog").get_by_role("button", name="Leave").click()
        expect(shared.locator("[data-test=shared-join]")).to_be_visible()
    finally:
        page.context.clear_cookies()
        sign_in(page, server, "admin")
        body = {"name": "Lane A", "gpus": [0], "idle_mode": "none"}
        _call(page, server, "PATCH", f"/api/admin/lanes/{lanes['Lane A']}", body)
    assert not page.errors  # type: ignore[attr-defined]


def test_mobile_chinese_card_fits(browser: Browser, server: str):
    """390 px, Chinese: the start card's Open is on the first screen; card buttons are not cut off (F03, F10)."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN")
    page = ctx.new_page()
    try:
        page.goto(f"{server}/login")
        page.evaluate("localStorage.setItem('slotserve.locale', 'zh-CN')")
        _call(page, server, "POST", "/api/auth/login", {"username": "carol", "password": PASSWORD})
        page.goto(f"{server}/devenv")
        open_button = page.locator("[data-test=start-open]")
        expect(open_button).to_have_text("打开")
        box = open_button.bounding_box()
        assert box and box["y"] + box["height"] <= 844
        templates = _templates(page, server)
        _call(page, server, "POST", "/api/devenv/cpu/start", {"template_id": templates["vscode"]})
        page.reload()
        card = page.locator("[data-test=env-card]").first
        expect(card.get_by_role("button", name="打开 VS Code")).to_be_visible(timeout=20_000)
        for button in card.locator("button").all():
            if button.is_visible():
                b = button.bounding_box()
                assert b and b["x"] >= 0 and b["x"] + b["width"] <= 390, button.inner_text()
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        _stop_all(page, server)
    finally:
        ctx.close()
