"""Compute-centred usage figures: booked and used GPU-hours, utilization, memory and energy.

Booked GPU-hours come from the calendar (booked time x GPUs of the lane). Everything measured
(used time, utilization, memory, energy, occupancy by kind) comes from ``usage_samples`` rows of resource "gpu";
CPU core-hours come from rows of resource "cpu".
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import USAGE_KINDS, Lane, Node, Reservation, UsageSample
from ..schemas import UsageGpuRow, UsageKindRow, UsageLaneRow, UsageNodeRow, UsageSeries
from .usage_sampler import gpu_count


@dataclass
class Window:
    """``days`` ending now, cut into local-time buckets (6 hours up to a week, else days)."""

    days: int
    tz_offset: int  # minutes east of UTC
    now: datetime
    bucket_hours: int = 0
    first: int = 0  # bucket number (local epoch seconds // bucket seconds)
    count: int = 0

    def __post_init__(self):
        self.bucket_hours = self.bucket_hours or (6 if self.days <= 7 else 24)
        self.count = self.days * 24 // self.bucket_hours
        self.first = math.floor(self.local(self.now) / self.size) - self.count + 1

    @property
    def size(self) -> int:
        return self.bucket_hours * 3600

    def local(self, t: datetime) -> float:
        return t.timestamp() + self.tz_offset * 60

    @property
    def since(self) -> datetime:
        return datetime.fromtimestamp(self.first * self.size - self.tz_offset * 60, UTC)

    def labels(self) -> list[str]:
        return [
            datetime.fromtimestamp((self.first + i) * self.size, UTC).strftime("%Y-%m-%dT%H:%M")
            for i in range(self.count)
        ]

    def sql_bucket(self, column):
        """SQL expression: bucket position (0-based) of a timestamp column."""
        epoch = cast(func.strftime("%s", column), Integer)
        return (epoch + self.tz_offset * 60) / self.size - self.first

    def spread(self, start: datetime, end: datetime):
        """Yields (bucket position, seconds) for the part of [start, end) inside the window."""
        a, b = max(self.local(start), self.local(self.since)), min(self.local(end), self.local(self.now))
        while a < b:
            pos = math.floor(a / self.size)
            edge = min(b, (pos + 1) * self.size)
            if 0 <= pos - self.first < self.count:
                yield pos - self.first, edge - a
            a = edge


@dataclass
class Acc:
    """Sums of sampled rows."""

    seconds: float = 0.0
    metered: float = 0.0
    util: float = 0.0
    busy: float = 0.0
    wh: float = 0.0
    peak: int | None = None
    total: int | None = None
    by_kind: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def add(self, kind, sec, metered, util, busy, wh, peak, total) -> None:
        self.seconds += sec
        self.metered += metered
        self.util += util
        self.busy += busy
        self.wh += wh
        if peak is not None:
            self.peak = max(self.peak or 0, peak)
        if total is not None:
            self.total = max(self.total or 0, total)
        self.by_kind[kind] += sec

    @property
    def avg_util(self) -> float | None:
        return round(self.util / self.metered, 1) if self.metered else None

    @property
    def used_hours(self) -> float:
        return round(self.busy / 3600, 2)


def _h(seconds: float) -> float:
    return round(seconds / 3600, 2)


@dataclass
class Report:
    window: Window
    names: dict[int, str]
    booked: dict[int, float] = field(default_factory=lambda: defaultdict(float))  # user -> GPU-seconds
    calendar: dict[int, float] = field(default_factory=lambda: defaultdict(float))  # user -> booked seconds
    bookings: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    lane_booked: dict[int | None, float] = field(default_factory=lambda: defaultdict(float))
    person_series: dict[int, list[float]] = field(default_factory=dict)
    people: dict[int, Acc] = field(default_factory=lambda: defaultdict(Acc))
    lanes: dict[int | None, Acc] = field(default_factory=lambda: defaultdict(Acc))
    gpus: dict[tuple[int | None, int], Acc] = field(default_factory=lambda: defaultdict(Acc))
    nodes: dict[int | None, Acc] = field(default_factory=lambda: defaultdict(Acc))
    node_booked: dict[int | None, float] = field(default_factory=lambda: defaultdict(float))
    node_names: dict[int, str] = field(default_factory=dict)
    lane_node: dict[int, int | None] = field(default_factory=dict)  # lane -> its machine now
    gpu_node: dict[tuple[int | None, int], int | None] = field(default_factory=dict)  # where a GPU row ran
    kinds: dict[str, Acc] = field(default_factory=lambda: defaultdict(Acc))
    all: Acc = field(default_factory=Acc)
    booking_acc: Acc = field(default_factory=Acc)  # samples taken during bookings
    cpu_core_seconds: dict[int | None, float] = field(default_factory=lambda: defaultdict(float))
    kind_series: dict[str, list[float]] = field(default_factory=dict)
    lane_util: dict[int | None, list[list[float]]] = field(default_factory=dict)  # [util, metered] per bucket
    lane_names: dict[int, str] = field(default_factory=dict)
    lane_gpus: dict[int, int] = field(default_factory=dict)

    def lane_name(self, lane_id: int | None) -> str:
        return self.lane_names.get(lane_id, "?") if lane_id is not None else "?"

    def node_name(self, node_id: int | None) -> str | None:
        return self.node_names.get(node_id) if node_id is not None else None

    # ---- outputs ---------------------------------------------------------------

    def lane_rows(self) -> list[UsageLaneRow]:
        ids = [i for i in self.lane_names if i in self.lanes or i in self.lane_booked]
        ids += [i for i in self.lanes if i not in self.lane_names]
        return [
            UsageLaneRow(
                lane_id=i,
                lane_name=self.lane_name(i),
                node_id=self.lane_node.get(i),
                node_name=self.node_name(self.lane_node.get(i)),
                gpus=self.lane_gpus.get(i, 1),
                booked_gpu_hours=_h(self.lane_booked.get(i, 0.0)),
                used_gpu_hours=self.lanes[i].used_hours,
                avg_utilization=self.lanes[i].avg_util,
                energy_wh=round(self.lanes[i].wh, 1),
                peak_memory_mb=self.lanes[i].peak,
                occupancy={k: _h(v) for k, v in self.lanes[i].by_kind.items()},
            )
            for i in ids
        ]

    def gpu_rows(self) -> list[UsageGpuRow]:
        order = list(self.lane_names)
        keys = sorted(self.gpus, key=lambda k: (order.index(k[0]) if k[0] in order else len(order), k[1]))
        return [
            UsageGpuRow(
                lane_id=lane_id,
                lane_name=self.lane_name(lane_id),
                node_id=self.gpu_node.get((lane_id, idx)),
                node_name=self.node_name(self.gpu_node.get((lane_id, idx))),
                gpu_index=idx,
                gpu_hours=_h(a.seconds),
                used_gpu_hours=a.used_hours,
                avg_utilization=a.avg_util,
                energy_wh=round(a.wh, 1),
                peak_memory_mb=a.peak,
                memory_total_mb=a.total,
            )
            for (lane_id, idx), a in ((k, self.gpus[k]) for k in keys)
        ]

    def node_rows(self) -> list[UsageNodeRow]:
        ids = [i for i in self.node_names if i in self.nodes or i in self.node_booked or i in self.lane_node.values()]
        ids += [i for i in self.nodes if i not in ids]
        rows = []
        for i in ids:
            acc = self.nodes[i]
            lanes = [lane for lane, node in self.lane_node.items() if node == i]
            busy = sum(v for k, v in acc.by_kind.items() if k != "free")
            rows.append(
                UsageNodeRow(
                    node_id=i,
                    node_name=self.node_name(i) or "?",
                    lanes=len(lanes),
                    gpus=sum(self.lane_gpus.get(lane, 1) for lane in lanes),
                    booked_gpu_hours=_h(self.node_booked.get(i, 0.0)),
                    used_gpu_hours=acc.used_hours,
                    sampled_gpu_hours=_h(acc.seconds),
                    avg_utilization=acc.avg_util,
                    energy_wh=round(acc.wh, 1),
                    peak_memory_mb=acc.peak,
                    occupancy={k: _h(v) for k, v in acc.by_kind.items()},
                    occupied_pct=round(busy / acc.seconds * 100, 1) if acc.seconds else None,
                )
            )
        return rows

    def kind_rows(self) -> list[UsageKindRow]:
        return [
            UsageKindRow(
                kind=k,
                gpu_hours=_h(self.kinds[k].seconds),
                used_gpu_hours=self.kinds[k].used_hours,
                avg_utilization=self.kinds[k].avg_util,
                energy_wh=round(self.kinds[k].wh, 1),
            )
            for k in USAGE_KINDS
            if k in self.kinds
        ]

    def series_by_person(self) -> list[UsageSeries]:
        return [
            UsageSeries(key=str(uid), name=self.names.get(uid, "?"), values=[round(v / 3600, 3) for v in values])
            for uid, values in sorted(self.person_series.items(), key=lambda e: self.names.get(e[0], "?"))
        ]

    def series_by_kind(self) -> list[UsageSeries]:
        return [
            UsageSeries(key=k, name=k, values=[round(v / 3600, 3) for v in self.kind_series[k]])
            for k in USAGE_KINDS
            if k in self.kind_series
        ]

    def utilization_series(self) -> list[UsageSeries]:
        out = []
        for lane_id, cells in self.lane_util.items():
            if not any(m for _, m in cells):
                continue
            out.append(
                UsageSeries(
                    key=str(lane_id),
                    name=self.lane_name(lane_id),
                    values=[round(u / m, 1) if m else None for u, m in cells],
                )
            )
        order = list(self.lane_names)
        return sorted(out, key=lambda s: order.index(int(s.key)) if s.key.isdigit() and int(s.key) in order else 99)


async def build(
    db: AsyncSession,
    window: Window,
    names: dict[int, str],
    detected_gpus: int,
    user_id: int | None = None,
    lane_ids: set[int] | None = None,
    include_cpu: bool = True,
) -> Report:
    """``lane_ids``: only these lanes (one machine's); ``include_cpu``: count CPU-only environments."""
    rep = Report(window, names)
    lanes = list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())
    nodes = list((await db.execute(select(Node).order_by(Node.is_local.desc(), Node.name))).scalars())
    rep.node_names = {n.id: n.name for n in nodes}
    local_id = next((n.id for n in nodes if n.is_local), None)
    if lane_ids is not None:
        lanes = [lane for lane in lanes if lane.id in lane_ids]
    for lane in lanes:
        rep.lane_names[lane.id] = lane.name
        rep.lane_gpus[lane.id] = gpu_count(lane.devices, detected_gpus)
        rep.lane_node[lane.id] = lane.node_id or local_id
    since, now = window.since, window.now

    stmt = select(Reservation).where(
        Reservation.status == "booked", Reservation.start_at < now, Reservation.end_at > since
    )
    if user_id is not None:
        stmt = stmt.where(Reservation.user_id == user_id)
    if lane_ids is not None:
        stmt = stmt.where(Reservation.lane_id.in_(lane_ids))
    for r in (await db.execute(stmt)).scalars():
        n = rep.lane_gpus.get(r.lane_id, 1)
        series = rep.person_series.setdefault(r.user_id, [0.0] * window.count)
        total = 0.0
        for pos, sec in window.spread(r.start_at, r.end_at):
            series[pos] += sec * n
            total += sec * n
            rep.calendar[r.user_id] += sec
        rep.booked[r.user_id] += total
        rep.lane_booked[r.lane_id] += total
        rep.node_booked[rep.lane_node.get(r.lane_id, local_id)] += total
        rep.bookings[r.user_id] += 1

    bucket = window.sql_bucket(UsageSample.start)
    stmt = (
        select(
            bucket,
            UsageSample.node_id,
            UsageSample.lane_id,
            UsageSample.gpu_index,
            UsageSample.kind,
            UsageSample.user_id,
            func.sum(UsageSample.seconds),
            func.sum(UsageSample.metered_seconds),
            func.sum(UsageSample.util_seconds),
            func.sum(UsageSample.busy_seconds),
            func.sum(UsageSample.energy_wh),
            func.max(UsageSample.mem_peak_mb),
            func.max(UsageSample.mem_total_mb),
        )
        .where(UsageSample.start >= since, UsageSample.start < now, UsageSample.resource == "gpu")
        .group_by(
            bucket,
            UsageSample.node_id,
            UsageSample.lane_id,
            UsageSample.gpu_index,
            UsageSample.kind,
            UsageSample.user_id,
        )
    )
    if user_id is not None:
        stmt = stmt.where(UsageSample.user_id == user_id)
    if lane_ids is not None:
        stmt = stmt.where(UsageSample.lane_id.in_(lane_ids))
    rows = (await db.execute(stmt)).all()
    for pos, node_id, lane_id, gpu, kind, uid, sec, metered, util, busy, wh, peak, total in rows:
        vals = (kind, sec or 0, metered or 0, util or 0.0, busy or 0, wh or 0.0, peak, total)
        # Rows from before multiple machines carry no node: the lane's machine.
        node_id = node_id or rep.lane_node.get(lane_id, local_id)
        rep.all.add(*vals)
        rep.lanes[lane_id].add(*vals)
        rep.gpus[(lane_id, gpu)].add(*vals)
        rep.gpu_node.setdefault((lane_id, gpu), node_id)
        rep.nodes[node_id].add(*vals)
        rep.kinds[kind].add(*vals)
        if uid is not None:
            rep.people[uid].add(*vals)
            rep.booking_acc.add(*vals)
        pos = int(pos)
        if 0 <= pos < window.count:
            rep.kind_series.setdefault(kind, [0.0] * window.count)[pos] += sec or 0
            cells = rep.lane_util.setdefault(lane_id, [[0.0, 0.0] for _ in range(window.count)])
            cells[pos][0] += util or 0.0
            cells[pos][1] += metered or 0

    # Other resources (CPU-only environments) only contribute core-hours for now.
    stmt = (
        select(UsageSample.user_id, func.sum(UsageSample.cpu_core_seconds))
        .where(UsageSample.start >= since, UsageSample.start < now, UsageSample.resource == "cpu")
        .group_by(UsageSample.user_id)
    )
    if user_id is not None:
        stmt = stmt.where(UsageSample.user_id == user_id)
    if not include_cpu:
        return rep
    for uid, cores in (await db.execute(stmt)).all():
        rep.cpu_core_seconds[uid] += float(cores or 0)
    return rep
