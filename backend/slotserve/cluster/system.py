"""What a machine has (CPU, memory, disks, Docker, GPUs) and how busy it is, without extra dependencies.

Everything here is best effort and degrades to ``None`` / empty: a node without GPUs (no ``nvidia-smi``), without
Docker access or on an operating system without ``/proc`` still reports what it can. The parsers are pure functions
so they can be tested with output captured on other machines.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .. import __version__

# ---- parsers (pure) ------------------------------------------------------------------------------------------------


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """``/proc/meminfo`` -> (used, total) in MiB."""
    info: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        with contextlib.suppress(ValueError, IndexError):
            info[key.strip()] = int(value.split()[0])  # kB
    total = info.get("MemTotal")
    if not total:
        return None, None
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return (total - available) // 1024, total // 1024


def parse_cpuinfo(text: str) -> dict[str, Any]:
    """``/proc/cpuinfo`` -> {model, physical_cores, logical_cores} (None where the file does not say)."""
    model: str | None = None
    logical = 0
    cores: set[tuple[str, str]] = set()
    physical_id: str | None = None
    core_id: str | None = None
    for line in [*text.splitlines(), ""]:
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not key:  # a blank line ends one logical CPU
            if core_id is not None:
                cores.add((physical_id or "0", core_id))
            physical_id = core_id = None
            continue
        if key == "processor":
            logical += 1
        elif key in ("model name", "cpu model", "hardware") and model is None and value:
            model = value
        elif key == "physical id":
            physical_id = value
        elif key == "core id":
            core_id = value
    return {"model": model, "physical_cores": len(cores) or None, "logical_cores": logical or None}


def parse_net_dev(text: str) -> tuple[int, int]:
    """``/proc/net/dev`` -> (received, sent) bytes over every interface except loopback."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name.strip() == "lo" or len(fields) < 9:
            continue
        with contextlib.suppress(ValueError):
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def parse_cuda_version(text: str) -> str | None:
    """The CUDA version from the header plain ``nvidia-smi`` prints (``CUDA Version: 12.4``)."""
    found = re.search(r"CUDA Version:\s*([0-9.]+)", text)
    return found.group(1) if found else None


def docker_summary(info: dict | None) -> dict[str, Any]:
    """What Docker's ``/info`` says about running containers here (``available`` False without access)."""
    if not info:
        return {"available": False}
    runtimes = sorted((info.get("Runtimes") or {}).keys())
    return {
        "available": True,
        "version": info.get("ServerVersion"),
        "root_dir": info.get("DockerRootDir"),
        "runtimes": runtimes,
        "default_runtime": info.get("DefaultRuntime"),
        "gpu_runtime": "nvidia" in runtimes or info.get("DefaultRuntime") == "nvidia",
        "cpus": info.get("NCPU"),
        "memory_bytes": info.get("MemTotal"),
        "os": info.get("OperatingSystem"),
    }


# ---- live readings -------------------------------------------------------------------------------------------------


def memory_mb() -> tuple[int | None, int | None]:
    """(used, total) RAM in MiB, or (None, None) when unknown."""
    try:
        if sys.platform.startswith("linux"):
            return parse_meminfo(Path("/proc/meminfo").read_text())
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


class NetMeter:
    """Network throughput (bytes per second received / sent) between two calls; Linux only, else None."""

    def __init__(self):
        self._last = self._read()

    @staticmethod
    def _read() -> tuple[float, int, int] | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            rx, tx = parse_net_dev(Path("/proc/net/dev").read_text())
        except OSError:
            return None
        return time.monotonic(), rx, tx

    def rates(self) -> tuple[float | None, float | None]:
        now = self._read()
        last, self._last = self._last, now
        if now is None or last is None or now[0] <= last[0]:
            return None, None
        seconds = now[0] - last[0]
        return round(max(0, now[1] - last[1]) / seconds, 1), round(max(0, now[2] - last[2]) / seconds, 1)


def disk(label: str, path: str | Path | None) -> dict[str, Any] | None:
    """{label, path, total_bytes, free_bytes} of the file system holding ``path`` (None when unreadable)."""
    if not path:
        return None
    target = Path(path)
    with contextlib.suppress(OSError):
        while not target.exists() and target.parent != target:
            target = target.parent  # not created yet: its nearest existing parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    return {"label": label, "path": str(path), "total_bytes": usage.total, "free_bytes": usage.free}


class LoadProbe:
    """Live load of this machine: CPU %, memory, load average, network and free disk space."""

    def __init__(self):
        self.cpu = CpuMeter()
        self.net = NetMeter()

    def read(self, disks: list[dict] | None = None) -> dict[str, Any]:
        used, total = memory_mb()
        load = None
        if hasattr(os, "getloadavg"):
            with contextlib.suppress(OSError):
                load = os.getloadavg()[0]
        rx, tx = self.net.rates()
        out: dict[str, Any] = {
            "cpu_percent": self.cpu.percent(),
            "memory_used_mb": used,
            "memory_total_mb": total,
            "load": load,
            "net_rx_bps": rx,
            "net_tx_bps": tx,
        }
        if disks:
            out["disks"] = [d for d in (disk(x["label"], x["path"]) for x in disks) if d]
        return out


# ---- static facts --------------------------------------------------------------------------------------------------


def _windows_cpu_model() -> str | None:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or None
    except (OSError, ImportError):
        return None


def _windows_physical_cores() -> int | None:
    """Counts the RelationProcessorCore entries of GetLogicalProcessorInformation."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        size = ctypes.c_ulong(0)
        kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not kernel32.GetLogicalProcessorInformation(buf, ctypes.byref(size)):
            return None
        pointer = ctypes.sizeof(ctypes.c_void_p)
        entry = 32 if pointer == 8 else 24  # sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION)
        raw = buf.raw[: size.value]
        # the Relationship field follows the processor mask; RelationProcessorCore is 0
        return sum(1 for i in range(0, len(raw) - entry + 1, entry) if raw[i + pointer] == 0) or None
    except (OSError, AttributeError, ValueError):
        return None


def cpu_info() -> dict[str, Any]:
    """{model, physical_cores, logical_cores} of this machine."""
    out: dict[str, Any] = {"model": None, "physical_cores": None, "logical_cores": os.cpu_count()}
    try:
        if sys.platform.startswith("linux"):
            parsed = parse_cpuinfo(Path("/proc/cpuinfo").read_text(errors="replace"))
            out.update({k: v for k, v in parsed.items() if v})
        elif os.name == "nt":
            out["model"] = _windows_cpu_model()
            out["physical_cores"] = _windows_physical_cores()
    except OSError:
        pass
    if not out["model"]:
        out["model"] = platform.processor() or None
    return out


_driver_cache: dict[str, Any] | None = None


def gpu_driver() -> dict[str, Any] | None:
    """{vendor, driver_version, cuda_version} of the GPU driver, or None without one (no ``nvidia-smi``)."""
    global _driver_cache
    if _driver_cache is not None:
        return _driver_cache or None
    exe = shutil.which("nvidia-smi")
    if exe is None:
        _driver_cache = {}
        return None
    found: dict[str, Any] = {"vendor": "nvidia", "driver_version": None, "cuda_version": None}
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(
            [exe, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        found["driver_version"] = next(iter(out.strip().splitlines()), None)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        header = subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout
        found["cuda_version"] = parse_cuda_version(header)
    _driver_cache = found
    return found


DockerInfo = Callable[[], Awaitable[dict | None]]


async def hardware(models_dir: str | Path | None, docker_info: DockerInfo | None = None) -> dict[str, Any]:
    """Everything static about this machine: CPU, memory, disks, operating system, Docker and the GPU driver.

    ``docker_info`` returns Docker's ``/info`` (or None); a node agent may run without access to the Docker socket.
    The GPUs themselves are reported separately (``runtime.gpu``), with live numbers.
    """
    docker: dict | None = None
    if docker_info is not None:
        with contextlib.suppress(Exception):
            docker = await asyncio.wait_for(docker_info(), 5)
    summary = docker_summary(docker)
    cpu = await asyncio.to_thread(cpu_info)
    _used, total = memory_mb()
    disks = [d for d in (disk("models", models_dir), disk("docker", summary.get("root_dir"))) if d]
    return {
        "cpu_model": cpu["model"],
        "cpu_physical_cores": cpu["physical_cores"],
        "cpu_logical_cores": cpu["logical_cores"],
        "memory_total_mb": total,
        "os": f"{platform.system()} {platform.release()}".strip(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "disks": disks,
        "docker": summary,
        "gpu_driver": await asyncio.to_thread(gpu_driver),
    }


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
