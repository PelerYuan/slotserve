"""Shared database queries used by the API, gateway and scheduler."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm import Lane, LlmModel, ModelLoadEvent, ModelPreset, Reservation, User
from ..schemas import ModelOut, PresetOut, ReservationOut

LOAD_HISTORY = 5
LOAD_OVERHEAD_SECONDS = 15.0


def _model_source_exists(m: LlmModel) -> bool:
    """GGUF files must exist; other engines may name a local directory or a repository id."""
    path = Path(m.file_path)
    if (m.engine or "llama.cpp") == "llama.cpp":
        return path.is_file()
    return path.exists() or not path.is_absolute()


def as_utc(value: datetime | str) -> datetime:
    """Normalises a timestamp from an aggregate query (which may skip the column type) to aware UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def active_reservation(db: AsyncSession, now: datetime, lane_id: int | None = None) -> Reservation | None:
    """The running booking of a lane (``lane_id`` ``None``: of any lane)."""
    stmt = select(Reservation).where(
        Reservation.status == "booked", Reservation.start_at <= now, Reservation.end_at > now
    )
    if lane_id is not None:
        stmt = stmt.where(Reservation.lane_id == lane_id)
    return (await db.execute(stmt.order_by(Reservation.start_at).limit(1))).scalar_one_or_none()


async def next_reservation(db: AsyncSession, now: datetime, lane_id: int | None = None) -> Reservation | None:
    stmt = select(Reservation).where(Reservation.status == "booked", Reservation.start_at > now)
    if lane_id is not None:
        stmt = stmt.where(Reservation.lane_id == lane_id)
    return (await db.execute(stmt.order_by(Reservation.start_at).limit(1))).scalar_one_or_none()


async def lanes(db: AsyncSession) -> list[Lane]:
    return list((await db.execute(select(Lane).order_by(Lane.position, Lane.id))).scalars())


async def primary_lane(db: AsyncSession) -> Lane | None:
    return (await db.execute(select(Lane).order_by(Lane.position, Lane.id).limit(1))).scalar_one_or_none()


async def default_model(db: AsyncSession, lane_id: int | None = None) -> LlmModel | None:
    """The model a lane serves when nobody has it booked (``lane_id`` ``None``: the first lane)."""
    lane = await db.get(Lane, lane_id) if lane_id is not None else await primary_lane(db)
    if lane is None or lane.default_model_id is None:
        return None
    model = await db.get(LlmModel, lane.default_model_id)
    if model is None or model.status != "ready" or model.deleted_at is not None:
        return None
    return model


async def estimate_load_seconds(db: AsyncSession, model: LlmModel, settings: Settings) -> float:
    stmt = (
        select(ModelLoadEvent.started_at, ModelLoadEvent.finished_at)
        .where(ModelLoadEvent.model_id == model.id, ModelLoadEvent.success.is_(True))
        .order_by(ModelLoadEvent.finished_at.desc())
        .limit(LOAD_HISTORY)
    )
    rows = (await db.execute(stmt)).all()
    if rows:
        return sum((f - s).total_seconds() for s, f in rows) / len(rows)
    return model.size_bytes / (settings.disk_read_mbps * 1_000_000) + LOAD_OVERHEAD_SECONDS


def reservation_phase(r: Reservation, now: datetime) -> str:
    if r.status != "booked":
        return r.status  # pending | cancelled | rejected | expired
    if now < r.start_at:
        return "upcoming"
    if now < r.end_at:
        return "active"
    return "finished"


async def reservation_out(db: AsyncSession, rows: list[Reservation], now: datetime) -> list[ReservationOut]:
    if not rows:
        return []
    user_ids = {r.user_id for r in rows}
    model_ids = {r.model_id for r in rows if r.model_id is not None}
    preset_ids = {r.preset_id for r in rows if r.preset_id is not None}
    users = dict((await db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))).all())
    models = dict((await db.execute(select(LlmModel.id, LlmModel.name).where(LlmModel.id.in_(model_ids)))).all())
    lane_names = dict((await db.execute(select(Lane.id, Lane.name))).all())
    presets = (
        dict((await db.execute(select(ModelPreset.id, ModelPreset.name).where(ModelPreset.id.in_(preset_ids)))).all())
        if preset_ids
        else {}
    )
    return [
        ReservationOut(
            id=r.id,
            user_id=r.user_id,
            username=users.get(r.user_id, "?"),
            model_id=r.model_id,
            model_name=models.get(r.model_id, "?") if r.model_id is not None else None,
            preset_id=r.preset_id,
            preset_name=presets.get(r.preset_id) if r.preset_id is not None else None,
            exclusive=r.model_id is None,
            series_id=r.series_id,
            lane_id=r.lane_id,
            lane_name=lane_names.get(r.lane_id),
            start_at=r.start_at,
            end_at=r.end_at,
            status=r.status,
            phase=reservation_phase(r, now),
            note=r.note,
            review_note=r.review_note,
            reviewed_by=r.reviewed_by,
            released_at=r.released_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def model_out(
    db: AsyncSession, models: list[LlmModel], settings: Settings, loaded: set[int] | int | None
) -> list[ModelOut]:
    loaded_ids = loaded if isinstance(loaded, set) else ({loaded} if loaded is not None else set())
    lane_defaults: dict[int, list[int]] = {}
    for lane_id, model_id in (await db.execute(select(Lane.id, Lane.default_model_id))).all():
        if model_id is not None:
            lane_defaults.setdefault(model_id, []).append(lane_id)
    creator_ids = {m.created_by for m in models if m.created_by is not None}
    creators: dict[int, str] = {}
    if creator_ids:
        creators = dict((await db.execute(select(User.id, User.username).where(User.id.in_(creator_ids)))).all())
    from ..runtime.gpu import gpu_stats  # local import: runtime imports this module

    vram_bytes = sum(g.memory_total_mb for g in await gpu_stats()) * 1024 * 1024
    presets: dict[int, list[PresetOut]] = {}
    if models:
        rows = await db.execute(
            select(ModelPreset).where(ModelPreset.model_id.in_([m.id for m in models])).order_by(ModelPreset.name)
        )
        for p in rows.scalars():
            presets.setdefault(p.model_id, []).append(PresetOut.model_validate(p))
    out = []
    for m in models:
        path = Path(m.file_path)
        out.append(
            ModelOut(
                id=m.id,
                name=m.name,
                file_path=m.file_path,
                file_name=path.name,
                file_exists=_model_source_exists(m),
                size_bytes=m.size_bytes,
                status=m.status,
                is_default=m.id in lane_defaults,
                is_loaded=m.id in loaded_ids,
                args=m.args,
                description=m.description,
                est_load_seconds=round(await estimate_load_seconds(db, m, settings), 1),
                created_by=m.created_by,
                created_by_name=creators.get(m.created_by) if m.created_by else None,
                created_at=m.created_at,
                presets=presets.get(m.id, []),
                engine=m.engine or "llama.cpp",
                default_lane_ids=sorted(lane_defaults.get(m.id, [])),
                exceeds_vram=bool(vram_bytes) and m.size_bytes > vram_bytes * 0.95,
            )
        )
    return out
