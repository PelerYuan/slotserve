"""What a machine has (CPU cores, memory, GPUs) and how busy it is, without extra dependencies."""

from __future__ import annotations

import ctypes
import os
import platform
import socket
import sys
import time
from pathlib import Path

from .. import __version__


def memory_mb() -> tuple[int | None, int | None]:
    """(used, total) RAM in MiB, or (None, None) when unknown."""
    try:
        if sys.platform.startswith("linux"):
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                info[key] = int(value.split()[0])  # kB
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
            return (total - available) // 1024, total // 1024
        if os.name == "nt":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                total = status.ullTotalPhys // (1024 * 1024)
                return total - status.ullAvailPhys // (1024 * 1024), total
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


class CpuMeter:
    """CPU utilization (0-100) between two calls, from /proc/stat or GetSystemTimes."""

    def __init__(self):
        self._last: tuple[float, float] | None = self._times()

    @staticmethod
    def _times() -> tuple[float, float] | None:
        """(idle, total) CPU time since boot."""
        try:
            if sys.platform.startswith("linux"):
                fields = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                return idle, sum(fields)
            if os.name == "nt":
                idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
                if ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return float(idle.value), float(kernel.value + user.value)  # kernel time includes idle
        except (OSError, ValueError, IndexError, AttributeError):
            pass
        return None

    def percent(self) -> float | None:
        now = self._times()
        last, self._last = self._last, now
        if now is None or last is None or now[1] <= last[1]:
            return None
        busy = 1.0 - (now[0] - last[0]) / (now[1] - last[1])
        return round(max(0.0, min(1.0, busy)) * 100, 1)


def machine_info() -> dict:
    """Static facts about this machine (sent once per connection)."""
    _used, total = memory_mb()
    return {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_cores": os.cpu_count() or 0,
        "memory_total_mb": total,
        "version": __version__,
        "started_at": time.time(),
    }
