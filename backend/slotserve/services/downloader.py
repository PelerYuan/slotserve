"""Background download queues with HTTP Range resume: one queue per machine (node).

Each node downloads one file at a time, but nodes download in parallel: a slow or offline node never holds up
the others. A download whose node goes offline goes back to the queue and resumes where it stopped (the
``.part`` file stays on the node) once the node is back.
"""

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
        self.current_id: int | None = None  # the last download started (any node)
        self.active: dict[int | None, int] = {}  # node id (None: this machine) -> running task id
        self._workers: dict[int | None, asyncio.Task] = {}
        self.events = None  # Notifier, set by the app
        self.cluster = None  # ClusterHub, set by the app: downloads that run on other machines
        self._remote_calls: set[asyncio.Task] = set()
        self.deployer = None  # services.catalog_deploy.Deployer, set by the app

    def poke(self) -> None:
        self._wake.set()

    def cancel(self, task_id: int) -> None:
        self._cancel.add(task_id)
        if self.cluster is not None and task_id in self.cluster.downloads:
            task = asyncio.get_running_loop().create_task(self.cluster.cancel_download(task_id))
            self._remote_calls.add(task)
            task.add_done_callback(self._remote_calls.discard)

    def is_remote(self, node_id: int | None) -> bool:
        """Whether a task downloads on another machine (a node agent)."""
        return node_id is not None and self.cluster is not None and not self.cluster.registry.is_local(node_id)

    def _available(self, node_id: int | None) -> bool:
        if not self.is_remote(node_id):
            return True
        executor = self.cluster.executor(node_id)
        return executor is not None and executor.online

    def dest_path(self, filename: str, base: Path | None = None) -> Path:
        """``base``: a model library from Admin -> Storage (storage hook); default MODELS_DIR."""
        return (base or self.settings.models_dir) / filename

    async def task_dir(self, db, task: DownloadTask) -> Path | None:
        """The model library a task downloads into (storage hook), None for MODELS_DIR."""
        if task.storage_id is None:
            return None
        from ..orm import Storage
        from .storage import library_path

        st = await db.get(Storage, task.storage_id)
        if st is None or st.purpose != "models" or (path := library_path(st)) is None:
            raise RuntimeError("the model library is no longer available")
        return path

    async def recover(self) -> None:
        """Tasks left running by a previous process go back to the queue."""
        async with SessionLocal() as db:
            await db.execute(update(DownloadTask).where(DownloadTask.status == "running").values(status="queued"))
            await db.commit()

    def _key(self, node_id: int | None) -> int | None:
        """The queue a task belongs to: its node, ``None`` for this machine."""
        return node_id if self.is_remote(node_id) else None

    async def run(self) -> None:
        await self.recover()
        try:
            while True:
                self._wake.clear()
                try:
                    await self._dispatch()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("download loop error")
                try:
                    await asyncio.wait_for(self._wake.wait(), 10)
                except TimeoutError:
                    pass
        finally:
            workers = list(self._workers.values())
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with contextlib.suppress(BaseException):
                    await worker

    async def _dispatch(self) -> None:
        """Starts a worker for every node that has queued downloads, is reachable and has none running."""
        async with SessionLocal() as db:
            stmt = select(DownloadTask.node_id).where(DownloadTask.status == "queued").distinct()
            nodes = {self._key(node_id) for node_id in (await db.execute(stmt)).scalars()}
        for key in nodes:
            worker = self._workers.get(key)
            if worker is not None and not worker.done():
                continue
            if not self._available(key):  # downloads of an offline node wait for it
                continue
            self._workers[key] = asyncio.get_running_loop().create_task(self._worker(key), name=f"downloads-{key}")

    async def _worker(self, key: int | None) -> None:
        try:
            while self._available(key):
                task_id = await self._next_task(key)
                if task_id is None:
                    return
                try:
                    await self._process(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("download %s: unexpected error", task_id)
                    await self._set(task_id, status="failed", error="internal error", finished_at=utcnow())
        finally:
            if self._workers.get(key) is asyncio.current_task():
                self._workers.pop(key, None)
            self.poke()  # another node may be waiting for a worker slot

    async def _next_task(self, key: int | None = None) -> int | None:
        async with SessionLocal() as db:
            stmt = select(DownloadTask.id, DownloadTask.node_id).where(DownloadTask.status == "queued")
            for task_id, node_id in (await db.execute(stmt.order_by(DownloadTask.id))).all():
                if self._key(node_id) == key:
                    return task_id
            return None

    async def _set(self, task_id: int, **values) -> None:
        async with SessionLocal() as db:
            await db.execute(update(DownloadTask).where(DownloadTask.id == task_id).values(**values))
            await db.commit()

    async def _process(self, task_id: int) -> None:
        async with SessionLocal() as db:
            task = await db.get(DownloadTask, task_id)
            if task is None or task.status != "queued":
                return
            url, filename, user_id, node_id = task.url, task.filename, task.user_id, task.node_id
            storage_id, expected = task.storage_id, task.total_bytes
            remote = self.is_remote(node_id)
            base = None
            if not remote:
                try:
                    base = await self.task_dir(db, task)
                except RuntimeError as exc:
                    await self._set(task_id, status="failed", error=str(exc), finished_at=utcnow())
                    return
            owned = self.deployer is not None and await self.deployer.owns(task_id)
        key = self._key(node_id)
        self._cancel.discard(task_id)
        self.current_id = task_id
        self.active[key] = task_id
        await self._set(task_id, status="running", error=None, speed_bps=0.0)
        remote_path = ""
        try:
            if remote:
                remote_base = await self.cluster.library_dir(node_id, storage_id) if storage_id is not None else None
                remote_path = await self.cluster.run_download(
                    node_id,
                    task_id,
                    url,
                    filename,
                    self.auth_headers(url),
                    base=remote_base,
                    expected_bytes=expected,
                    existing_ok=owned,  # catalog files already on the node count as downloaded
                )
            else:
                await self._download(task_id, url, filename, base)
        except DownloadCancelled:
            await self._set(task_id, status="cancelled", speed_bps=0.0, finished_at=utcnow())
            return
        except Exception as exc:  # noqa: BLE001
            if remote and getattr(exc, "code", None) in ("offline", "disconnected"):
                # The node went away: try again (resuming) when it is back.
                await self._set(task_id, status="queued", speed_bps=0.0)
                return
            log.warning("download %s failed: %s", task_id, exc)
            await self._set(task_id, status="failed", error=str(exc)[:1000], speed_bps=0.0, finished_at=utcnow())
            self._emit("download_failed", user_id=user_id, filename=filename, error=str(exc)[:200])
            return
        finally:
            if self.current_id == task_id:
                self.current_id = None
            if self.active.get(key) == task_id:
                self.active.pop(key, None)

        if owned:
            # Part of a catalog deployment (on any node): it registers the model itself once every file is there.
            await self._set(task_id, status="done", speed_bps=0.0, finished_at=utcnow())
            self._emit("download_done", user_id=user_id, filename=filename)
            await self.deployer.task_done(task_id)
            return
        if remote:
            model_id = await self._register_remote(node_id, remote_path, filename, user_id)
            await self._set(task_id, status="done", speed_bps=0.0, finished_at=utcnow(), model_id=model_id)
            self._emit("download_done", user_id=user_id, filename=filename)
            return
        dest = self.dest_path(filename, base)
        model_id = None
        if scanner.is_primary_gguf(dest):
            async with SessionLocal() as db:
                model = await scanner.register_file(db, dest, self.settings, user_id)
                await db.commit()
                model_id = model.id if model else None
        await self._set(task_id, status="done", speed_bps=0.0, finished_at=utcnow(), model_id=model_id)
        self._emit("download_done", user_id=user_id, filename=filename)

    async def _register_remote(self, node_id: int, path: str, filename: str, user_id: int | None) -> int | None:
        """A model downloaded by a node is registered as available there."""
        from ..cluster import inventory
        from ..cluster import protocol as cluster_protocol

        if not path or not scanner.is_primary_gguf(Path(filename)):
            return None
        try:
            stat = await self.cluster.executor(node_id).request(cluster_protocol.M_MODELS_STAT, {"path": path})
            size = int(stat.get("size_bytes") or 0)
        except Exception:  # noqa: BLE001 - the size is only informative
            size = 0
        found = scanner.ModelFile(path=path, name=scanner.model_name_for(Path(filename)), size_bytes=size)
        async with SessionLocal() as db:
            local_id = await inventory.local_node_id(db)
            await inventory.register_node_files(db, node_id, [found], self.settings, user_id, local_id, "download")
            await db.commit()
            return await inventory.model_at(db, node_id, path)

    def _emit(self, event: str, **data) -> None:
        if self.events is not None and data.get("user_id") is not None:
            self.events.emit(event, **data)

    def auth_headers(self, url: str) -> dict[str, str]:
        """Sends the Hugging Face token only to the configured Hugging Face endpoint."""
        endpoint = self.settings.hf_endpoint.rstrip("/")
        if self.settings.hf_token and (url == endpoint or url.startswith(endpoint + "/")):
            return {"Authorization": f"Bearer {self.settings.hf_token}"}
        return {}

    async def _download(self, task_id: int, url: str, filename: str, base: Path | None = None) -> None:
        dest = self.dest_path(filename, base)
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
