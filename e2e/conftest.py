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
from playwright.sync_api import Browser, sync_playwright

from helpers import INVITE, PASSWORD, sign_in

ROOT = Path(__file__).resolve().parents[1]
SERVER_DATA: dict[str, Path] = {}  # base URL -> SLOTSERVE_DATA_DIR of that server

# The tests share one server and build on each other (the first sign-up becomes the admin and sets the default
# model; later tests add a second lane), so modules run in this order. Modules not listed run afterwards, in
# the usual order; new test files should not depend on anything but `test_setup.py`.
MODULE_ORDER = (
    "test_setup.py",
    "test_gateway.py",
    "test_schedule.py",
    "test_admin.py",
    "test_lanes.py",
    "test_usage.py",
    "test_channels.py",
    "test_devenv.py",
    "test_models.py",
)

# Tests import these from helpers.py; re-exported for tests written against the old conftest.
__all__ = ["INVITE", "PASSWORD", "sign_in"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    def rank(item: pytest.Item) -> int:
        name = item.path.name
        return MODULE_ORDER.index(name) if name in MODULE_ORDER else len(MODULE_ORDER)

    items.sort(key=rank)  # stable: keeps the order inside each module


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
    SERVER_DATA[base] = tmp / "data"
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
def setup_code(server) -> str:
    """The code for creating the first admin (what an operator reads from the log / `slotserve setup-code`)."""
    return (SERVER_DATA[server] / "setup_code").read_text().strip()


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
