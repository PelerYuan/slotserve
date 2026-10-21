import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..cluster import inventory
from ..cluster import protocol as cluster_protocol
from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user, current_user, get_lanes
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Lane, LlmModel, ModelPreset, NodeModel, Reservation, User
from ..runtime.engines import split_args
from ..runtime.lanes import LaneManager
from ..schemas import ModelCreateIn, ModelOut, ModelUpdateIn, PresetIn, PresetOut, ScanResultOut
from ..services import booking, queries, scanner
from ..services.audit import record

router = APIRouter(prefix="/api/models", tags=["models"])
log = logging.getLogger(__name__)


async def _get_model(db: AsyncSession, model_id: int) -> LlmModel:
    model = await db.get(LlmModel, model_id)
    if model is None or model.deleted_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return model


def _check_owner(model: LlmModel, user: User) -> None:
    if not user.is_admin and model.created_by != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")


@router.get("", response_model=list[ModelOut])
async def list_models(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    models = (
        (
            await db.execute(
                select(LlmModel)
                .where(LlmModel.deleted_at.is_(None))
                .order_by(LlmModel.is_default.desc(), LlmModel.name)
            )
        )
        .scalars()
        .all()
    )
    return await queries.model_out(db, list(models), get_settings(), lanes.loaded_model_ids())


@router.post("/scan", response_model=ScanResultOut)
async def scan_models(
    node_id: int | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Registers new model files: on this machine and every connected node (or only on ``node_id``)."""
    settings = get_settings()
    nodes = lanes.nodes
    local_id = nodes.local.node_id
    added: list[str] = []
    skipped = 0
    if node_id is None or nodes.is_local(node_id):
        added, skipped = await scanner.scan(db, settings, user.id)
        if len(nodes.all()) > 1:
            await inventory.claim_local(db, await nodes.local.scan_models(), local_id)
            await db.commit()
    for executor in nodes.all():
        if executor.is_local or not executor.online or (node_id is not None and executor.node_id != node_id):
            continue
        try:
            files = await executor.scan_models()
        except cluster_protocol.RemoteError as exc:
            log.warning("scanning node %s failed: %s", executor.name, exc)
            continue
        more, known = await inventory.register_node_files(db, executor.node_id, files, settings, user.id, local_id)
        await db.commit()
        added += more
        skipped += known
    return ScanResultOut(added=added, skipped=skipped)


def _source(engine: str, path: str) -> tuple[str, int]:
    """Validates where a model comes from; returns (stored path, size in bytes)."""
    path = path.strip()
    local = Path(path)
    if engine == "llama.cpp":
        if not local.is_file() or not path.lower().endswith(".gguf"):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "model_path_invalid")
        return str(local.resolve()), scanner.total_size(local)
    if local.is_dir():
        size = sum(f.stat().st_size for f in local.rglob("*") if f.is_file())
        return str(local.resolve()), size
    return path, 0  # repository id (for example Qwen/Qwen3-8B) or a path the engine resolves itself


@router.post("", response_model=ModelOut, status_code=201)
async def create_model(
    body: ModelCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Registers a model by hand: a GGUF file for llama.cpp, or a directory / repository id for other engines."""
    taken = await db.execute(select(LlmModel.id).where(LlmModel.name == body.name))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "model_name_taken")
    try:
        split_args(body.args)
    except ValueError as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_args", reason=str(exc)) from exc
    remote = None if lanes.nodes.is_local(body.node_id) else lanes.nodes.find(body.node_id)
    if body.node_id is not None and not lanes.nodes.is_local(body.node_id) and remote is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "lane_node_invalid")
    if remote is not None:
        path, size = await _remote_source(remote, body.engine, body.path)
    else:
        path, size = _source(body.engine, body.path)
    model = LlmModel(
        name=body.name,
        file_path=path,
        size_bytes=size,
        status="ready",
        args=body.args.strip() or (get_settings().default_llama_args if body.engine == "llama.cpp" else ""),
        description=body.description,
        engine=None if body.engine == "llama.cpp" else body.engine,
        created_by=user.id,
    )
    db.add(model)
    await db.flush()
    if remote is not None:
        await inventory.add_row(db, model, remote.node_id, path, size, "manual", lanes.nodes.local.node_id)
    await db.commit()
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


async def _remote_source(executor, engine: str, path: str) -> tuple[str, int]:
    """Like ``_source``, checked by the node that has the files."""
    try:
        found = await executor.request(cluster_protocol.M_MODELS_STAT, {"path": path.strip(), "engine": engine})
    except cluster_protocol.RemoteError as exc:
        raise ApiError(status.HTTP_409_CONFLICT, "node_offline", node=executor.name) from exc
    if engine == "llama.cpp" and (found.get("kind") != "file" or not path.strip().lower().endswith(".gguf")):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_path_invalid")
    if found.get("exists"):
        return str(found["path"]), int(found.get("size_bytes") or 0)
    return path.strip(), 0  # repository id or a path the engine resolves itself


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int,
    body: ModelUpdateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    if body.name is not None and body.name != model.name:
        taken = await db.execute(select(LlmModel.id).where(LlmModel.name == body.name))
        if taken.first() is not None:
            raise ApiError(status.HTTP_409_CONFLICT, "model_name_taken")
        model.name = body.name
    if body.args is not None:
        try:
            split_args(body.args)
        except ValueError as exc:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_args", reason=str(exc)) from exc
        model.args = body.args.strip()
    if body.description is not None:
        model.description = body.description
    if body.engine is not None or body.path is not None:
        engine = body.engine or model.engine or "llama.cpp"
        model.file_path, model.size_bytes = _source(engine, body.path or model.file_path)
        model.engine = None if engine == "llama.cpp" else engine
    await db.commit()
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


async def sync_default_flags(db: AsyncSession) -> None:
    """Keeps ``models.is_default`` (used for sorting) in line with the lanes' default models."""
    ids = {mid for mid in (await db.execute(select(Lane.default_model_id))).scalars() if mid is not None}
    await db.execute(update(LlmModel).values(is_default=False))
    if ids:
        await db.execute(update(LlmModel).where(LlmModel.id.in_(ids)).values(is_default=True))


@router.post("/{model_id}/default", response_model=ModelOut)
async def set_default(
    model_id: int,
    lane_id: int | None = None,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Makes the model the one a lane serves when nobody has booked it (``lane_id`` omitted: the first lane)."""
    model = await _get_model(db, model_id)
    if model.status != "ready":
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")
    lane = await booking.resolve_lane(db, lane_id)
    await booking.check_model_on_lane(db, model, lane)
    lane.default_model_id = model.id
    await sync_default_flags(db)
    record(db, admin, "model.set_default", model.name, lane=lane.name)
    await db.commit()
    lanes.poke(lane.id)
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


@router.delete("/{model_id}/default", status_code=204)
async def clear_default(
    model_id: int,
    lane_id: int | None = None,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Stops serving the model by default (on one lane, or on every lane when ``lane_id`` is omitted)."""
    model = await _get_model(db, model_id)
    stmt = update(Lane).where(Lane.default_model_id == model.id)
    if lane_id is not None:
        stmt = stmt.where(Lane.id == lane_id)
    await db.execute(stmt.values(default_model_id=None))
    await sync_default_flags(db)
    record(db, admin, "model.clear_default", model.name)
    await db.commit()
    lanes.poke()


@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: int,
    delete_file: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    if model.id in lanes.loaded_model_ids():
        raise ApiError(status.HTTP_409_CONFLICT, "model_in_use")
    future = await db.execute(
        select(Reservation.id).where(
            Reservation.model_id == model.id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.end_at > utcnow(),
        )
    )
    if future.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "model_has_reservations")
    record(db, user, "model.delete", model.name, delete_file=delete_file)
    on_nodes = await _node_files(db, lanes, model) if delete_file else []
    past = await db.execute(select(Reservation.id).where(Reservation.model_id == model.id).limit(1))
    if past.first() is not None:
        # Keep usage history intact: soft-delete and free the name.
        model.deleted_at = utcnow()
        model.is_default = False
        await db.execute(update(Lane).where(Lane.default_model_id == model.id).values(default_model_id=None))
        model.name = f"{model.name}#deleted-{model.id}"
    else:
        await db.delete(model)
    if delete_file:
        path = Path(model.file_path)
        root = get_settings().models_dir.resolve()
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
    await db.commit()
    calls = [executor.request(cluster_protocol.M_MODELS_DELETE, {"path": path}) for executor, path in on_nodes]
    for result in await asyncio.gather(*calls, return_exceptions=True):
        if isinstance(result, Exception):
            log.warning("deleting a model file on a node failed: %s", result)


async def _node_files(db: AsyncSession, lanes: LaneManager, model: LlmModel) -> list:
    """(executor, path) of every connected node that has the model's files."""
    rows = (await db.execute(select(NodeModel).where(NodeModel.model_id == model.id))).scalars().all()
    out = []
    for row in rows:
        executor = lanes.nodes.find(row.node_id)
        if executor is not None and not executor.is_local and executor.online:
            out.append((executor, row.path))
    return out


# ---- launch-argument presets ------------------------------------------------


def _check_args(args: str) -> str:
    try:
        split_args(args)
    except ValueError as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_args", reason=str(exc)) from exc
    return args.strip()


async def _get_preset(db: AsyncSession, model: LlmModel, preset_id: int) -> ModelPreset:
    preset = await db.get(ModelPreset, preset_id)
    if preset is None or preset.model_id != model.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return preset


async def _name_free(db: AsyncSession, model: LlmModel, name: str, exclude: int | None = None) -> None:
    stmt = select(ModelPreset.id).where(ModelPreset.model_id == model.id, ModelPreset.name == name)
    if exclude is not None:
        stmt = stmt.where(ModelPreset.id != exclude)
    if (await db.execute(stmt)).first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "preset_name_taken")


@router.post("/{model_id}/presets", response_model=PresetOut, status_code=201)
async def create_preset(
    model_id: int, body: PresetIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    await _name_free(db, model, body.name.strip())
    preset = ModelPreset(model_id=model.id, name=body.name.strip(), args=_check_args(body.args))
    db.add(preset)
    await db.commit()
    return preset


@router.patch("/{model_id}/presets/{preset_id}", response_model=PresetOut)
async def update_preset(
    model_id: int,
    preset_id: int,
    body: PresetIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    preset = await _get_preset(db, model, preset_id)
    await _name_free(db, model, body.name.strip(), exclude=preset.id)
    preset.name, preset.args = body.name.strip(), _check_args(body.args)
    await db.commit()
    return preset


@router.delete("/{model_id}/presets/{preset_id}", status_code=204)
async def delete_preset(
    model_id: int, preset_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Bookings that used the preset fall back to the model's own arguments."""
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    preset = await _get_preset(db, model, preset_id)
    await db.delete(preset)
    await db.commit()
