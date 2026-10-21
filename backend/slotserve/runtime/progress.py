"""Read loading progress out of a model server's own log output.

llama.cpp prints one dot per percent while it reads the weights; vLLM and SGLang print tqdm bars such as
``Loading safetensors checkpoint shards:  50% Completed | 2/4``. Known log lines also tell which stage the
server is in. Anything unrecognised simply yields no percentage, and callers fall back to the time estimate.
"""

import re
from dataclasses import dataclass

# Stages in the order a server goes through them; the last one seen in the log wins.
_WEIGHTS = (
    r"load_tensors|llama_model_loader|loading model"
    r"|Loading (safetensors|weights|model weights)|Load weight begin"
)
_CONTEXT = r"llama_context|llama_kv_cache|KV cache|Memory profiling|max_total_num_tokens|init_memory_pool"
_WARMUP = r"warming up|warmup|Capturing (CUDA|cuda) graph|cuda graph"
_READY = r"server is listening|model loaded|Uvicorn running|Application startup complete|The server is fired up"
STAGES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (("weights", _WEIGHTS), ("context", _CONTEXT), ("warmup", _WARMUP), ("ready", _READY))
)
DOTS = re.compile(r"^\s*(\.+)\s*$")
TQDM = re.compile(r"(?:Loading|shards|Load weight|Capturing)[^%\n]*?(\d{1,3})%")


@dataclass(frozen=True)
class LogProgress:
    stage: str | None  # weights | context | warmup | ready, None when nothing recognisable was logged
    percent: float | None  # progress of the weights stage, from the log


def parse_progress(lines: list[str]) -> LogProgress:
    """``lines`` is the output of the current launch only."""
    stage: str | None = None
    percent: float | None = None
    dots = 0
    for line in lines:
        for name, pattern in STAGES:
            if pattern.search(line):
                stage = name
        if m := DOTS.match(line):
            dots += len(m.group(1))
            percent = float(min(dots, 100))
        elif matches := TQDM.findall(line):
            percent = float(min(int(matches[-1]), 100))
    if stage in ("context", "warmup", "ready"):
        percent = 100.0
    return LogProgress(stage=stage, percent=percent)
