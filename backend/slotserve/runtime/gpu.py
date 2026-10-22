"""GPU metrics via ``nvidia-smi`` (absent on machines without an NVIDIA driver)."""

import asyncio
import shutil
import subprocess
import time

from ..schemas import GpuOut

_QUERY = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
_CACHE_SECONDS = 3.0
_cache: tuple[float, list[GpuOut]] = (0.0, [])


def _num(value: str, cast=int, default=0):
    try:
        return cast(float(value))
    except ValueError:
        return default


def parse_query_output(out: str) -> list[GpuOut]:
    """Parses ``nvidia-smi --query-gpu=<_QUERY> --format=csv,noheader,nounits`` output.

    Fields a GPU or driver does not report (``[N/A]``, ``[Not Supported]``) read as 0 (power: ``None``);
    lines that are not GPU rows are skipped.
    """
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


def _query() -> list[GpuOut]:
    exe = shutil.which("nvidia-smi")
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
    return parse_query_output(out)


async def gpu_stats() -> list[GpuOut]:
    global _cache
    ts, cached = _cache
    if time.monotonic() - ts < _CACHE_SECONDS:
        return cached
    gpus = await asyncio.to_thread(_query)
    _cache = (time.monotonic(), gpus)
    return gpus
