"""Owns the single llama-server child process."""

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import IO

import httpx

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import LlmModel, ModelLoadEvent, ModelPreset

log = logging.getLogger(__name__)

# Flags that SlotServe always sets itself; user-supplied copies are dropped.
MANAGED_FLAGS = {"-m", "--model", "--host", "--port", "-a", "--alias"}
FAIL_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
# llama.cpp / CUDA messages that mean the model did not fit in GPU memory.
OOM_RE = re.compile(r"out of memory|cudaMalloc failed|failed to allocate .*buffer|unable to allocate .*CUDA", re.I)


class RuntimeState(StrEnum):
    STOPPED = "stopped"
    LOADING = "loading"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: RuntimeState
    model_id: int | None
    model_name: str | None
    preset_id: int | None
    preset_name: str | None
    load_started_at: datetime | None
    ready_at: datetime | None
    est_load_seconds: float | None
    error: str | None
    error_hint: str | None = None  # "oom" when the log shows GPU memory exhaustion
    draining: bool = False
    inflight: int = 0


def split_args(text: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(text)
    # Non-POSIX mode keeps backslashes in Windows paths but also keeps the quotes.
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(text, posix=False)]


def strip_managed_flags(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        flag = arg.split("=", 1)[0]
        if flag in MANAGED_FLAGS:
            skip = "=" not in arg
            continue
        out.append(arg)
    return out


class LlamaRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log_path: Path = settings.log_dir / "llama-server.log"
        self._lock = asyncio.Lock()
        self._proc: subprocess.Popen | None = None
        self._log_file: IO[bytes] | None = None
        self._watch_task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(3.0))

        self.state = RuntimeState.STOPPED
        self.model_id: int | None = None
        self.model_name: str | None = None
        self.preset_id: int | None = None
        self.preset_name: str | None = None
        self.load_started_at: datetime | None = None
        self.ready_at: datetime | None = None
        self.est_load_seconds: float | None = None
        self.error: str | None = None
        self.error_hint: str | None = None
        self._failed_at: datetime | None = None
        self._fail_count = 0
        # Gateway requests currently being served; a switch waits for them ("draining").
        self.inflight = 0
        self.draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    # ---- public -----------------------------------------------------------

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            model_id=self.model_id,
            model_name=self.model_name,
            preset_id=self.preset_id,
            preset_name=self.preset_name,
            load_started_at=self.load_started_at,
            ready_at=self.ready_at,
            est_load_seconds=self.est_load_seconds,
            error=self.error,
            error_hint=self.error_hint,
            draining=self.draining,
            inflight=self.inflight,
        )

    def begin_request(self) -> None:
        self.inflight += 1
        self._idle.clear()

    def end_request(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        if self.inflight == 0:
            self._idle.set()

    async def _drain(self) -> None:
        """Lets requests in flight finish before the process is stopped (new ones are refused meanwhile)."""
        if self.state != RuntimeState.READY or self.inflight == 0 or self.settings.drain_seconds <= 0:
            return
        self.draining = True
        log.info("waiting up to %ss for %s request(s) to finish", self.settings.drain_seconds, self.inflight)
        try:
            await asyncio.wait_for(self._idle.wait(), self.settings.drain_seconds)
        except TimeoutError:
            log.info("drain timed out with %s request(s) still running", self.inflight)
        finally:
            self.draining = False

    @property
    def spec(self) -> tuple[int, int | None] | None:
        """What is (or is being) loaded: ``(model_id, preset_id)``."""
        return (self.model_id, self.preset_id) if self.model_id is not None else None

    async def ensure(
        self,
        model: LlmModel | None,
        est_load_seconds: float = 0.0,
        force: bool = False,
        preset: ModelPreset | None = None,
    ) -> None:
        """Make the running process match ``model`` with ``preset`` (``None`` = nothing loaded)."""
        async with self._lock:
            if model is None:
                if self.state != RuntimeState.STOPPED or self.model_id is not None:
                    await self._drain()
                    await self._stop()
                    self._reset_identity()
                return

            same = self.spec == (model.id, preset.id if preset else None)
            if not force and same and self.state in (RuntimeState.LOADING, RuntimeState.READY):
                return
            if not force and same and self.state == RuntimeState.FAILED and not self._retry_due():
                return
            if not same:
                self._fail_count = 0
                await self._drain()
            await self._stop()
            self._start(model, est_load_seconds, preset)

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop()
            self._reset_identity()
        await self._http.aclose()

    def tail_log(self, lines: int = 200) -> list[str]:
        if not self.log_path.is_file():
            return []
        with self.log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
        tail = deque(data.decode("utf-8", errors="replace").splitlines(), maxlen=lines)
        return list(tail)

    def build_command(self, model: LlmModel, preset: ModelPreset | None = None) -> list[str]:
        user_args = strip_managed_flags(split_args((preset.args if preset else model.args) or ""))
        base = split_args(self.settings.llama_server_cmd)
        if base:
            exe = Path(base[0])
            if exe.exists():
                base[0] = str(exe.resolve())
            elif found := shutil.which(base[0]):
                base[0] = found
        return [
            *base,
            "-m",
            model.file_path,
            "--host",
            self.settings.llama_host,
            "--port",
            str(self.settings.llama_port),
            "--alias",
            model.name,
            *user_args,
        ]

    # ---- internals --------------------------------------------------------

    def _retry_due(self) -> bool:
        if self._failed_at is None:
            return True
        idx = min(self._fail_count - 1, len(FAIL_BACKOFF_SECONDS) - 1)
        return (utcnow() - self._failed_at).total_seconds() >= FAIL_BACKOFF_SECONDS[max(idx, 0)]

    def _reset_identity(self) -> None:
        self.model_id = None
        self.model_name = None
        self.preset_id = None
        self.preset_name = None
        self.load_started_at = None
        self.ready_at = None
        self.est_load_seconds = None
        self.error = None
        self.error_hint = None
        self._fail_count = 0
        self._failed_at = None

    def _start(self, model: LlmModel, est_load_seconds: float, preset: ModelPreset | None = None) -> None:
        cmd = self.build_command(model, preset)
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_log()
        self._log_file = self.log_path.open("ab")
        started = utcnow()
        self._log_file.write(f"\n===== {started.isoformat()} start: {shlex.join(cmd)}\n".encode())
        self._log_file.flush()

        self.model_id = model.id
        self.model_name = model.name
        self.preset_id = preset.id if preset else None
        self.preset_name = preset.name if preset else None
        self.load_started_at = started
        self.ready_at = None
        self.est_load_seconds = est_load_seconds
        self.error = None
        self.error_hint = None

        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=self._log_file, stderr=subprocess.STDOUT, **kwargs
            )
        except OSError as exc:
            self._proc = None
            self._mark_failed(f"spawn failed: {exc}")
            self._schedule_event(model.id, started, False, self.error)
            return

        log.info("llama-server starting (pid %s) for model %s", self._proc.pid, model.name)
        self.state = RuntimeState.LOADING
        self._watch_task = asyncio.create_task(self._watch(self._proc, model.id, started))

    def _rotate_log(self, max_bytes: int = 20 * 1024 * 1024) -> None:
        if self.log_path.is_file() and self.log_path.stat().st_size > max_bytes:
            old = self.log_path.with_suffix(".log.1")
            with contextlib.suppress(OSError):
                old.unlink(missing_ok=True)
                self.log_path.rename(old)

    async def _watch(self, proc: subprocess.Popen, model_id: int, started: datetime) -> None:
        deadline = started.timestamp() + self.settings.load_timeout_seconds
        health = f"{self.settings.llama_base_url}/health"
        while True:
            code = proc.poll()
            if code is not None:
                self._mark_failed(f"llama-server exited with code {code} while loading")
                await self._record_event(model_id, started, False, self.error)
                return
            try:
                resp = await self._http.get(health)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if utcnow().timestamp() > deadline:
                self._mark_failed(f"model did not become ready within {self.settings.load_timeout_seconds}s")
                await self._record_event(model_id, started, False, self.error)
                await asyncio.to_thread(self._terminate, proc)
                return
            await asyncio.sleep(1.0)

        self.state = RuntimeState.READY
        self.ready_at = utcnow()
        self._fail_count = 0
        self._failed_at = None
        log.info("model %s ready after %.1fs", self.model_name, (self.ready_at - started).total_seconds())
        await self._record_event(model_id, started, True, None)

        await asyncio.to_thread(proc.wait)
        if self._proc is proc:
            self._mark_failed(f"llama-server exited unexpectedly with code {proc.returncode}")

    def _mark_failed(self, message: str) -> None:
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.flush()
        lines = self.tail_log(200)
        starts = [i for i, line in enumerate(lines) if line.startswith("===== ") and " start: " in line]
        current = lines[starts[-1] :] if starts else lines  # only this launch's output
        if any(OOM_RE.search(line) for line in current):
            self.error_hint = "oom"
            message += " (GPU out of memory: offload fewer layers with -ngl, or lower the context size -c)"
        log.warning(message)
        self.state = RuntimeState.FAILED
        self.error = message
        self.ready_at = None
        self._failed_at = utcnow()
        self._fail_count += 1

    def _schedule_event(self, model_id: int, started: datetime, success: bool, error: str | None) -> None:
        asyncio.get_running_loop().create_task(self._record_event(model_id, started, success, error))

    async def _record_event(self, model_id: int, started: datetime, success: bool, error: str | None) -> None:
        try:
            async with SessionLocal() as db:
                db.add(
                    ModelLoadEvent(
                        model_id=model_id, started_at=started, finished_at=utcnow(), success=success, error=error
                    )
                )
                await db.commit()
        except Exception:
            log.exception("failed to record load event")

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=self.settings.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    async def _stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        proc, self._proc = self._proc, None
        if proc is not None:
            self.state = RuntimeState.STOPPING
            log.info("stopping llama-server (pid %s)", proc.pid)
            await asyncio.to_thread(self._terminate, proc)
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.write(f"===== {utcnow().isoformat()} stopped\n".encode())
                self._log_file.close()
            self._log_file = None
        self.state = RuntimeState.STOPPED
