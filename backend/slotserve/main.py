import asyncio
import contextlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, db
from .api import (
    admin,
    auth,
    calendar,
    devenv,
    downloads,
    keys,
    lanes,
    metrics,
    models,
    notifications,
    reservations,
    runtime_admin,
    status,
    usage,
    waitlist,
)
from .config import Settings, get_settings
from .gateway import devenv_web, proxy
from .runtime.lanes import LaneManager, ensure_default_lane
from .services import app_settings
from .services.devenv import DevEnvManager, seed_templates
from .services.downloader import Downloader
from .services.housekeeper import Housekeeper
from .services.notify import Notifier
from .services.ratelimit import AttemptLimiter
from .services.telegram import TelegramBot

log = logging.getLogger("slotserve")
PACKAGE_STATIC = Path(__file__).parent / "static"
WEB_PATH = re.compile(r"devenv/\d+/port(/|$)")  # dev environment ports (gateway/devenv_web.py)


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.models_dir.mkdir(parents=True, exist_ok=True)
        if settings.secret_key == "change-me" or settings.invite_code == "change-me":
            log.warning("SLOTSERVE_SECRET_KEY / SLOTSERVE_INVITE_CODE still use defaults - change them!")
        migrated_from = await asyncio.to_thread(
            db.prepare, settings.data_dir / "slotserve.db", settings.data_dir / "backups"
        )
        if migrated_from is not None:
            log.info("database upgraded from schema v%s (backup in %s)", migrated_from, settings.data_dir / "backups")
        db.configure(settings.database_url)
        await db.create_all()

        app.state.settings = settings
        app.state.settings_defaults = app_settings.snapshot_defaults(settings)
        async with db.SessionLocal() as session:
            await app_settings.load_overrides(session, settings)
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0))
        app.state.limiter = AttemptLimiter()
        app.state.notifier = Notifier(settings)
        await ensure_default_lane()
        app.state.lanes = LaneManager(settings, app.state.notifier)
        await app.state.lanes.start(run=start_background)
        # The first lane, for code paths that only ever deal with one GPU group.
        app.state.runtime = app.state.lanes.primary.runtime
        app.state.scheduler = app.state.lanes.primary.scheduler
        app.state.downloader = Downloader(settings)
        app.state.downloader.events = app.state.notifier
        app.state.housekeeper = Housekeeper(app.state)
        async with db.SessionLocal() as session:
            await seed_templates(session)
        app.state.devenv = DevEnvManager(app.state, docker=getattr(app.state, "docker_client", None))
        app.state.telegram = TelegramBot(app, transport=getattr(app.state, "telegram_transport", None))
        app.state.notifier.bot = app.state.telegram
        if restored := await app.state.notifier.restore_pending():
            log.info("re-queued %s notification(s) saved at the last shutdown", restored)
        tasks = []
        if start_background:
            tasks = [
                asyncio.create_task(app.state.downloader.run(), name="downloader"),
                asyncio.create_task(app.state.notifier.run(), name="notifier"),
                asyncio.create_task(app.state.housekeeper.run(), name="housekeeper"),
                asyncio.create_task(app.state.telegram.run(), name="telegram"),
                asyncio.create_task(app.state.devenv.run(), name="devenv"),
            ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await app.state.notifier.save_pending()
            await app.state.lanes.shutdown()
            await app.state.telegram.close()
            await app.state.devenv.close()
            await app.state.http.aclose()
            await db.dispose()

    app = FastAPI(
        title="SlotServe",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="slotserve_session",
        max_age=30 * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    for module in (
        auth,
        notifications,
        keys,
        models,
        downloads,
        reservations,
        waitlist,
        status,
        usage,
        admin,
        calendar,
        metrics,
        lanes,
        runtime_admin,
        devenv,
    ):
        app.include_router(module.router)
    app.include_router(devenv.keys_router)
    app.include_router(proxy.router)
    app.include_router(devenv_web.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    _mount_frontend(app, settings.static_dir or PACKAGE_STATIC)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"
    root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request):
        if full_path.startswith(("api/", "v1/")) or WEB_PATH.match(full_path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not index.is_file():
            return JSONResponse(
                {"detail": "Frontend not built. Run `npm run build` in frontend/ or use the Docker image."},
                status_code=404,
            )
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            headers = (
                {"cache-control": "public, max-age=31536000, immutable"} if "/assets/" in candidate.as_posix() else None
            )
            return FileResponse(candidate, headers=headers)
        return FileResponse(index, headers={"cache-control": "no-cache"})
