"""``/api/health``: one summary for monitoring and for the admin's system health table (architecture A05).

The same shape as the cluster branch's endpoint (``status``, ``version``, ``problems[]``, ``nodes``, ``lanes``,
``downloads``, ``node_list``) plus ``checks[]``, the full results of the check registry (services/health.py);
there, node problems become checks when the branches merge. Reading needs the metrics token
(``Authorization: Bearer``) or an admin session; ``?strict=true`` answers 503 unless ``status`` is ok, i.e. while
any check that is not dismissed is at level error. Changing things (re-run, dismiss) needs an admin session.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..db import SessionLocal, get_db, utcnow
from ..deps import SESSION_USER_KEY, admin_user
from ..errors import ApiError
from ..orm import DownloadTask, User
from ..runtime.manager import RuntimeState
from ..security import constant_time_equals
from ..services.audit import record
from ..services.health import HealthMonitor

router = APIRouter(prefix="/api/health", tags=["health"])


class CheckActionOut(BaseModel):
    kind: str  # link (a page) | setting (a setting key) | retry (POST /api/health/checks/{id}/run) | docs
    target: str


class DismissedOut(BaseModel):
    by: int | None
    reason: str
    at: datetime


class CheckOut(BaseModel):
    id: str
    group: str  # system | disk | engine | gpu | devenv | storage | chat | backup | tasks | security | events
    level: str  # ok | info | warn | error
    title_key: str  # sentence key (frontend checks.<title_key>); title is it rendered for the viewer
    title: str
    params: dict[str, Any]
    evidence: str
    actions: list[CheckActionOut]
    dismissible: bool
    dismissed: DismissedOut | None
    diagnosis: dict | None
    onboarding: bool  # part of the setup checklist
    doc_anchor: str
    since: datetime | None  # when the check reached this level
    last_run: datetime | None
    alerted: bool  # admins were alerted and not yet told it recovered


class HealthOut(BaseModel):
    status: str  # ok | degraded (a check at error) | down (no lane serves, or the database fails)
    version: str
    problems: list[str]  # English one-liners of the errors (as before checks[] existed)
    checks: list[CheckOut]
    nodes: dict[str, int] = Field(default_factory=dict)  # by status (multi-machine builds)
    lanes: dict[str, int]  # by runtime state
    downloads: dict[str, int]  # queued / running / failed (last 24 hours)
    node_list: list[dict] = Field(default_factory=list)  # multi-machine builds


class DismissIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


async def _viewer(request: Request) -> tuple[bool, User | None]:
    """(allowed, admin): the metrics token or an admin session."""
    token = get_settings().metrics_token
    auth = request.headers.get("authorization", "")
    user_id = request.session.get(SESSION_USER_KEY) if "session" in request.scope else None
    user = None
    if user_id is not None:
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
        if user is not None and user.is_admin and user.disabled_at is None:
            return True, user
    if token and auth.lower().startswith("bearer ") and constant_time_equals(auth[7:].strip(), token):
        return True, None
    return False, None


def monitor_of(request: Request) -> HealthMonitor:
    return request.app.state.health


async def build(request: Request, locale: str = "en") -> HealthOut:
    monitor = monitor_of(request)
    if not monitor.results:
        await monitor.run_checks()
    await monitor.load()
    lanes: dict[str, int] = {}
    contexts = request.app.state.lanes.ordered()
    for ctx in contexts:
        state = ctx.runtime.snapshot().state
        lanes[state.value] = lanes.get(state.value, 0) + 1
    since = utcnow() - timedelta(hours=24)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(DownloadTask.status, func.count(DownloadTask.id))
                .where((DownloadTask.status.in_(("queued", "running"))) | (DownloadTask.finished_at >= since))
                .group_by(DownloadTask.status)
            )
        ).all()
    downloads = {"queued": 0, "running": 0, "failed": 0}
    for state, n in rows:
        if state in downloads:
            downloads[state] = n
    problems = monitor.problems()
    if downloads["failed"]:
        problems.append(f"{downloads['failed']} download(s) failed in the last 24 hours")
    serving = sum(n for s, n in lanes.items() if s != RuntimeState.FAILED.value)
    database = monitor.results.get("database")
    if (contexts and not serving) or (database is not None and database.level == "error"):
        level = "down"
    elif monitor.worst() == "error":
        level = "degraded"
    else:
        level = "ok"
    return HealthOut(
        status=level,
        version=__version__,
        problems=problems,
        checks=[CheckOut(**c) for c in monitor.items(locale)],
        lanes=lanes,
        downloads=downloads,
    )


@router.get("", response_model=HealthOut)
async def health(request: Request, strict: bool = Query(default=False)):
    """Health for monitoring and the admin UI. ``strict=true``: 503 unless everything is fine."""
    allowed, admin = await _viewer(request)
    if not allowed:
        return JSONResponse(
            {"detail": {"code": "unauthorized"}}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
    body = await build(request, (admin.locale if admin is not None else None) or "en")
    return JSONResponse(body.model_dump(mode="json"), status_code=503 if strict and body.status != "ok" else 200)


@router.post("/run", response_model=HealthOut)
async def run_all(request: Request, admin: User = Depends(admin_user)):
    """Runs every check now (the "check again" button)."""
    await monitor_of(request).run_checks(force=True)
    return await build(request, admin.locale or "en")


@router.post("/checks/{check_id}/run", response_model=CheckOut)
async def run_one(check_id: str, request: Request, admin: User = Depends(admin_user)):
    monitor = monitor_of(request)
    await monitor.run_checks(force=True, ids={check_id})
    return _one(monitor, check_id, admin)


@router.post("/checks/{check_id}/dismiss", response_model=CheckOut)
async def dismiss(
    check_id: str,
    body: DismissIn,
    request: Request,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Hides a dismissible check's current outcome (with a reason, audited) until its level changes."""
    monitor = monitor_of(request)
    if not monitor.results:
        await monitor.run_checks()
    result = monitor.results.get(check_id)
    if result is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if not result.dismissible:
        raise ApiError(status.HTTP_409_CONFLICT, "check_not_dismissible")
    try:
        await monitor.dismiss(check_id, admin.id, body.reason.strip())
    except KeyError:  # not recorded yet: record it, then dismiss
        await monitor.run_checks(force=True, ids={check_id})
        await monitor.dismiss(check_id, admin.id, body.reason.strip())
    record(db, admin, "health.dismiss", check_id, reason=body.reason.strip(), title_key=result.title_key)
    await db.commit()
    return _one(monitor, check_id, admin)


@router.delete("/checks/{check_id}/dismiss", response_model=CheckOut)
async def undismiss(
    check_id: str, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    monitor = monitor_of(request)
    if check_id not in monitor.results:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    try:
        await monitor.undismiss(check_id)
    except KeyError as exc:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found") from exc
    record(db, admin, "health.undismiss", check_id)
    await db.commit()
    return _one(monitor, check_id, admin)


def _one(monitor: HealthMonitor, check_id: str, admin: User) -> CheckOut:
    for item in monitor.items(admin.locale or "en"):
        if item["id"] == check_id:
            return CheckOut(**item)
    raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
