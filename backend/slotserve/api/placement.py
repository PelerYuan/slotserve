"""Where a booking can go: ``POST /api/placement/fit`` (QuickBook's "automatic" choice and its "Change" list)."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..orm import User
from ..services import placement

router = APIRouter(prefix="/api/placement", tags=["placement"])


class FitIn(BaseModel):
    """What to book. The fields mean what they mean in ``POST /api/reservations``; ``kind`` says which apply."""

    kind: Literal["model", "exclusive", "devenv"]
    model_id: int | None = None
    preset_id: int | None = None
    devenv_template_id: int | None = None
    devenv_project_id: int | None = None
    devenv_project_name: str | None = Field(default=None, max_length=64)
    start_at: datetime
    end_at: datetime
    lane_id: int | None = None  # only judge this lane
    gpu: int | None = Field(default=None, ge=1)  # at least this many GPUs
    # Judged only with several machines (W4); accepted now so clients need not change later.
    gpu_mem_mb: int = Field(default=0, ge=0)
    cpu_cores: float = Field(default=0.0, ge=0)
    mem_mb: int = Field(default=0, ge=0)
    # Also look for the earliest window of the same length that fits when this one does not.
    suggest: bool = True

    @model_validator(mode="after")
    def _consistent(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.kind == "model" and self.model_id is None:
            raise ValueError("kind model needs model_id")
        if self.kind == "devenv" and self.devenv_template_id is None and self.devenv_project_id is None:
            raise ValueError("kind devenv needs devenv_template_id or devenv_project_id")
        return self


class ReasonOut(BaseModel):
    code: str
    params: dict[str, Any] = {}


class CandidateOut(BaseModel):
    node_id: int | None
    lane_id: int | None
    lane_name: str | None
    gpus: list[int] | None
    score: float
    fits: bool
    reasons_ok: list[ReasonOut]
    reasons_no: list[ReasonOut]
    free_until: datetime | None
    needs_approval: bool
    est_migration_bytes: int | None
    owner_id: int | None
    trust: str
    interruptions_7d: int


class SuggestionOut(BaseModel):
    start_at: datetime
    end_at: datetime
    candidate: CandidateOut


class FitOut(BaseModel):
    start_at: datetime  # the window as it would be booked (a start in the current slot becomes "now")
    end_at: datetime
    best: CandidateOut | None  # the automatic choice; None: nothing fits
    candidates: list[CandidateOut]  # every lane, fitting ones first, best first
    reasons_ok: list[ReasonOut]  # request-wide: quota_left, needs_approval
    reasons_no: list[ReasonOut]  # request-wide refusals (also in every candidate's reasons_no)
    needs_approval: bool
    next_free: SuggestionOut | None  # when nothing fits: the earliest window of the same length that does


def _reason(r: placement.Reason) -> ReasonOut:
    return ReasonOut(code=r.code, params=r.params)


def _candidate(c: placement.Candidate) -> CandidateOut:
    return CandidateOut(
        node_id=c.node_id,
        lane_id=c.lane_id,
        lane_name=c.lane_name,
        gpus=c.gpus,
        score=c.score,
        fits=c.fits,
        reasons_ok=[_reason(r) for r in c.reasons_ok],
        reasons_no=[_reason(r) for r in c.reasons_no],
        free_until=c.free_until,
        needs_approval=c.needs_approval,
        est_migration_bytes=c.est_migration_bytes,
        owner_id=c.owner_id,
        trust=c.trust,
        interruptions_7d=c.interruptions_7d,
    )


def request_of(body: FitIn, user: User) -> placement.Request:
    return placement.Request(
        user_id=user.id,
        kind=body.kind,
        model_id=body.model_id if body.kind == "model" else None,
        preset_id=body.preset_id if body.kind == "model" else None,
        devenv_template_id=body.devenv_template_id if body.kind == "devenv" else None,
        devenv_project_id=body.devenv_project_id if body.kind == "devenv" else None,
        devenv_project_name=body.devenv_project_name if body.kind == "devenv" else None,
        lane_id=body.lane_id,
        gpu=body.gpu,
        gpu_mem_mb=body.gpu_mem_mb,
        cpu_cores=body.cpu_cores,
        mem_mb=body.mem_mb,
    )


@router.post("/fit", response_model=FitOut)
async def fit(body: FitIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Where the booking would go and why, judged by the rules the booking itself is checked against: booking the
    ``best`` candidate (its ``lane_id`` and the returned window) for yourself is accepted. Nothing is written."""
    now = utcnow()
    req = request_of(body, user)
    result = await placement.evaluate(db, req, (body.start_at, body.end_at), now)
    suggestion = None
    if result.best is None and body.suggest and not _hopeless(result):
        found = await placement.next_free(db, req, (body.start_at, body.end_at), now)
        if found is not None:
            (s, e), cand = found
            suggestion = SuggestionOut(start_at=s, end_at=e, candidate=_candidate(cand))
    start, end = result.window or (body.start_at, body.end_at)
    best = result.best
    return FitOut(
        start_at=start,
        end_at=end,
        best=_candidate(best) if best else None,
        candidates=[_candidate(c) for c in result.candidates],
        reasons_ok=[_reason(r) for r in result.reasons_ok],
        reasons_no=[_reason(r) for r in result.reasons_no],
        needs_approval=result.needs_approval,
        next_free=suggestion,
    )


# Refusals another time cannot fix: looking for a later window would only repeat them.
_TIMELESS = {
    "not_found",
    "model_not_ready",
    "model_required",
    "preset_without_model",
    "devenv_disabled",
    "ssh_key_required",
    "ssh_login_required",
    "devenv_project_template",
    "devenv_project_limit",
    "reservation_too_long",
    "reservation_too_short",
    "reservation_too_far",
    "quota_upcoming",
}


def _hopeless(result: placement.Fit) -> bool:
    return any(r.code in _TIMELESS for r in result.reasons_no)
