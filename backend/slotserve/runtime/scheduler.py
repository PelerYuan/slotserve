"""Decides which model should be loaded right now and drives the runtime towards it."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import Lane, LlmModel, ModelPreset, Reservation
from ..services import kv, queries
from .manager import FAIL_BACKOFF_SECONDS, LlamaRuntime, RuntimeState

log = logging.getLogger(__name__)

Spec = tuple[int, int | None]


class EventSink(Protocol):
    def emit(self, event: str, **data: Any) -> None: ...


@dataclass
class Target:
    model: LlmModel | None
    preset: ModelPreset | None
    est_load_seconds: float
    # reservation | exclusive | preload | preload-early | preload-exclusive | manual | fallback | default | idle
    # | idle-devenv (the lane's shared dev environment runs) | idle-none (the lane is set to run nothing)
    reason: str
    reservation: Reservation | None = None
    fallback_from: str | None = None

    @property
    def spec(self) -> Spec | None:
        return (self.model.id, self.preset.id if self.preset else None) if self.model else None


MANUAL_KEY = "manual_override"


def manual_key(lane_id: int | None) -> str:
    """kv key of a lane's manual override (lane 1 keeps the pre-lane key)."""
    return MANUAL_KEY if lane_id in (None, 1) else f"{MANUAL_KEY}:{lane_id}"


@dataclass
class ManualOverride:
    """An admin's "load this now" / "unload" request; ``model_id`` ``None`` means unload."""

    model_id: int | None
    preset_id: int | None
    until: datetime | None  # None = until the next booking starts
    by: str
    created_at: datetime

    def to_json(self) -> dict:
        return {
            "model_id": self.model_id,
            "preset_id": self.preset_id,
            "until": self.until.isoformat() if self.until else None,
            "by": self.by,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict) -> "ManualOverride":
        return cls(
            model_id=data.get("model_id"),
            preset_id=data.get("preset_id"),
            until=datetime.fromisoformat(data["until"]) if data.get("until") else None,
            by=data.get("by", "?"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


async def _model_and_preset(
    db: AsyncSession, model_id: int | None, preset_id: int | None
) -> tuple[LlmModel | None, ModelPreset | None]:
    model = await db.get(LlmModel, model_id) if model_id is not None else None
    if model is None or model.deleted_at is not None:
        return None, None
    preset = await db.get(ModelPreset, preset_id) if preset_id is not None else None
    if preset is not None and preset.model_id != model.id:
        preset = None
    return model, preset


async def compute_target(
    db: AsyncSession,
    settings: Settings,
    now: datetime,
    current: Spec | None,
    manual: ManualOverride | None = None,
    lane_id: int | None = None,
) -> Target:
    """Pure decision logic (see docs: "Scheduling and preloading")."""
    buffer = settings.preload_buffer_seconds

    active = await queries.active_reservation(db, now, lane_id)
    if active is not None:
        if active.model_id is None:
            return Target(None, None, 0.0, "exclusive", active)
        model, preset = await _model_and_preset(db, active.model_id, active.preset_id)
        if model is not None:
            est = await queries.estimate_load_seconds(db, model, settings)
            return Target(model, preset, est, "reservation", active)

    lane = await db.get(Lane, lane_id) if lane_id is not None else await queries.primary_lane(db)
    mode = lane.mode if lane is not None else "model"
    default = await queries.default_model(db, lane_id) if mode == "model" else None
    default_spec = (default.id, None) if default else None
    est_default = await queries.estimate_load_seconds(db, default, settings) if default else 0.0

    upcoming = await queries.next_reservation(db, now, lane_id)
    if upcoming is not None:
        gap = (upcoming.start_at - now).total_seconds()
        if upcoming.model_id is None:
            # Free the GPU a little early so the booker gets all of it from the first minute.
            if gap <= settings.stop_timeout_seconds + buffer:
                return Target(None, None, 0.0, "preload-exclusive", upcoming)
        else:
            model, preset = await _model_and_preset(db, upcoming.model_id, upcoming.preset_id)
            if model is not None:
                spec = (model.id, preset.id if preset else None)
                est_next = await queries.estimate_load_seconds(db, model, settings)
                lead = max(est_next + buffer, settings.preload_min_minutes * 60)
                if mode == "devenv":
                    lead += settings.stop_timeout_seconds  # the shared environment has to stop first
                if spec != default_spec:
                    if gap <= lead:
                        return Target(model, preset, est_next, "preload", upcoming)
                    # Switching back to the default would leave it usable only briefly before the next switch.
                    if current != default_spec and gap <= est_default + lead + buffer:
                        return Target(model, preset, est_next, "preload-early", upcoming)

    if manual is not None and (manual.until is None or manual.until > now):
        model, preset = await _model_and_preset(db, manual.model_id, manual.preset_id)
        est = await queries.estimate_load_seconds(db, model, settings) if model else 0.0
        return Target(model, preset, est, "manual")

    if mode != "model":
        return Target(None, None, 0.0, f"idle-{mode}")
    return Target(default, None, est_default, "default" if default else "idle")


class Scheduler:
    def __init__(
        self, runtime: LlamaRuntime, settings: Settings, events: EventSink | None = None, lane_id: int | None = None
    ):
        self.lane_id = lane_id
        self.runtime = runtime
        self.settings = settings
        self.events = events
        self.target: Target | None = None
        self.manual: ManualOverride | None = None
        self._wake = asyncio.Event()
        self._force_restart = False
        self._failures: dict[Spec, tuple[int, datetime]] = {}
        self._seen_failure: datetime | None = None
        self._fallback_notified: set[int] = set()
        self.last_reason: str | None = None
        # Whether something else (a lane's shared dev environment) still holds the GPUs; models wait for it.
        self.hold: Callable[[int | None], bool] | None = None

    def poke(self) -> None:
        """Re-evaluate immediately (after a reservation or model change)."""
        self._wake.set()

    def request_restart(self) -> None:
        self._force_restart = True
        self._wake.set()

    def set_manual(self, override: ManualOverride | None) -> None:
        self.manual = override
        self.poke()

    @property
    def manual_key(self) -> str:
        return manual_key(self.lane_id)

    async def restore_manual(self) -> None:
        """Picks up an admin's manual choice saved before a restart."""
        async with SessionLocal() as db:
            data = await kv.get(db, self.manual_key)
        if data:
            override = ManualOverride.from_json(data)
            if override.until is None or override.until > utcnow():
                self.manual = override

    def _emit(self, event: str, **data: Any) -> None:
        if self.events is not None:
            self.events.emit(event, **data)

    def _observe_runtime(self) -> None:
        """Learns from the runtime's last outcome: failed specs back off, successful ones are forgiven."""
        rt = self.runtime
        if rt.state == RuntimeState.FAILED and rt.spec and rt._failed_at and rt._failed_at != self._seen_failure:
            self._seen_failure = rt._failed_at
            count = self._failures.get(rt.spec, (0, rt._failed_at))[0] + 1
            self._failures[rt.spec] = (count, rt._failed_at)
            self._emit("load_failed", model=rt.model_name, preset=rt.preset_name, error=rt.error, lane_id=self.lane_id)
        elif rt.state == RuntimeState.READY and rt.spec in self._failures:
            del self._failures[rt.spec]

    async def _apply_fallback(self, db: AsyncSession, target: Target, now: datetime) -> Target:
        """While a booked model is backing off after a failed load, serve the default model instead."""
        if target.reason not in ("reservation", "preload", "preload-early", "manual") or target.spec is None:
            return target
        failure = self._failures.get(target.spec)
        if failure is None:
            return target
        count, failed_at = failure
        wait = FAIL_BACKOFF_SECONDS[min(count, len(FAIL_BACKOFF_SECONDS)) - 1]
        if now - failed_at >= timedelta(seconds=wait):
            return target  # time to try the booked model again
        default = await queries.default_model(db, self.lane_id)
        if default is None or (default.id, None) == target.spec:
            return target
        est = await queries.estimate_load_seconds(db, default, self.settings)
        res = target.reservation
        if res is not None and res.id not in self._fallback_notified:
            self._fallback_notified.add(res.id)
            self._emit(
                "fallback",
                reservation_id=res.id,
                user_id=res.user_id,
                model=target.model.name if target.model else None,
                default=default.name,
            )
        return Target(default, None, est, "fallback", res, fallback_from=target.model.name if target.model else None)

    async def tick(self) -> None:
        now = utcnow()
        self._observe_runtime()
        async with SessionLocal() as db:
            target = await compute_target(db, self.settings, now, self.runtime.spec, self.manual, self.lane_id)
            if self.manual is not None:
                expired = self.manual.until is not None and self.manual.until <= now
                booked = target.reason in ("reservation", "exclusive") and self.manual.until is None
                if expired or booked:
                    log.info("manual override by %s ended", self.manual.by)
                    self.manual = None
                    await kv.put(db, self.manual_key, None)
                    await db.commit()
            target = await self._apply_fallback(db, target, now)
        if target.reason != self.last_reason:
            log.info("scheduler target: %s (%s)", target.model.name if target.model else None, target.reason)
            self.last_reason = target.reason
        self.target = target
        if target.model is not None and self.hold is not None and self.hold(self.lane_id):
            return  # the shared dev environment is being stopped; load once it has let go of the GPUs
        force, self._force_restart = self._force_restart, False
        await self.runtime.ensure(target.model, target.est_load_seconds, force=force, preset=target.preset)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.settings.scheduler_interval_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
