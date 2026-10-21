"""Personal computers: owners share their machine on a weekly schedule, pause sharing, and machines are woken up.

* **Sharing hours.** A node may have an owner (a SlotServe user) and a weekly schedule of shared windows in the
  server's time zone (``Node.sharing_schedule``; ``NULL`` = always shared). Bookings by others must lie inside
  shared windows; outside them the node's lanes run no default model and no shared dev environment. Bookings that
  already exist keep running (a schedule change that conflicts with them tells the owner and the people affected).
* **Pause** ("I'm using my PC"). From the moment the owner pauses, everyone with work on the machine is warned;
  after ``sharing_grace_minutes`` running bookings of others are ended (kept dev environments are paused, not
  deleted, like at the end of any booking) and the node's lanes run nothing until the pause ends.
* **Wake-on-LAN.** Shortly before a booking on an offline node starts (``wol_lead_minutes``), an online machine in
  the same local network (same ``network`` label or the same subnet) sends the magic packet (agent RPC
  ``wol.send``; the control plane's own machine can send it too). If the node is still offline when the booking
  starts, the booker and the owner are told.
* **Contribution.** Once a minute, owned nodes that are online add to ``node_share_daily`` (online, shared, and
  GPU-seconds offered); use by others and compute consumed come from the usage samples.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..errors import ApiError
from ..orm import BOOKING_KINDS, HOLDING_STATUSES, Lane, Node, NodeShareDaily, Reservation, UsageSample, User

log = logging.getLogger(__name__)

Window = tuple[datetime, datetime]
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_WINDOWS = 50
INTERVAL_SECONDS = 15
SAMPLE_SECONDS = 60
MAX_SAMPLE_GAP = 300  # longer gaps (restarts) are not guessed
ANNOUNCE_WINDOW = timedelta(hours=24)  # bookings told about an open-ended pause: those starting this soon
BROADCAST = "255.255.255.255"


# ---- the weekly schedule ----------------------------------------------------------------------------------


def tz_of(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def validate_schedule(value: Any) -> list[dict] | None:
    """``None`` (always shared) or a list of ``{"days": [0..6], "start": "HH:MM", "end": "HH:MM"}``.

    Monday is day 0. ``end`` may be ``24:00``; an end at or before the start runs into the next day
    (``23:00``-``09:00``). An empty list means "never shared".
    """
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_WINDOWS:
        raise ValueError("expected a list of weekly windows")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected a window object")
        days = item.get("days")
        if not isinstance(days, list) or not days:
            raise ValueError("a window needs at least one day")
        clean_days = sorted({int(d) for d in days if isinstance(d, int) and not isinstance(d, bool)})
        if len(clean_days) != len(set(days)) or any(d < 0 or d > 6 for d in clean_days):
            raise ValueError("days are numbers from 0 (Monday) to 6 (Sunday)")
        start, end = str(item.get("start", "")), str(item.get("end", ""))
        if not TIME_RE.match(start) or not (TIME_RE.match(end) or end == "24:00"):
            raise ValueError("times are HH:MM (end may be 24:00)")
        if _minutes(start) == _minutes(end) % (24 * 60) and end != "24:00":
            raise ValueError("a window cannot start and end at the same time (use 00:00-24:00 for a whole day)")
        out.append({"days": clean_days, "start": start, "end": end})
    return out


def load_schedule(raw: str | None) -> list[dict] | None:
    if raw is None:
        return None
    try:
        return validate_schedule(json.loads(raw))
    except (ValueError, TypeError):
        log.warning("ignoring an unreadable sharing schedule: %r", raw[:200])
        return None


def _merge(windows: list[Window]) -> list[Window]:
    out: list[Window] = []
    for s, e in sorted(windows):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _local(day: date, minutes: int, tz: ZoneInfo) -> datetime:
    # Wall-clock arithmetic: 24:00 is the next day's midnight, also across daylight saving changes.
    return (datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=minutes)).astimezone(UTC)


def shared_windows(schedule: list[dict] | None, tz: ZoneInfo, start: datetime, end: datetime) -> list[Window]:
    """The shared stretches of [start, end), in UTC, merged."""
    if end <= start:
        return []
    if schedule is None:
        return [(start, end)]
    found: list[Window] = []
    day = start.astimezone(tz).date() - timedelta(days=1)  # a window of the day before may run into today
    last = end.astimezone(tz).date()
    while day <= last:
        for w in schedule:
            if day.weekday() not in w["days"]:
                continue
            s_min, e_min = _minutes(w["start"]), _minutes(w["end"])
            s = _local(day, s_min, tz)
            e = _local(day, e_min if e_min > s_min else e_min + 24 * 60, tz)
            s, e = max(s, start), min(e, end)
            if e > s:
                found.append((s, e))
        day += timedelta(days=1)
    return _merge(found)


def gaps(windows: list[Window], start: datetime, end: datetime) -> list[Window]:
    out: list[Window] = []
    cursor = start
    for s, e in windows:
        if s > cursor:
            out.append((cursor, min(s, end)))
        cursor = max(cursor, e)
    if cursor < end:
        out.append((cursor, end))
    return out


def in_schedule(schedule: list[dict] | None, tz: ZoneInfo, at: datetime) -> bool:
    return bool(shared_windows(schedule, tz, at, at + timedelta(seconds=1)))


def uncovered(schedule: list[dict] | None, tz: ZoneInfo, start: datetime, end: datetime) -> list[Window]:
    """The parts of [start, end) outside the shared windows."""
    return gaps(shared_windows(schedule, tz, start, end), start, end)


def next_change(schedule: list[dict] | None, tz: ZoneInfo, now: datetime) -> datetime | None:
    """When the schedule switches between shared and not shared next (within two weeks)."""
    if schedule is None:
        return None
    horizon = now + timedelta(days=15)
    windows = shared_windows(schedule, tz, now, horizon)
    if windows and windows[0][0] <= now:
        return windows[0][1] if windows[0][1] < horizon else None
    return windows[0][0] if windows else None


# ---- one node's sharing state ---------------------------------------------------------------------------------


@dataclass
class Sharing:
    node: Node
    schedule: list[dict] | None

    @classmethod
    def of(cls, node: Node) -> Sharing:
        return cls(node, load_schedule(node.sharing_schedule))

    def paused(self, now: datetime) -> bool:
        n = self.node
        return n.sharing_paused_at is not None and (n.sharing_paused_until is None or n.sharing_paused_until > now)

    def stops_at(self, settings: Settings) -> datetime | None:
        """When the pause takes effect (the end of the grace period)."""
        if self.node.sharing_paused_at is None:
            return None
        return self.node.sharing_paused_at + timedelta(minutes=settings.sharing_grace_minutes)

    def stopped(self, settings: Settings, now: datetime) -> bool:
        stops = self.stops_at(settings)
        return self.paused(now) and stops is not None and now >= stops

    def pause_overlaps(self, start: datetime, end: datetime, now: datetime) -> bool:
        n = self.node
        if not self.paused(now):
            return False
        return start < (n.sharing_paused_until or datetime.max.replace(tzinfo=UTC)) and end > n.sharing_paused_at

    def shared_now(self, settings: Settings, now: datetime) -> bool:
        return not self.paused(now) and in_schedule(self.schedule, tz_of(settings), now)

    def blocked(self, settings: Settings, start: datetime, end: datetime, now: datetime) -> list[dict]:
        """Unavailable stretches of [start, end) for people other than the owner."""
        missing = uncovered(self.schedule, tz_of(settings), start, end)
        out = [{"start": s, "end": e, "reason": "unshared"} for s, e in missing]
        if self.paused(now):
            ps = max(self.node.sharing_paused_at, start)
            pe = min(self.node.sharing_paused_until or end, end)
            if pe > ps:
                out.append({"start": ps, "end": pe, "reason": "paused"})
        return sorted(out, key=lambda b: b["start"])


def summary(node: Node, settings: Settings, now: datetime, owner_name: str | None = None) -> dict:
    """The sharing fields shown on a node (owner, schedule, shared now, pause); safe for any node."""
    sh = Sharing.of(node)
    paused = sh.paused(now)
    return {
        "owner_id": node.owner_id,
        "owner_name": owner_name,
        "sharing_schedule": sh.schedule,
        "shared_now": sh.shared_now(settings, now),
        "sharing_paused": paused,
        "sharing_paused_until": node.sharing_paused_until if paused else None,
        "sharing_stops_at": sh.stops_at(settings) if paused else None,
        "sharing_next_change": None if paused else next_change(sh.schedule, tz_of(settings), now),
        "network": node.network,
        "mac_addresses": node.mac_addresses or "",
        "woken_at": node.woken_at,
    }


_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def _days_text(days: list[int]) -> str:
    """``[0, 1, 2, 3, 4]`` -> ``Mo-Fr``; ``[0, 2]`` -> ``Mo,We``."""
    runs: list[list[int]] = []
    for d in sorted(days):
        if runs and d == runs[-1][-1] + 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    return ",".join(_DAYS[r[0]] if len(r) == 1 else f"{_DAYS[r[0]]}-{_DAYS[r[-1]]}" for r in runs)


def schedule_text(schedule: list[dict] | None) -> str | None:
    """A compact weekly schedule (``Mo-Fr 19:00-08:00; Sa-Su 00:00-24:00``); None when always shared."""
    if schedule is None:
        return None
    if not schedule:
        return "-"
    return "; ".join(f"{_days_text(w['days'])} {w['start']}-{w['end']}" for w in schedule)


def machine_extras(settings: Settings):
    """Owner, sharing hours and pause for the Machines page (``cluster.machines.EXTRAS``)."""

    async def extras(db: AsyncSession, node_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not node_ids:
            return {}
        now = utcnow()
        rows = (
            await db.execute(
                select(Node, User.username).outerjoin(User, User.id == Node.owner_id).where(Node.id.in_(node_ids))
            )
        ).all()
        out: dict[int, dict[str, Any]] = {}
        for node, owner in rows:
            sh = Sharing.of(node)
            out[node.id] = {
                "owner": owner,
                "shared_hours": schedule_text(sh.schedule),
                "sharing_paused": sh.paused(now),
            }
        return out

    return extras


# ---- nodes and their lanes --------------------------------------------------------------------------------------


async def local_id(db: AsyncSession) -> int | None:
    return (await db.execute(select(Node.id).where(Node.is_local.is_(True)).limit(1))).scalar_one_or_none()


async def node_of_lane(db: AsyncSession, lane: Lane | None) -> Node | None:
    if lane is None:
        return None
    node_id = lane.node_id if lane.node_id is not None else await local_id(db)
    return await db.get(Node, node_id) if node_id is not None else None


async def lanes_of(db: AsyncSession, node: Node) -> list[Lane]:
    cond = Lane.node_id == node.id
    if node.is_local:
        cond = or_(cond, Lane.node_id.is_(None))
    return list((await db.execute(select(Lane).where(cond).order_by(Lane.position, Lane.id))).scalars())


async def lane_gate(db: AsyncSession, settings: Settings, lane: Lane | None, now: datetime) -> tuple[str, int | None]:
    """What a lane may run now: ``open``; ``closed`` (outside sharing hours: bookings only, no idle work);
    ``stopped`` (sharing paused and the grace period is over: only the owner's own bookings). Also the owner."""
    node = await node_of_lane(db, lane)
    if node is None:
        return "open", None
    sh = Sharing.of(node)
    if sh.paused(now):
        return ("stopped" if sh.stopped(settings, now) else "open"), node.owner_id
    if sh.schedule is not None and not in_schedule(sh.schedule, tz_of(settings), now):
        return "closed", node.owner_id
    return "open", node.owner_id


async def check_booking(db: AsyncSession, settings: Settings, lane: Lane, windows: list[Window], user_id: int) -> None:
    """Bookings by others must lie inside the node's sharing hours and outside a pause."""
    node = await node_of_lane(db, lane)
    if node is None or (node.owner_id is not None and node.owner_id == user_id):
        return
    sh = Sharing.of(node)
    now = utcnow()
    tz = tz_of(settings)
    for start, end in windows:
        if sh.pause_overlaps(start, end, now):
            until = node.sharing_paused_until
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "node_sharing_paused",
                node=node.name,
                until=until.isoformat() if until else None,
            )
        missing = uncovered(sh.schedule, tz, start, end)
        if missing:
            raise ApiError(
                status.HTTP_400_BAD_REQUEST,
                "outside_sharing_hours",
                node=node.name,
                start_at=missing[0][0].isoformat(),
                end_at=missing[0][1].isoformat(),
            )


async def schedule_conflicts(
    db: AsyncSession, settings: Settings, node: Node, schedule: list[dict] | None, now: datetime
) -> list[Reservation]:
    """Bookings by others that a new schedule would leave (partly) outside the sharing hours."""
    lane_ids = [lane.id for lane in await lanes_of(db, node)]
    if not lane_ids or schedule is None:
        return []
    rows = (
        await db.execute(
            select(Reservation)
            .where(
                Reservation.lane_id.in_(lane_ids),
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.end_at > now,
            )
            .order_by(Reservation.start_at)
        )
    ).scalars()
    tz = tz_of(settings)
    return [r for r in rows if r.user_id != node.owner_id and uncovered(schedule, tz, max(r.start_at, now), r.end_at)]


async def affected_by_pause(db: AsyncSession, node: Node, now: datetime) -> list[Reservation]:
    """Bookings of others that run now or start during the pause (within a day for an open-ended one)."""
    lane_ids = [lane.id for lane in await lanes_of(db, node)]
    if not lane_ids:
        return []
    until = node.sharing_paused_until or now + ANNOUNCE_WINDOW
    stmt = select(Reservation).where(
        Reservation.lane_id.in_(lane_ids),
        Reservation.status.in_(HOLDING_STATUSES),
        Reservation.end_at > now,
        Reservation.start_at < until,
    )
    if node.owner_id is not None:
        stmt = stmt.where(Reservation.user_id != node.owner_id)
    return list((await db.execute(stmt.order_by(Reservation.start_at))).scalars())


# ---- Wake-on-LAN ---------------------------------------------------------------------------------------------------


def local_info(settings: Settings) -> dict:
    """The control plane's own machine, as an agent would report it."""
    from ..cluster import system

    return system.machine_info(settings.agent_wol_mac, settings.agent_network, settings.agent_host_os)


def _info(node: Node, executor) -> dict:
    if executor is not None and getattr(executor, "info", None):
        return executor.info
    if node.inventory:
        with contextlib.suppress(ValueError):
            return json.loads(node.inventory).get("info") or {}
    return {}


def macs_of(node: Node, info: dict) -> list[str]:
    """Hand-entered MACs, then those passed to the agent, then the physical cards it sees."""
    from ..cluster import system

    out = system.parse_macs(node.mac_addresses)
    for mac in info.get("wol_macs") or []:
        if mac not in out:
            out.append(mac)
    for card in info.get("networks") or []:
        if card.get("physical") and card.get("mac") and card["mac"] not in out:
            out.append(card["mac"])
    return out


def lan_keys(node: Node, info: dict) -> set[str]:
    """What identifies the machine's local network: its label and the subnets of its physical cards."""
    keys = {k for k in (node.network or "", str(info.get("network") or "")) if k}
    keys |= {c["subnet"] for c in info.get("networks") or [] if c.get("physical") and c.get("subnet")}
    return keys


def broadcasts_of(info: dict) -> list[str]:
    found = [c["broadcast"] for c in info.get("networks") or [] if c.get("physical") and c.get("broadcast")]
    return [*dict.fromkeys(found), BROADCAST]


# ---- contribution ------------------------------------------------------------------------------------------------


async def contribution(
    db: AsyncSession, since: datetime, until: datetime, node_id: int | None = None
) -> tuple[list[dict], dict[int, dict]]:
    """Per person: machine-hours offered by the machines they own and compute they used; and per node."""
    local = await local_id(db)
    nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
    lane_node = {
        lid: (nid if nid is not None else local) for lid, nid in (await db.execute(select(Lane.id, Lane.node_id))).all()
    }
    names = dict((await db.execute(select(User.id, User.username))).all())
    people: dict[int, dict] = defaultdict(
        lambda: {
            "nodes": [],
            "online_hours": 0.0,
            "shared_hours": 0.0,
            "gpu_hours_shared": 0.0,
            "gpu_hours_used_by_others": 0.0,
            "gpu_hours_consumed": 0.0,
        }
    )
    per_node: dict[int, dict] = defaultdict(
        lambda: {
            "online_hours": 0.0,
            "shared_hours": 0.0,
            "gpu_hours_shared": 0.0,
            "gpu_hours_used_by_others": 0.0,
            "gpu_hours_used_by_owner": 0.0,
        }
    )
    stmt = select(
        NodeShareDaily.node_id,
        NodeShareDaily.owner_id,
        func.sum(NodeShareDaily.online_seconds),
        func.sum(NodeShareDaily.shared_seconds),
        func.sum(NodeShareDaily.gpu_shared_seconds),
    ).where(NodeShareDaily.day >= since.date().isoformat(), NodeShareDaily.day <= until.date().isoformat())
    if node_id is not None:
        stmt = stmt.where(NodeShareDaily.node_id == node_id)
    stmt = stmt.group_by(NodeShareDaily.node_id, NodeShareDaily.owner_id)
    for nid, owner, online, shared, gpu in (await db.execute(stmt)).all():
        for target in ([people[owner]] if owner is not None else []) + [per_node[nid]]:
            target["online_hours"] += (online or 0) / 3600
            target["shared_hours"] += (shared or 0) / 3600
            target["gpu_hours_shared"] += (gpu or 0) / 3600
        if owner is not None and nodes.get(nid) is not None and nodes[nid].name not in people[owner]["nodes"]:
            people[owner]["nodes"].append(nodes[nid].name)

    samples = (
        await db.execute(
            select(UsageSample.lane_id, UsageSample.user_id, func.sum(UsageSample.seconds))
            .where(
                UsageSample.start >= since,
                UsageSample.start < until,
                UsageSample.resource == "gpu",
                UsageSample.kind.in_(BOOKING_KINDS),
                UsageSample.user_id.is_not(None),
            )
            .group_by(UsageSample.lane_id, UsageSample.user_id)
        )
    ).all()
    for lane_id, uid, seconds in samples:
        hours = (seconds or 0) / 3600
        nid = lane_node.get(lane_id)
        node = nodes.get(nid) if nid is not None else None
        if node_id is None:
            people[uid]["gpu_hours_consumed"] += hours
        if node is None or (node_id is not None and node.id != node_id):
            continue
        if node.owner_id is not None and node.owner_id != uid:
            people[node.owner_id]["gpu_hours_used_by_others"] += hours
            per_node[node.id]["gpu_hours_used_by_others"] += hours
        elif node.owner_id is not None:
            per_node[node.id]["gpu_hours_used_by_owner"] += hours
    rows = []
    for uid, data in people.items():
        if uid not in names:
            continue
        rounded = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in data.items()}
        rows.append({"user_id": uid, "username": names[uid], **rounded})
    rows.sort(key=lambda r: (-r["gpu_hours_shared"], -r["shared_hours"], r["username"]))
    nodes_out = {nid: {k: round(v, 2) for k, v in d.items()} for nid, d in per_node.items()}
    return rows, nodes_out


# ---- the periodic job -------------------------------------------------------------------------------------------


class SharingService:
    """Ends pauses, enforces them after the grace period, wakes machines and samples contributions."""

    def __init__(self, app_state):
        self.state = app_state
        self.settings: Settings = app_state.settings
        self._wake = asyncio.Event()
        self._last_sample: datetime | None = None
        self._enforced: set[tuple[int, datetime]] = set()  # (node id, paused at) whose pause took effect
        self._lock = asyncio.Lock()  # one round at a time (the loop and explicit calls)

    def poke(self) -> None:
        self._wake.set()

    def _emit(self, event: str, **data: Any) -> None:
        notifier = getattr(self.state, "notifier", None)
        if notifier is not None:
            notifier.emit(event, **data)

    def fmt(self, dt: datetime | None) -> str:
        notifier = getattr(self.state, "notifier", None)
        if dt is None:
            return ""
        return notifier.fmt_time(dt) if notifier is not None else dt.isoformat()

    def _executor(self, node: Node):
        registry = getattr(self.state, "nodes", None)
        return registry.find(node.id) if registry is not None else None

    def online(self, node: Node) -> bool:
        if node.is_local:
            return True
        executor = self._executor(node)
        return executor is not None and executor.online

    def changed(self, lane_ids: list[int] | None = None) -> None:
        """Re-plan the lanes (and dev environments) after sharing changed."""
        lanes = getattr(self.state, "lanes", None)
        if lanes is not None:
            if lane_ids is None:
                lanes.poke()
            else:
                for lane_id in lane_ids:
                    lanes.poke(lane_id)
        devenv = getattr(self.state, "devenv", None)
        if devenv is not None:
            devenv.poke()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sharing job failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), INTERVAL_SECONDS)
            self._wake.clear()

    async def tick(self, now: datetime | None = None) -> None:
        async with self._lock:
            await self._tick(now or utcnow())

    async def _tick(self, now: datetime) -> None:
        async with SessionLocal() as db:
            nodes = list((await db.execute(select(Node))).scalars())
            for node in nodes:
                await self._pause_lifecycle(db, node, now)
            await db.commit()
            await self._wake_due(db, now)
            await self._asleep(db, now)
            await self._sample(db, nodes, now)
            await db.commit()

    # ---- pause ------------------------------------------------------------------------------------------

    async def announce_pause(self, db: AsyncSession, node: Node, now: datetime) -> int:
        """Tells everyone with work on the machine that sharing is paused (called when the owner pauses)."""
        sh = Sharing.of(node)
        stops = sh.stops_at(self.settings) or now
        affected = await affected_by_pause(db, node, now)
        for r in affected:
            if r.start_at <= stops:
                self._emit(
                    "sharing_paused_active", user_id=r.user_id, reservation_id=r.id, node=node.name, at=self.fmt(stops)
                )
            else:
                until = node.sharing_paused_until
                self._emit(
                    "sharing_paused_upcoming_until" if until else "sharing_paused_upcoming",
                    user_id=r.user_id,
                    reservation_id=r.id,
                    node=node.name,
                    until=self.fmt(until),
                )
        return len(affected)

    async def _pause_lifecycle(self, db: AsyncSession, node: Node, now: datetime) -> None:
        sh = Sharing.of(node)
        if node.sharing_paused_at is not None and not sh.paused(now):
            # The pause ran out.
            node.sharing_paused_at = node.sharing_paused_until = node.sharing_paused_by = None
            self.changed([lane.id for lane in await lanes_of(db, node)])
            return
        if not sh.stopped(self.settings, now):
            return
        lanes = await lanes_of(db, node)
        lane_ids = [lane.id for lane in lanes]
        if not lane_ids:
            return
        stmt = select(Reservation).where(
            Reservation.lane_id.in_(lane_ids),
            Reservation.status == "booked",
            Reservation.start_at <= now,
            Reservation.end_at > now,
        )
        if node.owner_id is not None:
            stmt = stmt.where(Reservation.user_id != node.owner_id)
        ended = list((await db.execute(stmt)).scalars())
        for r in ended:
            r.end_at = now
            r.released_at = now
        if ended:
            await db.flush()
            for r in ended:
                self._emit("booking_stopped_sharing", user_id=r.user_id, reservation_id=r.id, node=node.name)
            log.info("sharing of %s paused: ended %s booking(s)", node.name, len(ended))
        key = (node.id, node.sharing_paused_at)
        if ended or key not in self._enforced:
            self._enforced.add(key)
            self.changed(lane_ids)

    # ---- waking machines ---------------------------------------------------------------------------------

    async def wake(self, db: AsyncSession, node: Node) -> list[str]:
        """Has online machines of the same network send the magic packet; returns their names."""
        from ..cluster import protocol as p
        from ..cluster import system

        if node.is_local:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "node_is_local")
        info = local_info(self.settings) if node.is_local else _info(node, self._executor(node))
        macs = macs_of(node, info)
        if not macs:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "node_no_mac", node=node.name)
        keys = lan_keys(node, info)
        broadcasts = broadcasts_of(info)
        params = {"macs": macs, "broadcasts": broadcasts, "port": self.settings.wol_port}
        senders: list[str] = []
        for other in (await db.execute(select(Node).where(Node.id != node.id, Node.revoked_at.is_(None)))).scalars():
            if not self.online(other):
                continue
            other_info = local_info(self.settings) if other.is_local else _info(other, self._executor(other))
            if not keys or not keys & lan_keys(other, other_info):
                continue
            try:
                if other.is_local:
                    await asyncio.to_thread(system.send_magic, macs, broadcasts, self.settings.wol_port)
                else:
                    await self._executor(other).request(p.M_WOL_SEND, params, timeout=10)
                senders.append(other.name)
            except Exception as exc:  # noqa: BLE001 - try the next machine
                log.warning("waking %s through %s failed: %s", node.name, other.name, exc)
        if not senders:
            raise ApiError(status.HTTP_409_CONFLICT, "no_wake_relay", node=node.name)
        node.woken_at = utcnow()
        log.info("woke %s through %s", node.name, ", ".join(senders))
        return senders

    async def _bookings_on_offline_nodes(self, db: AsyncSession, stmt) -> list[tuple[Reservation, Node]]:
        out = []
        for r in (await db.execute(stmt)).scalars():
            node = await node_of_lane(db, await db.get(Lane, r.lane_id) if r.lane_id is not None else None)
            if node is None or node.is_local or node.revoked_at is not None or self.online(node):
                continue
            out.append((r, node))
        return out

    async def _wake_due(self, db: AsyncSession, now: datetime) -> None:
        from .housekeeper import once

        lead = self.settings.wol_lead_minutes
        if lead <= 0:
            return
        stmt = select(Reservation).where(
            Reservation.status == "booked",
            Reservation.start_at <= now + timedelta(minutes=lead),
            Reservation.end_at > now,
        )
        for r, node in await self._bookings_on_offline_nodes(db, stmt):
            if not macs_of(node, _info(node, self._executor(node))):
                continue
            if not await once(db, f"wol:{r.id}:{r.start_at.isoformat()}"):
                continue
            try:
                await self.wake(db, node)
            except ApiError as exc:
                log.warning("could not wake %s for booking #%s: %s", node.name, r.id, exc.code)
            await db.commit()

    async def _asleep(self, db: AsyncSession, now: datetime) -> None:
        from .housekeeper import once

        stmt = select(Reservation).where(
            Reservation.status == "booked",
            Reservation.start_at <= now,
            Reservation.start_at > now - timedelta(minutes=30),
            Reservation.end_at > now,
        )
        for r, node in await self._bookings_on_offline_nodes(db, stmt):
            if not await once(db, f"asleep:{r.id}:{r.start_at.isoformat()}"):
                continue
            self._emit("booking_node_asleep", user_id=r.user_id, reservation_id=r.id, node=node.name)
            if node.owner_id is not None and node.owner_id != r.user_id:
                booker = await db.get(User, r.user_id)
                self._emit(
                    "booking_node_asleep_owner",
                    user_id=node.owner_id,
                    reservation_id=r.id,
                    node=node.name,
                    user=booker.username if booker else "?",
                )
            await db.commit()

    # ---- contribution sampling ---------------------------------------------------------------------------

    async def _sample(self, db: AsyncSession, nodes: list[Node], now: datetime) -> None:
        last, elapsed = self._last_sample, 0.0
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < SAMPLE_SECONDS - 5:
                return
        self._last_sample = now
        if last is None or elapsed > MAX_SAMPLE_GAP:
            return
        seconds = round(elapsed)
        day = now.astimezone(UTC).date().isoformat()
        for node in nodes:
            if node.owner_id is None or node.revoked_at is not None or not self.online(node):
                continue
            shared = Sharing.of(node).shared_now(self.settings, now)
            gpus = sum(len(lane.devices or []) for lane in await lanes_of(db, node))
            row = (
                await db.execute(
                    select(NodeShareDaily).where(
                        NodeShareDaily.node_id == node.id,
                        NodeShareDaily.day == day,
                        NodeShareDaily.owner_id == node.owner_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = NodeShareDaily(node_id=node.id, owner_id=node.owner_id, day=day)
                row.online_seconds = row.shared_seconds = row.gpu_shared_seconds = 0
                db.add(row)
            row.online_seconds += seconds
            if shared:
                row.shared_seconds += seconds
                row.gpu_shared_seconds += seconds * gpus
