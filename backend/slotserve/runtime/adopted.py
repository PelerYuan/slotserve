"""A model server process this program did not start (a node agent taking over servers its predecessor kept).

``AdoptedProcess`` offers the parts of ``subprocess.Popen`` that ``LlamaRuntime`` uses, by process id.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

_STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AdoptedProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and not pid_alive(self.pid):
            self.returncode = -1  # the real exit code belongs to the process's parent
        return self.returncode

    def _signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout or 0)
            time.sleep(0.1)
        return self.returncode or 0
