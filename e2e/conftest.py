"""End-to-end fixtures: a real SlotServe process with the mock llama-server, driven by Playwright.

Run from the repository root after building the frontend:

    cd frontend && npm run build && cd ..
    pip install -e "backend[dev]" -r e2e/requirements.txt && playwright install chromium
    pytest e2e

Set E2E_BROWSER_CHANNEL=msedge (or chrome) to use an installed browser instead of Playwright's Chromium.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
INVITE = "e2e-invite"
PASSWORD = "e2e-pass-123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("slotserve")
    models = tmp / "models"
    models.mkdir()
    for name in ("chat-small.gguf", "chat-large.gguf"):
        (models / name).write_bytes(b"GGUF" + b"\0" * 2048)
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
        "SLOTSERVE_DEVENV_WEB_PORT": str(_free_port()),  # the second listener (dev environment web interfaces)
        "SLOTSERVE_SCHEDULER_INTERVAL_SECONDS": "0.5",
        "MOCK_LOAD_SECONDS": "1",
        "MOCK_TOKEN_DELAY": "0",
    }
    log = (tmp / "server.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "slotserve"],
        cwd=tmp,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
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
    yield base
    if os.name == "nt":
        # Windows ends the server without running its shutdown, so its model servers would be left
        # behind; end the whole process tree instead.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        channel = os.environ.get("E2E_BROWSER_CHANNEL") or None
        b = p.chromium.launch(channel=channel, headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser):
    ctx = browser.new_context(viewport={"width": 1366, "height": 860}, locale="en-US")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def sign_in(page: Page, base: str, username: str, password: str = PASSWORD) -> None:
    page.goto(f"{base}/login")
    page.get_by_role("tab", name="Sign in").click()
    page.locator("input").nth(0).fill(username)
    page.locator("input").nth(1).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).last.click()
    page.wait_for_url(f"{base}/")
