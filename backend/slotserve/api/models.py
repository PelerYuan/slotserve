from pathlib import Path

from fastapi import APIRouter, Depends, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import admin_user, current_user, get_runtime, get_scheduler
from ..errors import ApiError
from ..orm import LlmModel, Reservation, User
from ..runtime.manager import LlamaRuntime, split_args
from ..runtime.scheduler import Scheduler
from ..schemas import ModelOut, ModelUpdateIn, ScanResultOut
from ..services import queries, scanner
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
    runtime: LlamaRuntime = Depends(get_runtime),
):
    models = (await db.execute(select(LlmModel).where(LlmModel.deleted_at.is_(None)).order_by(LlmModel.is_default.desc(), LlmModel.name))).scalars().all()
    return await queries.model_out(db, list(models), get_settings(), runtime.model_id)


@router.post("/scan", response_model=ScanResultOut)
async def scan_models(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    added, skipped = await scanner.scan(db, get_settings(), user.id)
    return ScanResultOut(added=added, skipped=skipped)


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int,
    body: ModelUpdateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    runtime: LlamaRuntime = Depends(get_runtime),
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
    await db.commit()
    return (await queries.model_out(db, [model], get_settings(), runtime.model_id))[0]


@router.post("/{model_id}/default", response_model=ModelOut)
async def set_default(
    model_id: int,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    runtime: LlamaRuntime = Depends(get_runtime),
    scheduler: Scheduler = Depends(get_scheduler),
):
    model = await _get_model(db, model_id)
    if model.status != "ready":
        raise ApiError(status.HTTP_400_BAD_REQUEST, "model_not_ready")
    await db.execute(update(LlmModel).values(is_default=False))
    model.is_default = True
    record(db, admin, "model.set_default", model.name)
    await db.commit()
    scheduler.poke()
    return (await queries.model_out(db, [model], get_settings(), runtime.model_id))[0]


@router.delete("/{model_id}/default", status_code=204)
async def clear_default(
    model_id: int,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
    scheduler: Scheduler = Depends(get_scheduler),
):
    model = await _get_model(db, model_id)
    model.is_default = False
    record(db, admin, "model.clear_default", model.name)
    await db.commit()
    scheduler.poke()


@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: int,
    delete_file: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    runtime: LlamaRuntime = Depends(get_runtime),
):
    model = await _get_model(db, model_id)
    _check_owner(model, user)
    if runtime.model_id == model.id:
        raise ApiError(status.HTTP_409_CONFLICT, "model_in_use")
    future = await db.execute(
        select(Reservation.id).where(
            Reservation.model_id == model.id, Reservation.status == "booked", Reservation.end_at > utcnow()
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
        model.name = f"{model.name}#deleted-{model.id}"
    else:
        await db.delete(model)
    if delete_file:
        path = Path(model.file_path)
        root = get_settings().models_dir.resolve()
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
    await db.commit()
