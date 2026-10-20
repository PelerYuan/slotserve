"""Inference engines: how to start a model's server process and how to tell when it is ready.

Every engine serves one model per process behind an OpenAI-compatible API, so scheduling,
routing and usage accounting are the same for all of them.
"""

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings


def split_args(text: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(text)
    # Non-POSIX mode keeps backslashes in Windows paths but also keeps the quotes.
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(text, posix=False)]


def strip_flags(args: list[str], managed: frozenset[str]) -> list[str]:
    """Drops flags SlotServe sets itself (with their value), in both ``--flag value`` and ``--flag=value`` form."""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        flag = arg.split("=", 1)[0]
        if flag in managed:
            skip = "=" not in arg
            continue
        out.append(arg)
    return out


def _resolve(base: list[str]) -> list[str]:
    """Absolute path for the executable when it is a relative file or found on PATH."""
    if base:
        exe = Path(base[0])
        if exe.exists():
            base[0] = str(exe.resolve())
        elif found := shutil.which(base[0]):
            base[0] = found
    return base


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    # The model is a local GGUF file (scanned/downloaded) rather than a directory or repository id.
    uses_gguf: bool
    managed: frozenset[str]

    def base_command(self, settings: Settings) -> list[str]:
        cmd = {
            "llama.cpp": settings.llama_server_cmd,
            "vllm": settings.vllm_cmd,
            "sglang": settings.sglang_cmd,
            "custom": settings.custom_engine_cmd,
        }[self.id]
        return _resolve(split_args(cmd))

    def health_path(self, settings: Settings) -> str:
        return settings.custom_engine_health if self.id == "custom" else "/health"

    def command(self, settings: Settings, model_path: str, name: str, args: str, host: str, port: int) -> list[str]:
        user = strip_flags(split_args(args or ""), self.managed)
        base = self.base_command(settings)
        if self.id == "llama.cpp":
            return [*base, "-m", model_path, "--host", host, "--port", str(port), "--alias", name, *user]
        if self.id == "vllm":
            return [*base, model_path, "--host", host, "--port", str(port), "--served-model-name", name, *user]
        if self.id == "sglang":
            return [
                *base,
                "--model-path",
                model_path,
                "--host",
                host,
                "--port",
                str(port),
                "--served-model-name",
                name,
                *user,
            ]
        values = {"model": model_path, "host": host, "port": str(port), "name": name}
        return [part.format_map(values) for part in base] + user


ENGINES: dict[str, Engine] = {
    "llama.cpp": Engine(
        "llama.cpp", "llama.cpp", True, frozenset({"-m", "--model", "--host", "--port", "-a", "--alias"})
    ),
    "vllm": Engine("vllm", "vLLM", False, frozenset({"--model", "--host", "--port", "--served-model-name"})),
    "sglang": Engine(
        "sglang", "SGLang", False, frozenset({"--model-path", "--model", "--host", "--port", "--served-model-name"})
    ),
    "custom": Engine("custom", "Custom", False, frozenset()),
}
DEFAULT_ENGINE = "llama.cpp"


def engine_for(model) -> Engine:
    return ENGINES.get(getattr(model, "engine", None) or DEFAULT_ENGINE, ENGINES[DEFAULT_ENGINE])
