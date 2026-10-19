from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user, get_downloader
from ..errors import ApiError
from ..orm import DownloadTask, User
from ..schemas import DownloadCreateIn, DownloadOut
from ..services.downloader import Downloader, filename_from_url, safe_filename

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

ACTIVE = ("queued", "running")


async def _out(db: AsyncSession, tasks: list[DownloadTask]) -> list[DownloadOut]:
    ids = {t.user_id for t in tasks if t.user_id is not None}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(ids)))).all()) if ids else {}
    return [DownloadOut.model_validate(t).model_copy(update={"username": names.get(t.user_id)}) for t in tasks]


@router.get("", response_model=list[DownloadOut])
async def list_downloads(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    tasks = (await db.execute(select(DownloadTask).order_by(DownloadTask.id.desc()).limit(100))).scalars().all()
    return await _out(db, list(tasks))


@router.post("", response_model=DownloadOut, status_code=201)
async def create_download(
    body: DownloadCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_url")
    filename = safe_filename(body.filename or filename_from_url(url))
    if filename is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_filename")
    if downloader.dest_path(filename).exists():
        raise ApiError(status.HTTP_409_CONFLICT, "file_exists", filename=filename)
    busy = await db.execute(
        select(DownloadTask.id).where(DownloadTask.filename == filename, DownloadTask.status.in_(ACTIVE))
    )
    if busy.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "download_in_progress", filename=filename)
    task = DownloadTask(user_id=user.id, url=url, filename=filename)
    db.add(task)
    await db.commit()
    downloader.poke()
    return (await _out(db, [task]))[0]


async def _owned_task(db: AsyncSession, task_id: int, user: User) -> DownloadTask:
    task = await db.get(DownloadTask, task_id)
    if task is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if not user.is_admin and task.user_id != user.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "not_owner")
    return task


@router.post("/{task_id}/retry", response_model=DownloadOut)
async def retry_download(
    task_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    task = await _owned_task(db, task_id, user)
    if task.status not in ("failed", "cancelled"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "download_not_retryable")
    task.status = "queued"
    task.error = None
    task.finished_at = None
    await db.commit()
    downloader.poke()
    return (await _out(db, [task]))[0]


@router.delete("/{task_id}", status_code=204)
async def cancel_download(
    task_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    task = await _owned_task(db, task_id, user)
    if task.status == "running":
        downloader.cancel(task.id)
    elif task.status == "queued":
        task.status = "cancelled"
        task.finished_at = utcnow()
        await db.commit()
    elif task.status in ("failed", "cancelled", "done"):
        # Remove finished entries from the list (keeps the downloaded file).
        part = downloader.dest_path(task.filename + ".part")
        if task.status != "done" and part.exists():
            part.unlink()
        await db.delete(task)
        await db.commit()
