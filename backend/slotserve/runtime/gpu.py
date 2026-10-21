"""GPU metrics via ``nvidia-smi`` (absent on machines without an NVIDIA driver)."""

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

from ..schemas import GpuOut

_QUERY = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
_CACHE_SECONDS = 3.0
_cache: tuple[float, list[GpuOut]] = (0.0, [])


def _num(value: str, cast=int, default=0):
    try:
        return cast(float(value))
    except ValueError:
        return default


# Where nvidia-smi lives when it is not on the PATH: WSL2 (Docker Desktop on Windows mounts the Windows driver's
# tools into /usr/lib/wsl/lib) and the NVIDIA container toolkit's default.
FALLBACK_PATHS = ("/usr/lib/wsl/lib/nvidia-smi", "/usr/bin/nvidia-smi")


def find_nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe is not None:
        return exe
    return next((p for p in FALLBACK_PATHS if Path(p).is_file()), None)


def _query() -> list[GpuOut]:
    exe = find_nvidia_smi()
    if exe is None:
        return []
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        gpus.append(
            GpuOut(
                index=_num(parts[0]),
                name=parts[1],
                memory_used_mb=_num(parts[2]),
                memory_total_mb=_num(parts[3]),
                utilization_pct=_num(parts[4]),
                temperature_c=_num(parts[5]),
                power_w=_num(parts[6], float, None),
            )
        )
    return gpus


async def gpu_stats() -> list[GpuOut]:
    global _cache
    ts, cached = _cache
    if time.monotonic() - ts < _CACHE_SECONDS:
        return cached
    gpus = await asyncio.to_thread(_query)
    _cache = (time.monotonic(), gpus)
    return gpus
