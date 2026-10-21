"""Downloads executed by a node agent into its own models directory (or a model library), reported as events.

Each download is a task id of the control plane. The agent remembers how its recent downloads ended, so a result
that was reported while the connection was down is not lost: when the control plane asks for the same task again
after reconnecting, it gets the result at once instead of a second download.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..config import Settings
from ..services.downloader import DownloadCancelled, Downloader

log = logging.getLogger(__name__)

Report = Callable[[dict], Awaitable[None]]
KEEP_RESULTS = 500
# Credentials for the download running in the current task, chosen by the control plane.
_headers: ContextVar[dict[str, str] | None] = ContextVar("download_headers", default=None)


def check_relative(name: str) -> str:
    """A file name, or a path below the target directory (safetensors folders); never absolute or with ``..``."""
    text = name.replace("\\", "/")
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or PureWindowsPath(name).drive
        or PurePosixPath(text).is_absolute()
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
    ):
        raise ValueError("invalid file name")
    return "/".join(parts)


class _NodeDownloader(Downloader):
    """The control plane's downloader (HTTP Range resume, free-space check), with progress sent as events."""

    def __init__(self, settings: Settings, report: Report):
        super().__init__(settings)
        self._report = report

    async def _set(self, task_id: int, **values) -> None:
        await self._report({"task_id": task_id, "status": "running", **values})

    def auth_headers(self, url: str) -> dict[str, str]:
        return dict(_headers.get() or {})  # the control plane decides which credentials a URL gets

    async def fetch(
        self,
        task_id: int,
        url: str,
        filename: str,
        headers: dict[str, str],
        base: Path | None = None,
        expected: int | None = None,
        existing_ok: bool = False,
    ) -> str:
        _headers.set(headers)
        dest = self.dest_path(filename, base)
        if dest.is_file() and not dest.with_name(dest.name + ".part").exists():
            size = dest.stat().st_size
            # Already here: finished while the control plane was away, or part of a catalog deploy.
            if existing_ok or (expected is not None and size == expected):
                await self._set(task_id, downloaded_bytes=size, total_bytes=size, speed_bps=0.0)
                return str(dest.resolve())
        await self._download(task_id, url, filename, base)
        return str(dest.resolve())


class AgentDownloads:
    def __init__(self, settings: Settings, report: Report):
        self.settings = settings
        self.report = report
        self._downloader = _NodeDownloader(settings, self._report)
        self._tasks: dict[int, asyncio.Task] = {}
        self._progress: dict[int, dict] = {}
        self.results: OrderedDict[int, dict] = OrderedDict()  # task id -> final report (done / failed / cancelled)

    async def _report(self, data: dict) -> None:
        if data.get("status") == "running":
            self._progress[data["task_id"]] = {**self._progress.get(data["task_id"], {}), **data}
        await self.report(data)

    async def start(self, params: dict) -> dict:
        task_id = int(params["task_id"])
        if task_id in self._tasks and not self._tasks[task_id].done():
            return {"accepted": True, "already": True}
        if task_id in self.results:
            return {"accepted": True, "result": self.results[task_id]}
        filename = check_relative(str(params["filename"]))
        base = params.get("base")
        if base is not None and not Path(str(base)).is_absolute():
            raise ValueError("invalid target directory")
        self._downloader._cancel.discard(task_id)
        task = asyncio.create_task(
            self._run(
                task_id,
                str(params["url"]),
                filename,
                dict(params.get("headers") or {}),
                Path(str(base)) if base else None,
                params.get("expected_bytes"),
                bool(params.get("existing_ok")),
            ),
            name=f"download-{task_id}",
        )
        self._tasks[task_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(task_id, None))
        return {"accepted": True}

    def _finish(self, task_id: int, data: dict) -> dict:
        self._progress.pop(task_id, None)
        self.results[task_id] = data
        while len(self.results) > KEEP_RESULTS:
            self.results.popitem(last=False)
        return data

    async def _run(
        self,
        task_id: int,
        url: str,
        filename: str,
        headers: dict[str, str],
        base: Path | None,
        expected: int | None,
        existing_ok: bool,
    ) -> None:
        try:
            path = await self._downloader.fetch(task_id, url, filename, headers, base, expected, existing_ok)
        except DownloadCancelled:
            await self.report(self._finish(task_id, {"task_id": task_id, "status": "cancelled"}))
            return
        except asyncio.CancelledError:
            self._progress.pop(task_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the control plane
            log.warning("download %s failed: %s", task_id, exc)
            await self.report(self._finish(task_id, {"task_id": task_id, "status": "failed", "error": str(exc)[:1000]}))
            return
        await self.report(self._finish(task_id, {"task_id": task_id, "status": "done", "path": path}))

    async def cancel(self, params: dict) -> dict:
        task_id = int(params["task_id"])
        self._downloader.cancel(task_id)
        return {"running": task_id in self._tasks}

    def snapshot(self) -> list[dict]:
        """Running downloads with their progress, and how recent ones ended (sent in ``hello``)."""
        running = []
        for tid in self._tasks:
            progress = {k: v for k, v in self._progress.get(tid, {}).items() if k != "status"}
            running.append({**progress, "task_id": tid, "status": "running"})
        return running + list(self.results.values())[-50:]

    async def list(self, _params: dict) -> dict:
        return {"downloads": self.snapshot()}

    def reconcile(self, wanted: list[int] | None) -> list[int]:
        """Cancels downloads the control plane no longer wants (cancelled or removed while it was away)."""
        if wanted is None:
            return []
        keep = {int(i) for i in wanted}
        stale = [tid for tid in self._tasks if tid not in keep]
        for tid in stale:
            log.info("download %s is no longer wanted by the control plane; cancelling it", tid)
            self._downloader.cancel(tid)
        return stale

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(BaseException):
                await task
