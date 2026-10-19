import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user, get_downloader
from ..errors import ApiError
from ..orm import DownloadTask, User
from ..schemas import DownloadCreateIn, DownloadOut, HfDownloadIn, HfFileOut, HfFilesOut
from ..services.downloader import Downloader, filename_from_url, safe_filename
from ..services.scanner import SPLIT_RE

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


async def _queue(db: AsyncSession, user: User, downloader: Downloader, url: str, filename: str | None) -> DownloadTask:
    if not url.lower().startswith(("http://", "https://")):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_url")
    name = safe_filename(filename or filename_from_url(url))
    if name is None:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_filename")
    if downloader.dest_path(name).exists():
        raise ApiError(status.HTTP_409_CONFLICT, "file_exists", filename=name)
    busy = await db.execute(
        select(DownloadTask.id).where(DownloadTask.filename == name, DownloadTask.status.in_(ACTIVE))
    )
    if busy.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "download_in_progress", filename=name)
    task = DownloadTask(user_id=user.id, url=url, filename=name)
    db.add(task)
    return task


@router.post("", response_model=DownloadOut, status_code=201)
async def create_download(
    body: DownloadCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    task = await _queue(db, user, downloader, body.url.strip(), body.filename)
    await db.commit()
    downloader.poke()
    return (await _out(db, [task]))[0]


# ---- Hugging Face -------------------------------------------------------------


def _hf_headers() -> dict[str, str]:
    token = get_settings().hf_token
    return {"Authorization": f"Bearer {token}"} if token else {}


@router.get("/hf/files", response_model=HfFilesOut)
async def hf_files(
    repo: str = Query(min_length=3, max_length=200, pattern=r"^[\w.\-]+/[\w.\-]+$"),
    revision: str = Query(default="main", min_length=1, max_length=100),
    _: User = Depends(current_user),
):
    """Lists the GGUF files of a Hugging Face model repository, grouping split files."""
    endpoint = get_settings().hf_endpoint.rstrip("/")
    url = f"{endpoint}/api/models/{repo}/tree/{quote(revision, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, params={"recursive": "true"}, headers=_hf_headers())
    except httpx.HTTPError as exc:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "hf_unreachable", reason=exc.__class__.__name__) from exc
    if resp.status_code in (401, 403):
        raise ApiError(status.HTTP_403_FORBIDDEN, "hf_forbidden")
    if resp.status_code == 404:
        raise ApiError(status.HTTP_404_NOT_FOUND, "hf_not_found", repo=repo)
    if resp.status_code != 200:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "hf_unreachable", reason=f"HTTP {resp.status_code}")

    groups: dict[str, list[tuple[str, int]]] = {}
    for item in resp.json():
        path = item.get("path", "")
        if item.get("type") != "file" or not path.lower().endswith(".gguf"):
            continue
        size = int((item.get("lfs") or {}).get("size") or item.get("size") or 0)
        groups.setdefault(SPLIT_RE.sub("", path), []).append((path, size))
    files = []
    for parts in groups.values():
        parts.sort()
        primary = parts[0][0]
        files.append(
            HfFileOut(
                path=primary,
                # Primary last: a model registers when its first part lands, so the other parts must be there.
                files=[p for p, _ in parts[1:]] + [primary],
                size_bytes=sum(size for _, size in parts),
                parts=len(parts),
                kind="mmproj" if "mmproj" in primary.lower() else "model",
            )
        )
    files.sort(key=lambda f: (f.kind != "model", f.path.lower()))
    return HfFilesOut(repo=repo, revision=revision, files=files)


@router.post("/hf", response_model=list[DownloadOut], status_code=201)
async def hf_download(
    body: HfDownloadIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    downloader: Downloader = Depends(get_downloader),
):
    endpoint = get_settings().hf_endpoint.rstrip("/")
    tasks = []
    for path in body.files:
        if ".." in path.split("/") or not re.fullmatch(r"[\w.\-/ ]+\.gguf", path, re.IGNORECASE):
            raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_filename")
        url = f"{endpoint}/{body.repo}/resolve/{quote(body.revision, safe='')}/{quote(path)}"
        tasks.append(await _queue(db, user, downloader, url, path.rsplit("/", 1)[-1]))
    await db.commit()
    downloader.poke()
    return await _out(db, tasks)


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
