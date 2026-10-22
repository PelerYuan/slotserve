from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user, current_user, get_lanes
from ..errors import ApiError
from ..orm import HOLDING_STATUSES, Lane, LlmModel, ModelPreset, Reservation, User
from ..runtime.engines import ENGINES, ArgReview, member_source_ok, review_args, split_args
from ..runtime.lanes import LaneManager
from ..schemas import (
    ModelArgsReviewIn,
    ModelCreateIn,
    ModelOut,
    ModelUpdateIn,
    PresetIn,
    PresetOut,
    ScanResultOut,
)
from ..services import booking, queries, scanner
from ..services.audit import record

router = APIRouter(prefix="/api/models", tags=["models"])


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
async def scan_models(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    added, skipped = await scanner.scan(db, get_settings(), user.id)
    return ScanResultOut(added=added, skipped=skipped)


# ---- launch-argument governance -------------------------------------------------------
#
# Engines run inside the SlotServe process's privileges, so launch arguments and model paths are a way to run
# code there. Members may use the tuning flags of runtime/engines.py; anything else is either refused (400
# ``args_need_approval`` with ``needs_approval: [flags]``) or, when they ask for it, stored on the model as
# ``args_pending`` until an admin approves. Admins may set anything, but flags that run code or write files
# (``denied``) need ``confirm_args`` (400 ``args_confirm_required`` with ``denied: [flags]`` otherwise).


def _require_recent_auth(admin: User) -> None:
    """Hook: an admin changing launch arguments must have re-entered their password recently.

    TODO(W0-01): replace with the ``sudo_user`` dependency (re-authentication, 5 minutes) once it is merged:
    make the endpoints below that call this take ``Depends(sudo_user)`` when the arguments change.
    """
    return None


async def _review(db: AsyncSession, engine: str, args: str, approved: list[str]) -> ArgReview:
    try:
        split_args(args)
    except ValueError as exc:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_args", reason=str(exc)) from exc
    roots, excluded = await queries.model_roots(db, get_settings())
    return review_args(ENGINES[engine], args, roots, approved, excluded)


def _gate(user: User, review: ArgReview, confirm: bool, can_request: bool, request_approval: bool) -> bool:
    """True: the arguments may be used now. False: store them as a request for an admin. Raises otherwise."""
    if user.is_admin:
        if review.denied and not confirm:
            raise ApiError(status.HTTP_400_BAD_REQUEST, "args_confirm_required", denied=review.denied)
        _require_recent_auth(user)
        return True
    if not review.needs_approval:
        return True
    if can_request and request_approval:
        return False
    raise ApiError(
        status.HTTP_400_BAD_REQUEST,
        "args_need_approval",
        needs_approval=review.needs_approval,
        can_request=can_request,
    )


def _check_engine(user: User, engine: str) -> None:
    if engine == "custom" and not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "custom_engine_admin_only")


async def _check_member_path(db: AsyncSession, user: User, path: str) -> None:
    """Members may only point models at a model library or a Hugging Face repository id."""
    if user.is_admin:
        return
    roots, excluded = await queries.model_roots(db, get_settings())
    if not member_source_ok(path, roots, excluded):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_path_outside_library")


def _ask_admins(request: Request, user: User, model: LlmModel, review: ArgReview) -> None:
    notifier = getattr(request.app.state, "notifier", None)
    if notifier is not None:
        notifier.emit(
            "args_approval_requested", user=user.username, model=model.name, flags=" ".join(review.needs_approval)
        )


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
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Registers a model by hand: a GGUF file for llama.cpp, or a directory / repository id for other engines."""
    taken = await db.execute(select(LlmModel.id).where(LlmModel.name == body.name))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "model_name_taken")
    _check_engine(user, body.engine)
    await _check_member_path(db, user, body.path)
    default = get_settings().default_llama_args if body.engine == "llama.cpp" else ""
    args = body.args.strip() or default
    review = await _review(db, body.engine, args, [default])  # the defaults are the admin's own setting
    use_now = _gate(user, review, body.confirm_args, True, body.request_approval)
    path, size = _source(body.engine, body.path)
    model = LlmModel(
        name=body.name,
        file_path=path,
        size_bytes=size,
        status="ready",
        args=args if use_now else default,
        args_pending=None if use_now else args,
        description=body.description,
        engine=None if body.engine == "llama.cpp" else body.engine,
        created_by=user.id,
    )
    db.add(model)
    if not use_now:
        record(db, user, "model.args_request", body.name, args=args, needs_approval=review.needs_approval)
    elif review.denied:
        record(db, user, "model.args_update", body.name, before="", after=args, denied=review.denied)
    await db.commit()
    if not use_now:
        _ask_admins(request, user, model, review)
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int,
    body: ModelUpdateIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Edits a model. Unchanged fields a form sends back are not checked against the argument policy again."""
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    if body.name is not None and body.name != model.name:
        taken = await db.execute(select(LlmModel.id).where(LlmModel.name == body.name))
        if taken.first() is not None:
            raise ApiError(status.HTTP_409_CONFLICT, "model_name_taken")
        model.name = body.name
    old_engine = model.engine or "llama.cpp"
    engine = body.engine or old_engine
    args = body.args.strip() if body.args is not None else model.args
    args_changed = args != model.args
    engine_changed = engine != old_engine
    path_changed = body.path is not None and body.path.strip() != model.file_path
    if (args_changed or engine_changed or path_changed) and "custom" in (old_engine, engine):
        _check_engine(user, "custom")
    if body.description is not None:
        model.description = body.description
    if engine_changed or path_changed:
        source = body.path or model.file_path
        await _check_member_path(db, user, source)
        model.file_path, model.size_bytes = _source(engine, source)
        model.engine = None if engine == "llama.cpp" else engine
    requested = None
    if args_changed or (engine_changed and not user.is_admin):
        # Arguments accepted for one engine mean nothing for another.
        review = await _review(db, engine, args, [] if engine_changed else [model.args])
        if _gate(user, review, body.confirm_args, not engine_changed, body.request_approval):
            if args_changed:
                record(db, user, "model.args_update", model.name, before=model.args, after=args, denied=review.denied)
            model.args, model.args_pending = args, None
        else:
            model.args_pending, requested = args, review
            record(db, user, "model.args_request", model.name, args=args, needs_approval=review.needs_approval)
    await db.commit()
    if requested is not None:
        _ask_admins(request, user, model, requested)
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


@router.post("/{model_id}/args/approve", response_model=ModelOut)
async def approve_args(
    model_id: int,
    body: ModelArgsReviewIn,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """Puts the launch arguments a member asked for into use (from the model's next load on)."""
    model = await _get_model(db, model_id)
    if not model.args_pending:
        raise ApiError(status.HTTP_409_CONFLICT, "no_pending_args")
    review = await _review(db, model.engine or "llama.cpp", model.args_pending, [model.args])
    _gate(admin, review, body.confirm_args, False, False)
    record(
        db,
        admin,
        "model.args_update",
        model.name,
        before=model.args,
        after=model.args_pending,
        approved=review.needs_approval,
        denied=review.denied,
    )
    model.args, model.args_pending = model.args_pending, None
    await db.commit()
    return (await queries.model_out(db, [model], get_settings(), lanes.loaded_model_ids()))[0]


@router.delete("/{model_id}/args/pending", response_model=ModelOut)
async def drop_pending_args(
    model_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    lanes: LaneManager = Depends(get_lanes),
):
    """The owner withdraws a request, or an admin rejects it; the model keeps its current arguments."""
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    if model.args_pending:
        action = "model.args_reject" if user.is_admin and model.created_by != user.id else "model.args_withdraw"
        record(db, user, action, model.name, args=model.args_pending)
        model.args_pending = None
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


# ---- launch-argument presets ------------------------------------------------


async def _preset_args(
    db: AsyncSession, user: User, model: LlmModel, body: PresetIn, preset: ModelPreset | None = None
) -> str:
    """Presets follow the model's argument policy; there is no approval request for them (ask an admin)."""
    args = body.args.strip()
    before = preset.args if preset is not None else None
    if args == before:
        return args
    approved = [model.args] + ([before] if before else [])
    review = await _review(db, model.engine or "llama.cpp", args, approved)
    _gate(user, review, body.confirm_args, False, False)
    label = f"{model.name} ({body.name.strip()})"
    record(db, user, "model.args_update", label, before=before or "", after=args, denied=review.denied)
    return args


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
    preset = ModelPreset(model_id=model.id, name=body.name.strip(), args=await _preset_args(db, user, model, body))
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
    preset.args = await _preset_args(db, user, model, body, preset)
    preset.name = body.name.strip()
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
