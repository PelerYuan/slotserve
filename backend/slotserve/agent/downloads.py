"""Downloads executed by a node agent into its own models directory, reported back as events."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from ..config import Settings
from ..services.downloader import DownloadCancelled, Downloader

log = logging.getLogger(__name__)

Report = Callable[[dict], Awaitable[None]]
# Credentials for the download running in the current task, chosen by the control plane.
_headers: ContextVar[dict[str, str] | None] = ContextVar("download_headers", default=None)


class _NodeDownloader(Downloader):
    """The control plane's downloader (HTTP Range resume, free-space check), with progress sent as events."""

    def __init__(self, settings: Settings, report: Report):
        super().__init__(settings)
        self._report = report

    async def _set(self, task_id: int, **values) -> None:
        await self._report({"task_id": task_id, "status": "running", **values})

    def auth_headers(self, url: str) -> dict[str, str]:
        return dict(_headers.get() or {})  # the control plane decides which credentials a URL gets

    async def fetch(self, task_id: int, url: str, filename: str, headers: dict[str, str]) -> str:
        _headers.set(headers)
        await self._download(task_id, url, filename)
        return str(self.dest_path(filename).resolve())


class AgentDownloads:
    def __init__(self, settings: Settings, report: Report):
        self.settings = settings
        self.report = report
        self._downloader = _NodeDownloader(settings, report)
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(self, params: dict) -> dict:
        task_id = int(params["task_id"])
        if task_id in self._tasks and not self._tasks[task_id].done():
            return {"accepted": True, "already": True}
        filename = str(params["filename"])
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise ValueError("invalid file name")
        self._downloader._cancel.discard(task_id)
        task = asyncio.create_task(
            self._run(task_id, str(params["url"]), filename, dict(params.get("headers") or {})),
            name=f"download-{task_id}",
        )
        self._tasks[task_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(task_id, None))
        return {"accepted": True}

    async def _run(self, task_id: int, url: str, filename: str, headers: dict[str, str]) -> None:
        try:
            path = await self._downloader.fetch(task_id, url, filename, headers)
        except DownloadCancelled:
            await self.report({"task_id": task_id, "status": "cancelled"})
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the control plane
            log.warning("download %s failed: %s", task_id, exc)
            await self.report({"task_id": task_id, "status": "failed", "error": str(exc)[:1000]})
            return
        await self.report({"task_id": task_id, "status": "done", "path": path})

    async def cancel(self, params: dict) -> dict:
        task_id = int(params["task_id"])
        self._downloader.cancel(task_id)
        return {"running": task_id in self._tasks}

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(BaseException):
                await task
