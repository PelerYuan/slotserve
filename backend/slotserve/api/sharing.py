"""Personal computers: what owners (and admins) do with their machine, availability for the booking calendar,
Wake-on-LAN and contribution statistics (services/sharing.py)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, DevSession, Lane, LlmModel, Node, Reservation, User
from ..runtime.scheduler import ManualOverride
from ..services import kv, sharing
from ..services.audit import record

router = APIRouter(tags=["sharing"])


class WindowIn(BaseModel):
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class SharingIn(BaseModel):
    # Omitted: unchanged; null: always shared; []: never shared (bookings by others impossible).
    schedule: list[WindowIn] | None = None
    network: str | None = Field(default=None, max_length=64)
    mac_addresses: str | None = Field(default=None, max_length=255)


class PauseIn(BaseModel):
    hours: float | None = Field(default=None, gt=0, le=24 * 14)  # None = until resumed


class StopIn(BaseModel):
    reservation_id: int | None = None
    lane_id: int | None = None  # stop what runs on the lane while nobody has it booked


class NodeSharingOut(BaseModel):
    id: int
    name: str
    is_local: bool
    online: bool
    platform: str | None = None
    owner_id: int | None
    owner_name: str | None
    sharing_schedule: list[dict] | None
    shared_now: bool
    sharing_paused: bool
    sharing_paused_until: datetime | None
    sharing_stops_at: datetime | None
    sharing_next_change: datetime | None
    network: str | None
    mac_addresses: str
    wol_macs: list[str]  # every MAC a magic packet goes to (hand-entered and reported)
    woken_at: datetime | None
    timezone: str
    grace_minutes: int


class ConflictOut(BaseModel):
    id: int
    username: str
    start_at: datetime
    end_at: datetime


class SharingSaveOut(BaseModel):
    node: NodeSharingOut
    conflicts: list[ConflictOut]


class PauseOut(BaseModel):
    node: NodeSharingOut
    affected: int


class RunningLane(BaseModel):
    id: int
    name: str
    gpus: list[int]
    state: str | None
    model_name: str | None
    reason: str | None


class RunningBooking(BaseModel):
    id: int
    user_id: int
    username: str
    start_at: datetime
    end_at: datetime
    lane_id: int | None
    active: bool
    kind: str  # model | exclusive | devenv
    model_name: str | None


class RunningSession(BaseModel):
    id: int
    kind: str
    username: str | None
    lane_id: int | None
    state: str
    reservation_id: int | None


class RunningOut(BaseModel):
    lanes: list[RunningLane]
    bookings: list[RunningBooking]
    sessions: list[RunningSession]


class LaneBlock(BaseModel):
    start: datetime
    end: datetime
    reason: str  # unshared | paused


class LaneAvailabilityOut(BaseModel):
    lane_id: int
    node_id: int | None
    owner_id: int | None  # the owner may book outside the blocks
    blocks: list[LaneBlock]


class WakeOut(BaseModel):
    senders: list[str]


class ContributionRow(BaseModel):
    user_id: int
    username: str
    nodes: list[str]
    online_hours: float
    shared_hours: float
    gpu_hours_shared: float
    gpu_hours_used_by_others: float
    gpu_hours_consumed: float


class ContributionOut(BaseModel):
    since: datetime
    rows: list[ContributionRow]


class NodeContributionOut(BaseModel):
    since: datetime
    online_hours: float = 0.0
    shared_hours: float = 0.0
    gpu_hours_shared: float = 0.0
    gpu_hours_used_by_others: float = 0.0
    gpu_hours_used_by_owner: float = 0.0


def _service(request: Request) -> sharing.SharingService:
    return request.app.state.sharing


async def _node(db: AsyncSession, node_id: int) -> Node:
    node = await db.get(Node, node_id)
    if node is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return node


async def _owned(db: AsyncSession, node_id: int, user: User) -> Node:
    """A node the caller may control: their own machine, or any machine for admins."""
    node = await _node(db, node_id)
    if not user.is_admin and node.owner_id != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_node_owner")
    return node


async def node_out(request: Request, db: AsyncSession, node: Node) -> NodeSharingOut:
    settings = get_settings()
    service = _service(request)
    owner = await db.get(User, node.owner_id) if node.owner_id is not None else None
    data = sharing.summary(node, settings, utcnow(), owner.username if owner else None)
    info = sharing._info(node, service._executor(node))
    return NodeSharingOut(
        id=node.id,
        name=node.name,
        is_local=node.is_local,
        online=service.online(node) and node.revoked_at is None,
        platform=(info.get("platform") or {}).get("label"),
        wol_macs=sharing.macs_of(node, info),
        timezone=settings.timezone,
        grace_minutes=settings.sharing_grace_minutes,
        **data,
    )


@router.get("/api/nodes/mine", response_model=list[NodeSharingOut])
async def my_nodes(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The machines the caller owns."""
    nodes = (await db.execute(select(Node).where(Node.owner_id == user.id).order_by(Node.name))).scalars()
    return [await node_out(request, db, n) for n in nodes]


@router.get("/api/nodes/{node_id}/sharing", response_model=NodeSharingOut)
async def get_sharing(
    node_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    return await node_out(request, db, await _owned(db, node_id, user))


@router.put("/api/nodes/{node_id}/sharing", response_model=SharingSaveOut)
async def set_sharing(
    node_id: int,
    body: SharingIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sets the sharing hours (and the Wake-on-LAN details). Bookings of others left outside the new hours stay,
    but their owners and the machine's owner are told; they are returned as ``conflicts``."""
    settings = get_settings()
    node = await _owned(db, node_id, user)
    fields = body.model_fields_set
    conflicts: list[Reservation] = []
    now = utcnow()
    if "schedule" in fields:
        raw = None if body.schedule is None else [w.model_dump() for w in body.schedule]
        try:
            schedule = sharing.validate_schedule(raw)
        except ValueError as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_schedule", reason=str(exc)) from exc
        before = sharing.load_schedule(node.sharing_schedule)
        node.sharing_schedule = None if schedule is None else json.dumps(schedule)
        if schedule != before:
            conflicts = await sharing.schedule_conflicts(db, settings, node, schedule, now)
    if "network" in fields:
        node.network = (body.network or "").strip() or None
    if "mac_addresses" in fields:
        from ..cluster import system

        text = (body.mac_addresses or "").strip()
        macs = system.parse_macs(text)
        if text and len(macs) != len([p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_mac")
        node.mac_addresses = ", ".join(macs) or None
    record(db, user, "node.sharing", node.name, schedule=node.sharing_schedule, network=node.network)
    await db.commit()
    service = _service(request)
    names = dict((await db.execute(select(User.id, User.username))).all())
    for r in conflicts:
        service._emit("sharing_conflict", user_id=r.user_id, reservation_id=r.id, node=node.name)
    if conflicts and node.owner_id is not None:
        service._emit("sharing_conflict_owner", user_id=node.owner_id, node=node.name, count=len(conflicts))
    service.changed([lane.id for lane in await sharing.lanes_of(db, node)])
    return SharingSaveOut(
        node=await node_out(request, db, node),
        conflicts=[
            ConflictOut(id=r.id, username=names.get(r.user_id, "?"), start_at=r.start_at, end_at=r.end_at)
            for r in conflicts
        ],
    )


@router.post("/api/nodes/{node_id}/pause", response_model=PauseOut)
async def pause_sharing(
    node_id: int,
    body: PauseIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """ "I'm using my PC": everyone with work on the machine is warned now; after the grace period their bookings
    are ended (kept dev environments are paused) and nothing runs until the pause ends."""
    node = await _owned(db, node_id, user)
    now = utcnow()
    service = _service(request)
    if not sharing.Sharing.of(node).paused(now):
        node.sharing_paused_at = now
    node.sharing_paused_until = now + timedelta(hours=body.hours) if body.hours else None
    node.sharing_paused_by = user.username
    record(db, user, "node.pause", node.name, until=node.sharing_paused_until)
    await db.commit()
    affected = await service.announce_pause(db, node, now)
    service.changed([lane.id for lane in await sharing.lanes_of(db, node)])
    service.poke()
    return PauseOut(node=await node_out(request, db, node), affected=affected)


@router.post("/api/nodes/{node_id}/resume", response_model=NodeSharingOut)
async def resume_sharing(
    node_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    node = await _owned(db, node_id, user)
    node.sharing_paused_at = node.sharing_paused_until = node.sharing_paused_by = None
    record(db, user, "node.resume", node.name)
    await db.commit()
    _service(request).changed([lane.id for lane in await sharing.lanes_of(db, node)])
    return await node_out(request, db, node)


async def _running(request: Request, db: AsyncSession, node: Node) -> RunningOut:
    lanes = await sharing.lanes_of(db, node)
    lane_ids = [lane.id for lane in lanes]
    manager = getattr(request.app.state, "lanes", None)
    out_lanes = []
    for lane in lanes:
        ctx = manager.contexts.get(lane.id) if manager is not None else None
        rt = ctx.runtime if ctx is not None else None
        target = ctx.scheduler.target if ctx is not None else None
        out_lanes.append(
            RunningLane(
                id=lane.id,
                name=lane.name,
                gpus=lane.devices or [],
                state=rt.state.value if rt is not None else None,
                model_name=rt.model_name if rt is not None else None,
                reason=target.reason if target is not None else None,
            )
        )
    now = utcnow()
    bookings: list[RunningBooking] = []
    sessions: list[RunningSession] = []
    if lane_ids:
        names = dict((await db.execute(select(User.id, User.username))).all())
        models = dict((await db.execute(select(LlmModel.id, LlmModel.name))).all())
        rows = (
            await db.execute(
                select(Reservation)
                .where(
                    Reservation.lane_id.in_(lane_ids),
                    Reservation.status.in_(HOLDING_STATUSES),
                    Reservation.end_at > now,
                    Reservation.start_at < now + timedelta(hours=24),
                )
                .order_by(Reservation.start_at)
            )
        ).scalars()
        for r in rows:
            devenv = r.devenv_template_id is not None or r.devenv_project_id is not None
            bookings.append(
                RunningBooking(
                    id=r.id,
                    user_id=r.user_id,
                    username=names.get(r.user_id, "?"),
                    start_at=r.start_at,
                    end_at=r.end_at,
                    lane_id=r.lane_id,
                    active=r.start_at <= now and r.status == "booked",
                    kind="devenv" if devenv else ("exclusive" if r.model_id is None else "model"),
                    model_name=models.get(r.model_id) if r.model_id is not None else None,
                )
            )
        from ..services.devenv import OPEN_STATES

        for s in (
            await db.execute(
                select(DevSession).where(DevSession.lane_id.in_(lane_ids), DevSession.state.in_(OPEN_STATES))
            )
        ).scalars():
            sessions.append(
                RunningSession(
                    id=s.id,
                    kind=s.kind or "booking",
                    username=names.get(s.user_id) if s.user_id is not None else None,
                    lane_id=s.lane_id,
                    state=s.state,
                    reservation_id=s.reservation_id,
                )
            )
    return RunningOut(lanes=out_lanes, bookings=bookings, sessions=sessions)


@router.get("/api/nodes/{node_id}/running", response_model=RunningOut)
async def running(
    node_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Everything on the machine: its lanes' model servers, bookings running now or within a day, environments."""
    return await _running(request, db, await _owned(db, node_id, user))


@router.post("/api/nodes/{node_id}/stop", response_model=RunningOut)
async def stop(
    node_id: int,
    body: StopIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stops a booking on the machine (ended if running, cancelled if not yet started; its owner is told), or
    what a lane runs while nobody has it booked (until the next booking)."""
    node = await _owned(db, node_id, user)
    lane_ids = {lane.id for lane in await sharing.lanes_of(db, node)}
    now = utcnow()
    service = _service(request)
    if body.reservation_id is not None:
        r = await db.get(Reservation, body.reservation_id)
        if r is None or r.lane_id not in lane_ids or r.status not in HOLDING_STATUSES or r.end_at <= now:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        if r.start_at <= now and r.status == "booked":
            r.end_at = r.released_at = now
        else:
            r.status = "cancelled"
        owner = await db.get(User, r.user_id)
        record(db, user, "node.stop_booking", f"#{r.id} {owner.username if owner else '?'}", node=node.name)
        await db.commit()
        if r.user_id != user.id:
            service._emit(
                "booking_stopped_owner", user_id=r.user_id, reservation_id=r.id, node=node.name, by=user.username
            )
        service.changed([r.lane_id])
        housekeeper = getattr(request.app.state, "housekeeper", None)
        if housekeeper is not None:
            housekeeper.poke()
    elif body.lane_id is not None:
        if body.lane_id not in lane_ids:
            raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
        ctx = request.app.state.lanes.contexts.get(body.lane_id)
        if ctx is not None:
            override = ManualOverride(model_id=None, preset_id=None, until=None, by=user.username, created_at=now)
            ctx.scheduler.set_manual(override)
            await kv.put(db, ctx.scheduler.manual_key, override.to_json())
        record(db, user, "node.stop_lane", str(body.lane_id), node=node.name)
        await db.commit()
        service.changed([body.lane_id])
    else:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "validation_error", field="reservation_id")
    return await _running(request, db, node)


@router.post("/api/nodes/{node_id}/wake", response_model=WakeOut)
async def wake(node_id: int, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Sends the Wake-on-LAN packet through an online machine in the same network. Allowed for the owner, admins
    and people with a booking on the machine that runs now or starts within the next hour."""
    node = await _node(db, node_id)
    if not user.is_admin and node.owner_id != user.id:
        lane_ids = [lane.id for lane in await sharing.lanes_of(db, node)]
        now = utcnow()
        mine = (
            await db.execute(
                select(Reservation.id)
                .where(
                    Reservation.user_id == user.id,
                    Reservation.lane_id.in_(lane_ids),
                    Reservation.status == "booked",
                    Reservation.end_at > now,
                    Reservation.start_at < now + timedelta(hours=1),
                )
                .limit(1)
            )
        ).first()
        if mine is None:
            raise ApiError(status.HTTP_403_FORBIDDEN, "not_node_owner")
    senders = await _service(request).wake(db, node)
    record(db, user, "node.wake", node.name, via=",".join(senders))
    await db.commit()
    return WakeOut(senders=senders)


@router.get("/api/lanes/unavailable", response_model=list[LaneAvailabilityOut])
async def unavailable(
    start: datetime,
    end: datetime,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per lane, the stretches of [start, end) its machine is not shared (outside sharing hours, or paused)."""
    from ..services.booking import aware

    settings = get_settings()
    start, end = aware(start), aware(end)
    if end <= start or end - start > timedelta(days=62):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "validation_error", field="end")
    now = utcnow()
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
    local = next((n.id for n in nodes.values() if n.is_local), None)
    out = []
    for lane in (await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars():
        node = nodes.get(lane.node_id if lane.node_id is not None else local)
        blocks = sharing.Sharing.of(node).blocked(settings, start, end, now) if node is not None else []
        out.append(
            LaneAvailabilityOut(
                lane_id=lane.id,
                node_id=node.id if node else None,
                owner_id=node.owner_id if node else None,
                blocks=[LaneBlock(**b) for b in blocks],
            )
        )
    return out


# ---- contribution ------------------------------------------------------------------------------------------


def _since(days: int) -> datetime:
    return (utcnow() - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("/api/usage/contribution", response_model=ContributionOut)
async def contribution(
    days: int = Query(default=30, ge=1, le=365), _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Per person: hours their machines were online and shared, GPU-hours offered and used by others, and the
    GPU-hours they used themselves (bookings, from the usage samples)."""
    since = _since(days)
    rows, _nodes = await sharing.contribution(db, since, utcnow())
    return ContributionOut(since=since, rows=[ContributionRow(**r) for r in rows])


@router.get("/api/usage/contribution/export")
async def contribution_csv(
    days: int = Query(default=30, ge=1, le=365), _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    since = _since(days)
    rows, _nodes = await sharing.contribution(db, since, utcnow())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "user",
            "machines",
            "online_hours",
            "shared_hours",
            "gpu_hours_shared",
            "gpu_hours_used_by_others",
            "gpu_hours_consumed",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["username"],
                " ".join(r["nodes"]),
                r["online_hours"],
                r["shared_hours"],
                r["gpu_hours_shared"],
                r["gpu_hours_used_by_others"],
                r["gpu_hours_consumed"],
            ]
        )
    return Response(
        "﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="slotserve-contribution-{utcnow():%Y%m%d}.csv"'},
    )


@router.get("/api/nodes/{node_id}/contribution", response_model=NodeContributionOut)
async def node_contribution(
    node_id: int,
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    node = await _node(db, node_id)
    since = _since(days)
    _rows, per_node = await sharing.contribution(db, since, utcnow(), node_id=node.id)
    return NodeContributionOut(since=since, **per_node.get(node.id, {}))
