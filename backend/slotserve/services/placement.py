"""Placement: where a booking can go, and whether it would be accepted (A03, single-machine M1 version).

``fit`` is the only place that decides "where": QuickBook's "automatic" choice, the lane list behind "Change" and
(later) the multi-machine picker all read it, and the browser never works out by itself what is free. On one
machine a candidate is a lane. ``fit`` applies exactly the rules a new booking is checked against when it is
created (``services.booking``: time window, model, dev environment, overlaps on the lane, kept environment in use,
per-person quota, approval), by calling the same functions; so a candidate it marks as fitting is accepted when it
is booked as offered (the property test in ``tests/unit/placement`` creates every fitting candidate).

The signature is the one of the full ledger (A03), so the multi-machine version (W4) replaces the body, not the
callers: ``Request`` already has the fields that only matter with several machines (``node_ids``, ``data_home``,
GPU memory, cores, memory); here they are accepted and not evaluated, except ``gpu`` (the lane must have at least
that many GPUs). ``Candidate.node_id`` is ``None`` on a single machine.

Nothing here writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import utcnow
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, DevProject, Lane, Reservation, User
from . import booking, policies

Kind = Literal["model", "exclusive", "devenv"]
Window = tuple[datetime, datetime]


@dataclass(frozen=True)
class Reason:
    """Why a candidate fits or does not: an i18n code with its parameters.

    Codes of reasons against are the API error codes the booking itself would answer with (``reservation_conflict``,
    ``quota_weekly`` ...), so a refusal reads the same in QuickBook and in the full booking dialog.
    """

    code: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Request:
    """What someone wants to book (A03 ``Request``)."""

    user_id: int
    kind: Kind = "exclusive"
    model_id: int | None = None
    preset_id: int | None = None
    devenv_template_id: int | None = None
    devenv_project_id: int | None = None  # continue this kept environment
    devenv_project_name: str | None = None  # keep the new environment under this name
    lane_id: int | None = None  # only this lane (None: every lane)
    gpu: int | None = None  # at least this many GPUs in the lane
    # Multi-machine fields (W4); accepted, not evaluated on one machine.
    gpu_mem_mb: int = 0
    cpu_cores: float = 0.0
    mem_mb: int = 0
    data_home: int | None = None
    node_ids: list[int] | None = None


@dataclass
class Candidate:
    """One place the request could go (A03 ``Candidate``); on one machine: a lane."""

    node_id: int | None
    lane_id: int | None
    lane_name: str | None
    gpus: list[int] | None  # the lane's GPU indices; None = every visible GPU
    score: float
    reasons_ok: list[Reason]
    reasons_no: list[Reason]
    free_until: datetime | None = None  # the next booking on this lane after the window; None = none ahead
    needs_approval: bool = False
    est_migration_bytes: int | None = None
    owner_id: int | None = None
    trust: str = "dedicated"
    interruptions_7d: int = 0

    @property
    def fits(self) -> bool:
        return not self.reasons_no


@dataclass
class Fit:
    """``fit`` plus what the caller needs to explain it: the window as it will be booked and the request-wide
    reasons (also contained in every candidate's own lists)."""

    window: Window | None
    candidates: list[Candidate]
    reasons_ok: list[Reason]
    reasons_no: list[Reason]
    needs_approval: bool

    @property
    def best(self) -> Candidate | None:
        return next((c for c in self.candidates if c.fits), None)


@dataclass(frozen=True)
class Limits:
    """The booking limits that apply to someone (F07 per-person overrides will be read here)."""

    exempt: bool  # admins: no quota, no approval
    quota_weekly_hours: int  # 0 = unlimited
    quota_upcoming: int  # 0 = unlimited
    max_reservation_hours: int
    max_advance_days: int
    approval_mode: str
    approval_hours: int


@dataclass
class Change:
    """A booking to try out without making it (``dry_run``), optionally under other limits than the current ones."""

    request: Request
    window: Window
    limits: dict[str, Any] | None = None  # setting name -> value to assume, e.g. {"quota_weekly_hours": 4}


@dataclass
class Impact:
    admitted: bool
    candidate: Candidate | None
    reasons_no: list[Reason]
    needs_approval: bool


def policy_for(user: User, settings: Settings | None = None) -> Limits:
    """M1: the global limits (admins exempt). Per-person temporary quotas (F07) plug in here."""
    s = settings or get_settings()
    return Limits(
        exempt=bool(user.is_admin),
        quota_weekly_hours=s.quota_weekly_hours,
        quota_upcoming=s.quota_upcoming,
        max_reservation_hours=s.max_reservation_hours,
        max_advance_days=s.max_advance_days,
        approval_mode=s.approval_mode,
        approval_hours=s.approval_hours,
    )


def _reason(err: ApiError) -> Reason:
    detail = err.detail if isinstance(err.detail, dict) else {"code": str(err.detail), "params": {}}
    return Reason(detail["code"], dict(detail.get("params") or {}))


async def _check(reasons: list[Reason], coro) -> Any:
    """Runs one booking rule; a refusal becomes a reason instead of an error."""
    try:
        return await coro
    except ApiError as err:
        reasons.append(_reason(err))
        return None


async def _weekly_hours_left(db: AsyncSession, s: Settings, user: User, start: datetime) -> float | None:
    """Hours of the weekly quota not booked yet in the week ``start`` is in (None: no weekly quota applies)."""
    if user.is_admin or not s.quota_weekly_hours:
        return None
    try:
        tz = ZoneInfo(s.timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    week = booking._week_start(start, tz)
    week_end = week + timedelta(days=7)
    used = 0.0
    rows = await db.execute(
        select(Reservation.start_at, Reservation.end_at).where(
            Reservation.user_id == user.id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.start_at < week_end,
            Reservation.end_at > week,
        )
    )
    for s_at, e_at in rows.all():
        s_at, e_at = booking.aware(s_at), booking.aware(e_at)
        used += (min(e_at, week_end) - max(s_at, week)).total_seconds() / 3600
    limit = s.quota_weekly_hours + policies.extra_hours(await policies.grants(db, user.id), week, week_end)
    return round(max(0.0, limit - used), 2)


async def _devenv_rules(db: AsyncSession, s: Settings, req: Request, owner: User, reasons: list[Reason]) -> None:
    """The dev environment checks of a new booking, without creating the kept environment it may ask for."""
    if req.devenv_project_id is not None:
        await _check(reasons, booking.resolve_project(db, s, owner, req.devenv_project_id, None, None))
        return
    template = await _check(reasons, booking.resolve_devenv(db, s, req.devenv_template_id, owner))
    if req.devenv_template_id is not None and template is None:
        return
    # booking.resolve_project would add the new kept environment to the session: check its rules only.
    if not req.devenv_project_name or not req.devenv_project_name.strip():
        return
    if template is None:
        reasons.append(Reason("devenv_project_template"))
        return
    count = (await db.execute(select(func.count(DevProject.id)).where(DevProject.owner_id == owner.id))).scalar_one()
    if count >= s.devenv_projects:
        reasons.append(Reason("devenv_project_limit", {"limit": s.devenv_projects}))


async def _free_until(db: AsyncSession, lane_id: int, after: datetime) -> datetime | None:
    nxt = (
        await db.execute(
            select(func.min(Reservation.start_at)).where(
                Reservation.lane_id == lane_id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.start_at >= after,
            )
        )
    ).scalar_one_or_none()
    return booking.aware(nxt) if nxt is not None else None


async def fit(
    db: AsyncSession,
    req: Request,
    window: Window | None,
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> list[Candidate]:
    """Every place the request could go in ``window``, the fitting ones first and the best of them first.

    ``window`` ``None`` means "now, for one hour". Use ``evaluate`` for the request-wide reasons and the window
    as it will be booked.
    """
    return (await evaluate(db, req, window, now, settings=settings)).candidates


async def evaluate(
    db: AsyncSession,
    req: Request,
    window: Window | None,
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> Fit:
    s = settings or get_settings()
    now = now or utcnow()
    owner = await db.get(User, req.user_id)
    if owner is None:
        raise ValueError(f"no user {req.user_id}")
    if window is None:
        window = (now, now + timedelta(hours=1))

    # ---- request-wide rules, in the order the booking checks them ------------------------------------------
    no: list[Reason] = []
    ok: list[Reason] = []
    try:
        start, end = booking.check_window(s, window[0], window[1], now)
    except ApiError as err:
        no.append(_reason(err))
        start, end = booking.aware(window[0]), booking.aware(window[1])
    windows = [(start, end)]
    if req.kind == "model" and req.model_id is None:
        no.append(Reason("model_required"))
    model_id = req.model_id if req.kind == "model" else None
    preset_id = req.preset_id if req.kind == "model" else None
    await _check(no, booking.resolve_model(db, model_id, preset_id))
    if req.kind == "devenv":
        if req.devenv_template_id is None and req.devenv_project_id is None:
            no.append(Reason("devenv_project_template"))
        else:
            await _devenv_rules(db, s, req, owner, no)
    project_id = req.devenv_project_id if req.kind == "devenv" else None
    await _check(no, booking.raise_if_project_busy(db, project_id, windows))
    await _check(no, booking.check_quota(db, s, owner, windows))
    approval = booking.needs_approval(s, owner, windows)
    if approval:
        ok.append(Reason("needs_approval"))
    left = await _weekly_hours_left(db, s, owner, start)
    if left is not None:
        # "you have 6 h left this week" - also shown when this booking does not fit into them
        ok.append(Reason("quota_left", {"hours": left, "limit": s.quota_weekly_hours}))

    # ---- one candidate per lane ------------------------------------------------------------------------------
    stmt = select(Lane).order_by(Lane.position, Lane.id)
    if req.lane_id is not None:
        stmt = stmt.where(Lane.id == req.lane_id)
    lanes = list((await db.execute(stmt)).scalars())
    if req.lane_id is not None and not lanes:
        no.append(Reason("not_found"))
    candidates: list[Candidate] = []
    for order, lane in enumerate(lanes):
        lane_no = list(no)
        lane_ok = list(ok)
        await _check(lane_no, booking.raise_if_conflicts(db, windows, lane_id=lane.id))
        devices = lane.devices
        if req.gpu and devices is not None and len(devices) < req.gpu:
            lane_no.append(Reason("gpu_count_short", {"need": req.gpu, "has": len(devices)}))
        free_until = await _free_until(db, lane.id, end)
        score = 50.0
        if not any(r.code == "reservation_conflict" for r in lane_no):
            if free_until is None:
                lane_ok.append(Reason("free_ahead"))
                score += 30.0
            else:
                lane_ok.append(Reason("free_until", {"until": free_until.isoformat()}))
                # the longer it stays free after the booking, the easier it is to extend
                score += 30.0 * min(1.0, (free_until - end) / timedelta(hours=8))
        if model_id is not None and lane.default_model_id == model_id:
            lane_ok.append(Reason("model_is_default"))
            score += 10.0
        score -= min(order, 9)  # stable preference for the lanes listed first
        candidates.append(
            Candidate(
                node_id=None,
                lane_id=lane.id,
                lane_name=lane.name,
                gpus=devices,
                score=round(max(0.0, min(100.0, score)), 1),
                reasons_ok=lane_ok,
                reasons_no=lane_no,
                free_until=free_until,
                needs_approval=approval,
            )
        )
    candidates.sort(key=lambda c: (not c.fits, -c.score))
    return Fit(window=(start, end), candidates=candidates, reasons_ok=ok, reasons_no=no, needs_approval=approval)


async def next_free(
    db: AsyncSession,
    req: Request,
    window: Window,
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> tuple[Window, Candidate] | None:
    """The earliest window of the same length, at or after the asked one, that some lane can take (checked
    with ``evaluate``, so it is accepted too); None when there is none within the booking horizon."""
    s = settings or get_settings()
    now = now or utcnow()
    start, end = booking.aware(window[0]), booking.aware(window[1])
    start = max(start, now)
    length = end - start
    if length <= timedelta(0):
        return None
    horizon = now + timedelta(days=s.max_advance_days)
    stmt = select(Lane.id).order_by(Lane.position, Lane.id)
    if req.lane_id is not None:
        stmt = stmt.where(Lane.id == req.lane_id)
    starts: set[datetime] = set()
    for lane_id in (await db.execute(stmt)).scalars():
        for gap_start, gap_end in await booking.free_windows(db, lane_id, start, horizon + length):
            if gap_end - gap_start >= length:
                starts.add(gap_start)
    for candidate_start in sorted(starts)[:8]:
        if candidate_start > horizon:
            break
        result = await evaluate(db, req, (candidate_start, candidate_start + length), now, settings=s)
        if result.best is not None and result.window is not None:
            return result.window, result.best
    return None


async def dry_run(db: AsyncSession, change: Change, now: datetime | None = None) -> Impact:
    """Would this booking be accepted (under ``change.limits`` when given)? Nothing is written.

    The admission half of A03 ``dry_run``; the settings preview (W1-03) can call it with the limits it is about to
    save to see whether a booking would still go through.
    """
    s = get_settings()
    if change.limits:
        s = s.model_copy(update=change.limits)
    result = await evaluate(db, change.request, change.window, now, settings=s)
    best = result.best
    return Impact(
        admitted=best is not None,
        candidate=best,
        reasons_no=[] if best else _merged_no(result),
        needs_approval=result.needs_approval,
    )


def _merged_no(result: Fit) -> list[Reason]:
    seen: list[Reason] = []
    for c in result.candidates:
        for r in c.reasons_no:
            if r not in seen:
                seen.append(r)
    return seen or list(result.reasons_no)
