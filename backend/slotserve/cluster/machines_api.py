"""The Machines page and machine selection: summaries, load history and "Pick for me" (for everyone)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Node, Reservation, User
from . import machines as m
from . import recommend as rec

router = APIRouter(prefix="/api/machines", tags=["machines"])
TZ = Query(default=0, ge=-840, le=840, description="Minutes east of UTC: which bookings are today's and tomorrow's")


class RecommendIn(BaseModel):
    gpu: bool | None = None  # True: needs a GPU lane; False: CPU only; None: either
    min_gpu_memory_mb: int = Field(default=0, ge=0)
    cores: float = Field(default=0.0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    model_id: int | None = None
    purpose: str = Field(default="any", pattern="^(booking|cpu_env|deploy|any)$")
    # booking: the lane must be free in this window (an edited booking does not count against itself)
    start_at: datetime | None = None
    end_at: datetime | None = None
    exclude_reservation_id: int | None = None
    tz_offset: int = Field(default=0, ge=-840, le=840)

    @model_validator(mode="after")
    def _window(self):
        if (self.start_at is None) != (self.end_at is None) or (self.start_at and self.end_at <= self.start_at):
            raise ValueError("start_at and end_at go together, end after start")
        return self


class CandidateOut(BaseModel):
    node_id: int
    node_name: str
    lane_id: int | None
    lane_name: str | None
    score: float
    fits: bool
    reasons: list[str]


class RecommendOut(BaseModel):
    best: CandidateOut | None
    machines: list[CandidateOut]  # the best option of every machine, best first
    options: list[CandidateOut]  # every option (one per GPU lane, one per machine for CPU work)


@router.get("", response_model=list[m.MachineOut])
async def list_machines(
    request: Request, tz_offset: int = TZ, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Every machine with its hardware, live load and what is free on it now."""
    return await m.summarize(db, request.app.state, user, tz_offset)


async def _node(db: AsyncSession, node_id: int) -> Node:
    node = await db.get(Node, node_id)
    if node is None or node.revoked_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return node


@router.get("/{node_id}", response_model=m.MachineOut)
async def get_machine(
    node_id: int,
    request: Request,
    tz_offset: int = TZ,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await _node(db, node_id)
    return (await m.summarize(db, request.app.state, user, tz_offset, [node_id]))[0]


@router.get("/{node_id}/history", response_model=m.MachineHistoryOut)
async def machine_history(
    node_id: int, request: Request, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Recent load (one point a minute) and the GPU utilization of the machine's lanes per hour."""
    return await m.history(db, request.app.state, await _node(db, node_id))


async def taken_lanes(db: AsyncSession, start: datetime, end: datetime, exclude: int | None = None) -> set[int]:
    """Lanes with a booking (or one awaiting approval) overlapping [start, end)."""
    stmt = select(Reservation.lane_id).where(
        Reservation.status.in_(HOLDING_STATUSES), Reservation.start_at < end, Reservation.end_at > start
    )
    if exclude is not None:
        stmt = stmt.where(Reservation.id != exclude)
    return {lane_id for lane_id in (await db.execute(stmt)).scalars() if lane_id is not None}


def _out(c: rec.Candidate) -> CandidateOut:
    return CandidateOut(**c.__dict__)


@router.post("/recommend", response_model=RecommendOut)
async def recommend(
    body: RecommendIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Pick for me: the machine (and lane) that fits the request best, and every other option with reasons."""
    machines = await m.summarize(db, request.app.state, user, body.tz_offset)
    need = rec.Need(
        gpu=body.gpu,
        min_gpu_memory_mb=body.min_gpu_memory_mb,
        cores=body.cores,
        memory_mb=body.memory_mb,
        model_id=body.model_id,
        purpose=body.purpose,
    )
    if body.purpose == "booking" and body.start_at is not None and body.end_at is not None:
        need.taken_lanes = await taken_lanes(db, body.start_at, body.end_at, body.exclude_reservation_id)
    best, options = rec.recommend(machines, need)
    return RecommendOut(
        best=_out(best) if best else None,
        machines=[_out(c) for c in rec.best_per_machine(options)],
        options=[_out(c) for c in options],
    )
