"""The model catalog: curated open models with ready-to-download variants, memory estimates and lane fit.

Built-in entries live in ``slotserve/resources/model_catalog.json`` (refreshed and verified against the
Hugging Face API with ``tools/update_model_catalog.py``). Admins can add their own entries (stored in
the database) and hide built-in ones.
"""

import fnmatch
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..schemas import GpuOut

CATALOG_FILE = Path(__file__).resolve().parent.parent / "resources" / "model_catalog.json"

TASKS = ("chat", "code", "reasoning", "embedding", "vision")
SLUG = r"^[a-z0-9][a-z0-9._-]{0,63}$"
REPO = r"^[\w.\-]+/[\w.\-]+$"
DEFAULT_CTX = 8192
GIB = 1024**3

# Files of a safetensors repository that an engine needs (anything else, e.g. original checkpoints, is skipped).
_KEEP_EXT = (".json", ".safetensors", ".txt", ".model", ".tiktoken", ".jinja", ".py", ".vocab")
_SKIP = ("consolidated*.safetensors", "*.md", ".gitattributes", "*.onnx")


class Arch(BaseModel):
    """What the KV cache size depends on (decoder models only)."""

    layers: int = Field(ge=1, le=1000)
    kv_heads: int = Field(ge=1, le=1000)
    head_dim: int = Field(ge=1, le=4096)


class Variant(BaseModel):
    id: str = Field(pattern=SLUG)
    format: Literal["gguf", "safetensors"]
    repo: str = Field(pattern=REPO)
    revision: str = Field(default="main", min_length=1, max_length=100)
    quant: str = Field(min_length=1, max_length=32)  # Q4_K_M, Q8_0, BF16, FP8, AWQ, MXFP4, ...
    # gguf: every file of the model (split parts in order); safetensors: resolved from the repository at deploy time
    files: list[str] = Field(default_factory=list, max_length=64)
    size_bytes: int = Field(default=0, ge=0)
    file_count: int | None = None  # safetensors: number of files a deploy downloads
    mmproj: str | None = Field(default=None, max_length=300)  # vision projector for llama.cpp
    mmproj_size_bytes: int = Field(default=0, ge=0)
    gated: bool = False  # the repository needs a Hugging Face token with accepted licence terms
    # safetensors: extra globs to download / skip on top of the defaults
    include: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("files")
    @classmethod
    def _files(cls, files: list[str]) -> list[str]:
        for f in files:
            if not _safe_repo_path(f) or not f.lower().endswith(".gguf"):
                raise ValueError(f"invalid file {f!r}")
        return files

    @model_validator(mode="after")
    def _check(self):
        if self.format == "gguf" and not self.files:
            raise ValueError("a gguf variant needs files")
        if self.format == "safetensors" and (self.files or self.mmproj):
            raise ValueError("a safetensors variant lists no files")
        if self.mmproj and (not _safe_repo_path(self.mmproj) or not self.mmproj.lower().endswith(".gguf")):
            raise ValueError("invalid mmproj file")
        return self

    @property
    def engines(self) -> list[str]:
        return ["llama.cpp"] if self.format == "gguf" else ["vllm", "sglang"]

    @property
    def download_bytes(self) -> int:
        return self.size_bytes + self.mmproj_size_bytes


class Text(BaseModel):
    en: str = Field(default="", max_length=1000)
    zh: str = Field(default="", max_length=1000)


class Entry(BaseModel):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=80)
    family: str = Field(default="", max_length=40)
    publisher: str = Field(default="", max_length=60)
    license: str = Field(default="", max_length=60)
    tasks: list[str] = Field(min_length=1, max_length=5)
    params_b: float = Field(gt=0, le=10_000)  # total parameters, billions
    active_params_b: float | None = Field(default=None, gt=0)  # mixture-of-experts: parameters used per token
    context_length: int = Field(ge=256, le=10_000_000)
    default_ctx: int | None = Field(default=None, ge=256)
    tags: list[str] = Field(default_factory=list, max_length=12)
    description: Text = Field(default_factory=Text)
    source_repo: str | None = Field(default=None, pattern=REPO)  # the original model (for its model card)
    config_repo: str | None = Field(default=None, pattern=REPO)  # where update_model_catalog.py reads config.json
    arch: Arch | None = None
    extra_args: dict[str, str] = Field(default_factory=dict)  # per engine, appended to the defaults
    variants: list[Variant] = Field(min_length=1, max_length=20)

    @field_validator("tasks")
    @classmethod
    def _tasks(cls, tasks: list[str]) -> list[str]:
        bad = [t for t in tasks if t not in TASKS]
        if bad:
            raise ValueError(f"unknown task {bad[0]!r}")
        return tasks

    @field_validator("extra_args")
    @classmethod
    def _extra(cls, value: dict[str, str]) -> dict[str, str]:
        if any(k not in ("llama.cpp", "vllm", "sglang") for k in value):
            raise ValueError("extra_args keys are engines")
        return value

    @model_validator(mode="after")
    def _unique(self):
        ids = [v.id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("variant ids must be unique")
        return self

    @property
    def ctx(self) -> int:
        return min(self.default_ctx or DEFAULT_CTX, self.context_length)

    def variant(self, variant_id: str) -> Variant | None:
        return next((v for v in self.variants if v.id == variant_id), None)


class CatalogFile(BaseModel):
    version: int = 1
    updated: str = ""
    entries: list[Entry]

    @model_validator(mode="after")
    def _unique(self):
        ids = [e.id for e in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("entry ids must be unique")
        return self


def _safe_repo_path(path: str) -> bool:
    return bool(re.fullmatch(r"[\w.\-/ ]+", path)) and ".." not in path.split("/") and not path.startswith("/")


def load_file(path: Path) -> CatalogFile:
    return CatalogFile.model_validate(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def builtin() -> CatalogFile:
    return load_file(CATALOG_FILE)


# ---- repository files ------------------------------------------------------------


def wanted_file(path: str, variant: Variant) -> bool:
    """Whether a file of a safetensors repository belongs to the download."""
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(path, p) for p in variant.include):
        return True
    if any(fnmatch.fnmatch(path, p) for p in variant.exclude):
        return False
    if "/" in path or not name.lower().endswith(_KEEP_EXT):
        return False  # sub-folders hold original checkpoints, other runtimes' formats, images...
    return not any(fnmatch.fnmatch(name, p) for p in _SKIP)


def select_files(tree: list[dict], variant: Variant) -> list[tuple[str, int]]:
    """``(path, size)`` of the files to download from a repository listing (Hugging Face ``tree`` API)."""
    out = []
    for item in tree:
        path = item.get("path", "")
        if item.get("type") == "file" and _safe_repo_path(path) and wanted_file(path, variant):
            out.append((path, int((item.get("lfs") or {}).get("size") or item.get("size") or 0)))
    return sorted(out)


def local_dir_name(repo: str) -> str:
    """Folder (inside the models directory) a safetensors repository downloads to."""
    return repo.replace("/", "--")


# ---- memory estimate and fit -----------------------------------------------------


def kv_cache_bytes(arch: Arch | None, ctx: int) -> int:
    """Keys and values in 16-bit for every layer (an upper bound for sliding-window models)."""
    if arch is None:
        return 0
    return 2 * arch.layers * arch.kv_heads * arch.head_dim * ctx * 2


def estimate(entry: Entry, variant: Variant, ctx: int | None = None) -> dict:
    """GPU memory needed to serve the variant with ``ctx`` tokens of context (one sequence)."""
    ctx = min(ctx or entry.ctx, entry.context_length)
    weights = variant.download_bytes
    kv = kv_cache_bytes(entry.arch, ctx)
    # llama.cpp: compute buffers and CUDA context; vLLM / SGLang: activations, CUDA graphs, sampler.
    base, share = (0.5, 0.03) if variant.format == "gguf" else (1.5, 0.05)
    overhead = int(base * GIB + share * weights)
    return {"ctx": ctx, "weights": weights, "kv_cache": kv, "overhead": overhead, "total": weights + kv + overhead}


def usable_fraction(variant: Variant) -> float:
    # vLLM / SGLang reserve a fixed share of GPU memory (--gpu-memory-utilization 0.9 by default).
    return 0.95 if variant.format == "gguf" else 0.9


def lane_memory(devices: list[int] | None, gpus: list[GpuOut]) -> int | None:
    """Total memory of a lane's GPUs in bytes (``None`` when unknown)."""
    if not gpus:
        return None
    if devices:
        by_index = {g.index: g for g in gpus}
        if any(d not in by_index for d in devices):
            return None
        chosen = [by_index[d] for d in devices]
    else:
        chosen = gpus  # a lane without explicit GPUs sees all of them
    total = sum(g.memory_total_mb for g in chosen) * 1024 * 1024
    return total or None


def fit(total_needed: int, memory: int | None, variant: Variant) -> str:
    """fits | tight (only with a shorter context or partial offload) | no | unknown."""
    if memory is None:
        return "unknown"
    if total_needed <= memory * usable_fraction(variant):
        return "fits"
    if total_needed <= memory:
        return "tight"
    return "no"


# ---- launch arguments ------------------------------------------------------------


def launch_args(entry: Entry, engine: str, ctx: int, gpu_count: int = 1, mmproj_path: str | None = None) -> str:
    """Sensible defaults for a freshly deployed model; people can edit them afterwards."""
    if engine == "llama.cpp":
        parts = ["-ngl 99", f"-c {ctx}"]
        if mmproj_path:
            parts.append(f'--mmproj "{mmproj_path}"' if " " in mmproj_path else f"--mmproj {mmproj_path}")
    elif engine == "vllm":
        parts = [f"--max-model-len {ctx}"]
        if gpu_count > 1:
            parts.append(f"--tensor-parallel-size {gpu_count}")
    else:  # sglang
        parts = [f"--context-length {ctx}"]
        if gpu_count > 1:
            parts.append(f"--tp-size {gpu_count}")
    if extra := entry.extra_args.get(engine, "").strip():
        parts.append(extra)
    return " ".join(parts)


def model_name(entry: Entry, variant: Variant) -> str:
    return f"{entry.id}-{variant.id}"
