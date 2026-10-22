"""A light SlotServe app for security tests: no background tasks, environment under the test's control."""

import contextlib
from pathlib import Path

import httpx

from slotserve.config import get_settings
from slotserve.main import create_app

INVITE = "sec-invite"
PASSWORD = "secret123"

# the environment the app sees; values None are removed
BASE_ENV = {
    "SLOTSERVE_INVITE_CODE": INVITE,
    "SLOTSERVE_SECRET_KEY": None,
    "SLOTSERVE_PUBLIC_URL": None,
    "SLOTSERVE_DEVENV_WEB_PORT": "0",
    "SLOTSERVE_TELEGRAM_BOT_TOKEN": None,
}


@contextlib.asynccontextmanager
async def running_app(tmp_path: Path, monkeypatch, **env: str | None):
    """Run create_app() with ``tmp_path/data`` as data dir; keyword arguments override ``BASE_ENV``."""
    values = {
        "SLOTSERVE_DATA_DIR": str(tmp_path / "data"),
        "SLOTSERVE_MODELS_DIR": str(tmp_path / "models"),
        **BASE_ENV,
        **env,
    }
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # never pick up a developer's .env
    get_settings.cache_clear()
    app = create_app(start_background=False)
    try:
        async with app.router.lifespan_context(app):
            yield app
    finally:
        get_settings.cache_clear()


def client(app, *, csrf_header: bool = True, **kwargs) -> httpx.AsyncClient:
    headers = {"X-SlotServe": "1"} if csrf_header else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers, **kwargs)


async def register(c: httpx.AsyncClient, username: str, **extra) -> httpx.Response:
    return await c.post("/api/auth/register", json={"username": username, "password": PASSWORD, **extra})


async def register_admin(app, c: httpx.AsyncClient, username: str = "root") -> httpx.Response:
    r = await register(c, username, setup_code=app.state.setup_code.current())
    assert r.status_code == 201, r.text
    return r
