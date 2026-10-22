import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from chat_mocks import FakeDingTalk, FakeFeishu, FakeFeishuHook, FakeQQ, FakeWebhooks, FakeWeCom
from docker_mock import FakeDocker
from tg_mock import FakeTelegram

from slotserve.config import get_settings
from slotserve.main import create_app
from slotserve.services.docker_api import DockerClient

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
        # no second listener for dev environment web interfaces (tests serve app.state.web_app themselves)
        "SLOTSERVE_DEVENV_WEB_PORT": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # never pick up a developer's .env
    get_settings.cache_clear()
    application = create_app()
    application.state.fake_telegram = FakeTelegram()
    application.state.telegram_transport = httpx.ASGITransport(app=application.state.fake_telegram.app)
    fakes = SimpleNamespace(
        feishu=FakeFeishu(), dingtalk=FakeDingTalk(), wecom=FakeWeCom(), qq=FakeQQ(), feishu_hook=FakeFeishuHook()
    )
    application.state.fake_chat = fakes
    application.state.chat_transports = {
        "feishu": httpx.ASGITransport(app=fakes.feishu.app),
        "dingtalk": httpx.ASGITransport(app=fakes.dingtalk.app),
        "wecom": httpx.ASGITransport(app=fakes.wecom.app),
        "qq": httpx.ASGITransport(app=fakes.qq.app),
        "webhooks": httpx.ASGITransport(app=FakeWebhooks(fakes.feishu_hook, fakes.dingtalk, fakes.wecom)),
    }
    application.state.fake_docker = FakeDocker()
    application.state.docker_client = DockerClient(
        "unix:///fake/docker.sock", transport=httpx.ASGITransport(app=application.state.fake_docker.app)
    )
    async with application.router.lifespan_context(application):
        yield application
    for fake in (fakes.feishu, fakes.dingtalk, fakes.qq):
        await fake.close()
    get_settings.cache_clear()


@pytest.fixture
async def make_client(app):
    clients: list[httpx.AsyncClient] = []

    async def factory(username: str | None = None, password: str = "secret123") -> httpx.AsyncClient:
        # X-SlotServe: like the frontend's API client (CsrfGuard, security/csrf.py)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-SlotServe": "1"}
        )
        clients.append(client)
        if username:
            body = {"username": username, "password": password, "invite_code": INVITE}
            # the first account is the admin and signs up with the setup code (security/setup_code.py)
            body["setup_code"] = app.state.setup_code.current() or ""
            r = await client.post("/api/auth/register", json=body)
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
