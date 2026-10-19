import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest
from tg_mock import FakeTelegram

from slotserve.config import get_settings
from slotserve.main import create_app

ROOT = Path(__file__).resolve().parents[2]
MOCK_SERVER = ROOT / "tools" / "mock_llama_server.py"
INVITE = "test-invite"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def app(tmp_path, monkeypatch):
    env = {
        "SLOTSERVE_DATA_DIR": str(tmp_path / "data"),
        "SLOTSERVE_MODELS_DIR": str(tmp_path / "models"),
        "SLOTSERVE_INVITE_CODE": INVITE,
        "SLOTSERVE_SECRET_KEY": "test-secret",
        "SLOTSERVE_LLAMA_SERVER_CMD": f'"{sys.executable}" "{MOCK_SERVER}"',
        "SLOTSERVE_LLAMA_PORT": str(_free_port()),
        "SLOTSERVE_SCHEDULER_INTERVAL_SECONDS": "0.3",
        "SLOTSERVE_STOP_TIMEOUT_SECONDS": "5",
        "MOCK_LOAD_SECONDS": "0.5",
        "MOCK_TOKEN_DELAY": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # never pick up a developer's .env
    get_settings.cache_clear()
    application = create_app()
    application.state.fake_telegram = FakeTelegram()
    application.state.telegram_transport = httpx.ASGITransport(app=application.state.fake_telegram.app)
    async with application.router.lifespan_context(application):
        yield application
    get_settings.cache_clear()


@pytest.fixture
async def make_client(app):
    clients: list[httpx.AsyncClient] = []

    async def factory(username: str | None = None, password: str = "secret123") -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        if username:
            r = await client.post(
                "/api/auth/register", json={"username": username, "password": password, "invite_code": INVITE}
            )
            assert r.status_code == 201, r.text
        return client

    yield factory
    for c in clients:
        await c.aclose()


@pytest.fixture
def models_dir(app) -> Path:
    return app.state.settings.models_dir


def make_gguf(models_dir: Path, name: str, size: int = 1024) -> Path:
    path = models_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + b"\0" * (size - 4))
    return path


async def wait_for_state(client: httpx.AsyncClient, state: str, model: str | None = None, timeout: float = 20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = (await client.get("/api/status")).json()["runtime"]
        if last["state"] == state and (model is None or last["model_name"] == model):
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"runtime never reached {state}/{model}; last={last}")
