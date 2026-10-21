"""Serial background download queue with HTTP Range resume."""

import asyncio
import contextlib
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select, update

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import DownloadTask
from . import scanner

log = logging.getLogger(__name__)

CHUNK_FLUSH_BYTES = 4 * 1024 * 1024
PROGRESS_INTERVAL = 1.5
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class DownloadCancelled(Exception):
    pass


def filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def safe_filename(name: str) -> str | None:
    name = _BAD_CHARS.sub("_", name.strip())
    if not name or name in {".", ".."} or name.startswith("."):
        return None
    return name[:255]


class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._wake = asyncio.Event()
        self._cancel: set[int] = set()
        self.current_id: int | None = None
        self.events = None  # Notifier, set by the app
        self.deployer = None  # services.catalog_deploy.Deployer, set by the app

    def poke(self) -> None:
        self._wake.set()

    def cancel(self, task_id: int) -> None:
        self._cancel.add(task_id)

    def dest_path(self, filename: str) -> Path:
        return self.settings.models_dir / filename

    async def recover(self) -> None:
        """Tasks left running by a previous process go back to the queue."""
        async with SessionLocal() as db:
            await db.execute(update(DownloadTask).where(DownloadTask.status == "running").values(status="queued"))
            await db.commit()

    async def run(self) -> None:
        await self.recover()
        while True:
            try:
                task_id = await self._next_task()
                if task_id is not None:
                    await self._process(task_id)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("download loop error")
            try:
                await asyncio.wait_for(self._wake.wait(), 10)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _next_task(self) -> int | None:
        async with SessionLocal() as db:
            stmt = select(DownloadTask.id).where(DownloadTask.status == "queued").order_by(DownloadTask.id).limit(1)
            return (await db.execute(stmt)).scalar_one_or_none()

    async def _set(self, task_id: int, **values) -> None:
        async with SessionLocal() as db:
            await db.execute(update(DownloadTask).where(DownloadTask.id == task_id).values(**values))
            await db.commit()

    async def _process(self, task_id: int) -> None:
        async with SessionLocal() as db:
            task = await db.get(DownloadTask, task_id)
            if task is None or task.status != "queued":
                return
            url, filename, user_id = task.url, task.filename, task.user_id
        self._cancel.discard(task_id)
        self.current_id = task_id
        await self._set(task_id, status="running", error=None, speed_bps=0.0)
        try:
            await self._download(task_id, url, filename)
        except DownloadCancelled:
            await self._set(task_id, status="cancelled", speed_bps=0.0, finished_at=utcnow())
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("download %s failed: %s", task_id, exc)
            await self._set(task_id, status="failed", error=str(exc)[:1000], speed_bps=0.0, finished_at=utcnow())
            self._emit("download_failed", user_id=user_id, filename=filename, error=str(exc)[:200])
            return
        finally:
            self.current_id = None

        dest = self.dest_path(filename)
        model_id = None
        if self.deployer is not None and await self.deployer.owns(task_id):
            # Part of a catalog deployment: it registers the model itself once every file is here.
            await self._set(task_id, status="done", speed_bps=0.0, finished_at=utcnow())
            self._emit("download_done", user_id=user_id, filename=filename)
            await self.deployer.task_done(task_id)
            return
        if scanner.is_primary_gguf(dest):
            async with SessionLocal() as db:
                model = await scanner.register_file(db, dest, self.settings, user_id)
                await db.commit()
                model_id = model.id if model else None
        await self._set(task_id, status="done", speed_bps=0.0, finished_at=utcnow(), model_id=model_id)
        self._emit("download_done", user_id=user_id, filename=filename)

    def _emit(self, event: str, **data) -> None:
        if self.events is not None and data.get("user_id") is not None:
            self.events.emit(event, **data)

    def auth_headers(self, url: str) -> dict[str, str]:
        """Sends the Hugging Face token only to the configured Hugging Face endpoint."""
        endpoint = self.settings.hf_endpoint.rstrip("/")
        if self.settings.hf_token and (url == endpoint or url.startswith(endpoint + "/")):
            return {"Authorization": f"Bearer {self.settings.hf_token}"}
        return {}

    async def _download(self, task_id: int, url: str, filename: str) -> None:
        dest = self.dest_path(filename)
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise RuntimeError(f"file already exists: {dest.name}")

        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        headers.update(self.auth_headers(url))
        timeout = httpx.Timeout(30.0, read=120.0)
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 206:
                    total = _total_from_content_range(resp.headers.get("content-range"))
                elif resp.status_code == 200:
                    offset = 0
                    length = resp.headers.get("content-length")
                    total = int(length) if length and length.isdigit() else None
                elif resp.status_code == 416 and offset:
                    total = offset  # already complete
                else:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                if total is not None:
                    free = shutil.disk_usage(dest.parent).free
                    needed = total - offset + int(self.settings.download_min_free_gb * 1e9)
                    if free < needed:
                        raise RuntimeError(
                            f"not enough disk space: {free / 1e9:.1f} GB free, need {needed / 1e9:.1f} GB"
                        )

                await self._set(task_id, total_bytes=total, downloaded_bytes=offset)
                if resp.status_code != 416:
                    await self._stream_to_file(task_id, resp, part, offset)

        if total is not None and part.stat().st_size != total:
            raise RuntimeError(f"incomplete download: {part.stat().st_size} of {total} bytes")
        part.replace(dest)

    async def _stream_to_file(self, task_id: int, resp: httpx.Response, part: Path, offset: int) -> None:
        mode = "ab" if offset else "wb"
        done = offset
        buf = bytearray()
        last_report = time.monotonic()
        last_bytes = done
        f = await asyncio.to_thread(part.open, mode)
        try:
            async for chunk in resp.aiter_bytes():
                if task_id in self._cancel:
                    raise DownloadCancelled
                buf += chunk
                if len(buf) >= CHUNK_FLUSH_BYTES:
                    await asyncio.to_thread(f.write, bytes(buf))
                    done += len(buf)
                    buf.clear()
                now = time.monotonic()
                if now - last_report >= PROGRESS_INTERVAL:
                    speed = (done + len(buf) - last_bytes) / (now - last_report)
                    await self._set(task_id, downloaded_bytes=done + len(buf), speed_bps=speed)
                    last_report, last_bytes = now, done + len(buf)
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
                done += len(buf)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(f.close)
            await self._set(task_id, downloaded_bytes=done)


def _total_from_content_range(value: str | None) -> int | None:
    # "bytes 100-999/1000"
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1]
    return int(total) if total.isdigit() else None
