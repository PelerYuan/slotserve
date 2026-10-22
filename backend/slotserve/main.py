import asyncio
import contextlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, db
from .api import (
    admin,
    auth,
    calendar,
    catalog,
    channels,
    devenv,
    devenv_cpu,
    devenv_ports,
    downloads,
    health,
    inbox,
    invites,
    keys,
    lanes,
    metrics,
    models,
    notifications,
    placement,
    playground,
    reservations,
    runtime_admin,
    status,
    storage,
    usage,
    waitlist,
)
from .config import Settings, get_settings
from .gateway import container_gw, devenv_web, devenv_web_app, proxy
from .gateway.devenv_web_auth import WebOriginGuard
from .runtime.lanes import LaneManager, ensure_default_lane
from .security import secret_key
from .security.csrf import SESSION_COOKIE, CsrfGuard
from .security.sessions import ServerSessionMiddleware
from .security.setup_code import SetupCode
from .services import app_settings, maintenance
from .services import health as health_service
from .services.catalog_deploy import Deployer
from .services.chat.hub import ChannelHub
from .services.devenv import DevEnvManager, seed_templates
from .services.downloader import Downloader
from .services.housekeeper import Housekeeper
from .services.notify import Notifier
from .services.ratelimit import AttemptLimiter
from .services.storage import StorageService
from .services.usage_sampler import UsageSampler

log = logging.getLogger("slotserve")
PACKAGE_STATIC = Path(__file__).parent / "static"
WEB_PATH = re.compile(r"devenv/\d+/port(/|$)")  # dev environment ports (gateway/devenv_web.py)


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.models_dir.mkdir(parents=True, exist_ok=True)
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
            # [security] first admin: the setup code (logged while no admin exists)
            await app.state.setup_code.refresh(session)
        if not auth.invite_code_usable(settings.invite_code):
            log.warning(
                "the invite code is not set (SLOTSERVE_INVITE_CODE): only the first admin can sign up until an "
                "admin sets one in Admin -> System settings"
            )
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
        app.state.deployer = Deployer(settings, app.state.lanes)
        app.state.downloader.deployer = app.state.deployer
        app.state.housekeeper = Housekeeper(app.state)
        # GPU metrics come from nvidia-smi unless a reader was injected (tests, other hardware).
        app.state.usage_sampler = UsageSampler(app.state, stats=getattr(app.state, "gpu_stats_fn", None))
        async with db.SessionLocal() as session:
            await seed_templates(session)
        app.state.devenv = DevEnvManager(app.state, docker=getattr(app.state, "docker_client", None))
        app.state.lanes.gpu_holder = app.state.devenv.holds_gpu
        # [F11] the container-side gateway: only /v1, for dev environments; started by the dev environment manager
        # once Docker answers (gateway/container_gw.py)
        app.state.container_gw = container_gw.ContainerGateway(app.state, serve=start_background)
        app.state.storage = StorageService(app.state)  # storage hook: Admin -> Storage checks
        # [web origin] dev environment web interfaces on their own port (gateway/devenv_web_app.py)
        app.state.web_app = devenv_web_app.create_web_app(app.state)
        web_listener = None
        if start_background and settings.devenv_web_port:
            web_listener = await devenv_web_app.start(app.state.web_app, settings.host, settings.devenv_web_port)
        transports = dict(getattr(app.state, "chat_transports", None) or {})
        transports.setdefault("telegram", getattr(app.state, "telegram_transport", None))
        app.state.channels = ChannelHub(app, transports)
        app.state.telegram = app.state.channels.telegram
        app.state.notifier.hub = app.state.channels
        if restored := await app.state.notifier.restore_pending():
            log.info("re-queued %s notification(s) saved at the last shutdown", restored)
        # [health] checks, alerts and the supervision of the background tasks below (services/health.py)
        health_service.reset_runtime_state()
        app.state.health = health_service.HealthMonitor(settings, app.state)
        if maintenance.restore_hold(settings):
            log.warning("restored from a backup: nothing unknown is removed until an admin has seen the report")
        supervised = health_service.supervised
        tasks = []
        if start_background:
            tasks = [
                asyncio.create_task(supervised("downloader", app.state.downloader.run), name="downloader"),
                asyncio.create_task(supervised("notifier", app.state.notifier.run), name="notifier"),
                asyncio.create_task(supervised("housekeeper", app.state.housekeeper.run), name="housekeeper"),
                *(
                    asyncio.create_task(supervised(f"chat-{c.id}", c.run), name=f"chat-{c.id}")
                    for c in app.state.channels
                ),
                asyncio.create_task(supervised("devenv", app.state.devenv.run), name="devenv"),
                asyncio.create_task(supervised("usage-sampler", app.state.usage_sampler.run), name="usage-sampler"),
                asyncio.create_task(supervised("storage", app.state.storage.run), name="storage"),  # storage hook
                asyncio.create_task(supervised("health", app.state.health.run), name="health"),
            ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            await devenv_web_app.stop(web_listener)
            await app.state.container_gw.close()
            await app.state.housekeeper.close()
            await app.state.notifier.save_pending()
            await app.state.lanes.shutdown()
            await app.state.channels.close()
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
    # [security] middleware, outermost last: CsrfGuard -> WebOriginGuard -> ServerSessionMiddleware (tests/security)
    secret_key.resolve(settings)  # an unset or default key is replaced by the generated one in DATA_DIR
    app.state.setup_code = SetupCode(settings.data_dir)
    # sessions live in the database; the cookie only holds a random token (security/sessions.py)
    app.add_middleware(ServerSessionMiddleware, session_cookie=SESSION_COOKIE, https_only=settings.cookie_secure)
    app.add_middleware(WebOriginGuard, settings=settings)  # [web origin] pages there cannot change things here
    app.add_middleware(
        CsrfGuard,
        settings=settings,
        exempt_prefixes=("/v1/",),  # API keys only, never the session cookie
        exempt_paths=(channels.WECOM_CALLBACK,),  # signed by WeCom
        no_header_prefixes=("/devenv/",),  # apps in dev environments (with DEVENV_WEB_PORT=0)
    )
    for module in (
        auth,
        notifications,
        inbox,
        health,
        keys,
        models,
        downloads,
        reservations,
        waitlist,
        status,
        usage,
        admin,
        calendar,
        channels,
        metrics,
        lanes,
        runtime_admin,
        devenv,
        catalog,
        invites,
        playground,
    ):
        app.include_router(module.router)
    app.include_router(devenv.keys_router)
    app.include_router(storage.router)  # storage hook
    app.include_router(storage.user_router)
    app.include_router(devenv_cpu.router)  # devenv_cpu: environments without a GPU
    app.include_router(devenv_ports.router)
    app.include_router(placement.router)  # QuickBook: where a booking can go
    app.include_router(proxy.router)
    app.include_router(devenv_web.router)
    app.include_router(devenv_web.main_router)  # web-open: links to web interfaces, admin confirmation (R1-03)

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
