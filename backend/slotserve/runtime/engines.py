"""Inference engines: how to start a model's server process and how to tell when it is ready.

Every engine serves one model per process behind an OpenAI-compatible API, so scheduling,
routing and usage accounting are the same for all of them.
"""

import os
import re
import shlex
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
    # Launch-argument policy (tables and ``review_args`` below): flags members may set themselves, flags whose
    # value is a file that must lie in a model library, and flags even admins only set after a confirmation.
    user_allowed: frozenset[str] = field(default=frozenset())
    path_flags: frozenset[str] = field(default=frozenset())
    denied: frozenset[str] = field(default=frozenset())
    # Python argparse front ends accept unambiguous prefixes (``--trust-remote`` = ``--trust-remote-code``).
    abbreviates: bool = False

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


# ---- launch-argument tables -------------------------------------------------------------
#
# Engine processes run with SlotServe's privileges, so a launch argument can mean code execution (vLLM/SGLang
# ``--trust-remote-code``, plugin modules) or writing arbitrary files (llama.cpp ``--log-file``,
# ``--slot-save-path``). Members may only use the tuning flags listed as allowed; anything else waits for an
# admin (see ``review_args``). Node agents are meant to check the same tables again later, so keep them data.


def _flags(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Flags nobody sets without an explicit confirmation (admins included), on top of DENIED_SUFFIXES/PREFIXES.
COMMON_DENIED = _flags(
    "--trust-remote-code --api-key --chat-template-file --download-dir --slot-save-path --path --media-path"
)
DENIED_SUFFIXES = ("-file", "-path", "-paths", "-dir", "-plugin", "-cls", "-module", "-modules")
DENIED_PREFIXES = ("--log", "--ssl")

LLAMA_ALLOWED = _flags(
    """
    -c --ctx-size -n --n-predict --predict -ngl --gpu-layers --n-gpu-layers -np --parallel -fa --flash-attn
    -ctk --cache-type-k -ctv --cache-type-v -ts --tensor-split -sm --split-mode -mg --main-gpu -dev --device
    -t --threads -tb --threads-batch -b --batch-size -ub --ubatch-size -cb --cont-batching -nocb --no-cont-batching
    --temp --top-k --top-p --min-p --typical --repeat-penalty --repeat-last-n --presence-penalty
    --frequency-penalty --dry-multiplier -s --seed --jinja --no-jinja --chat-template --reasoning-format
    --reasoning-budget --embedding --embeddings --pooling --rerank --reranking --mlock --no-mmap --numa
    -ot --override-tensor -cmoe --cpu-moe -ncmoe --n-cpu-moe --rope-scaling --rope-scale --rope-freq-base
    --rope-freq-scale --yarn-orig-ctx --yarn-ext-factor --yarn-attn-factor --yarn-beta-fast --yarn-beta-slow
    --cache-reuse --swa-full -kvu --kv-unified -nkvo --no-kv-offload -dt --defrag-thold --keep
    --context-shift --no-context-shift --no-mmproj-offload --no-webui --metrics --slots --no-slots --props
    -to --timeout --threads-http --draft-max --draft --draft-n --draft-min --draft-n-min --draft-p-min
    -ngld --gpu-layers-draft --n-gpu-layers-draft -cd --ctx-size-draft -devd --device-draft
    --no-warmup -cram --cache-ram -v --verbose
    """
)
# Flags whose value is a file: members may use them for files inside a model library.
LLAMA_PATHS = _flags("-mm --mmproj --lora -md --model-draft")
LLAMA_DENIED = _flags(
    "-hf -hfr --hf-repo -hff --hf-file -hft --hf-token -hfd -hfrd --hf-repo-draft -hfv --hf-repo-v "
    "-mu --model-url -dr --docker-repo -mmu --mmproj-url --rpc --webui-config"
)

VLLM_ALLOWED = _flags(
    """
    --max-model-len --gpu-memory-utilization -tp --tensor-parallel-size -pp --pipeline-parallel-size
    -dp --data-parallel-size --enable-expert-parallel --dtype -q --quantization --max-num-seqs
    --max-num-batched-tokens --kv-cache-dtype --enable-prefix-caching --no-enable-prefix-caching
    --enable-chunked-prefill --no-enable-chunked-prefill --enforce-eager --swap-space --cpu-offload-gb
    --block-size --seed --max-logprobs --enable-auto-tool-choice --tool-call-parser --reasoning-parser
    --tokenizer-mode --config-format --load-format --runner --task --convert --limit-mm-per-prompt
    --max-seq-len-to-capture --disable-log-stats --disable-log-requests --override-generation-config
    --rope-scaling --rope-theta --async-scheduling --max-num-partial-prefills --disable-sliding-window
    --mm-processor-cache-gb --disable-custom-all-reduce --uvicorn-log-level
    """
)
VLLM_DENIED = _flags(
    "--middleware --logits-processors --logits-processor-pattern --chat-template --tokenizer --code-revision "
    "--allowed-local-media-path --distributed-executor-backend --model-loader-extra-config --lora-modules "
    "--hf-overrides --uds --kv-transfer-config --speculative-config"
)

SGLANG_ALLOWED = _flags(
    """
    --context-length --tp --tp-size --tensor-parallel-size --dp-size --data-parallel-size --ep-size
    --mem-fraction-static --max-running-requests --max-total-tokens --max-prefill-tokens --chunked-prefill-size
    --dtype --quantization --kv-cache-dtype --attention-backend --sampling-backend --grammar-backend
    --disable-radix-cache --enable-torch-compile --cuda-graph-max-bs --disable-cuda-graph --schedule-policy
    --schedule-conservativeness --reasoning-parser --tool-call-parser --is-embedding --random-seed
    --enable-mixed-chunk --enable-dp-attention --stream-interval --page-size --enable-metrics
    --load-format --tokenizer-mode
    """
)
SGLANG_DENIED = _flags(
    "--chat-template --enable-custom-logit-processor --json-model-override-args --tokenizer --lora-paths "
    "--speculative-draft-model-path"
)

ENGINES: dict[str, Engine] = {
    "llama.cpp": Engine(
        "llama.cpp",
        "llama.cpp",
        True,
        frozenset({"-m", "--model", "--host", "--port", "-a", "--alias"}),
        LLAMA_ALLOWED,
        LLAMA_PATHS,
        COMMON_DENIED | LLAMA_DENIED,
    ),
    "vllm": Engine(
        "vllm",
        "vLLM",
        False,
        frozenset({"--model", "--host", "--port", "--served-model-name"}),
        VLLM_ALLOWED,
        frozenset(),
        COMMON_DENIED | VLLM_DENIED,
        abbreviates=True,
    ),
    "sglang": Engine(
        "sglang",
        "SGLang",
        False,
        frozenset({"--model-path", "--model", "--host", "--port", "--served-model-name"}),
        SGLANG_ALLOWED,
        frozenset(),
        COMMON_DENIED | SGLANG_DENIED,
        abbreviates=True,
    ),
    # Its command line is whatever the admin configured; members may not use it at all (api/models.py).
    "custom": Engine("custom", "Custom", False, frozenset(), frozenset(), frozenset(), COMMON_DENIED),
}
DEFAULT_ENGINE = "llama.cpp"


def engine_for(model) -> Engine:
    return ENGINES.get(getattr(model, "engine", None) or DEFAULT_ENGINE, ENGINES[DEFAULT_ENGINE])


# ---- launch-argument policy -------------------------------------------------------------

FLAG_RE = re.compile(r"^--?[A-Za-z]")
POSITIONAL = "<positional>"
# A Hugging Face repository id: owner/name, neither part starting with a dot (no "..", no absolute paths).
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


@dataclass(frozen=True)
class Flag:
    name: str
    values: tuple[str, ...] = ()


def parse_flags(text: str) -> list[Flag]:
    """Groups launch arguments into flags with the values that follow them (``--a=b`` counts as ``--a b``).

    Arity is unknown, so every non-flag token belongs to the flag before it; tokens before the first flag are
    reported as ``<positional>``. Raises ValueError on unbalanced quotes.
    """
    out: list[Flag] = []
    name: str | None = None
    values: list[str] = []
    for token in split_args(text or ""):
        if FLAG_RE.match(token):
            if name is not None or values:
                out.append(Flag(name or POSITIONAL, tuple(values)))
            name, eq, inline = token.partition("=")
            values = [inline] if eq else []
        else:
            values.append(token)
    if name is not None or values:
        out.append(Flag(name or POSITIONAL, tuple(values)))
    return out


def is_denied(engine: Engine, flag: str) -> bool:
    if flag in engine.denied:
        return True
    if not flag.startswith("--"):
        return False
    if flag.endswith(DENIED_SUFFIXES) or flag.startswith(DENIED_PREFIXES):
        return True
    # an abbreviation of a denied flag (argparse front ends), unless it is itself an allowed flag
    return engine.abbreviates and flag not in engine.user_allowed and any(d.startswith(flag) for d in engine.denied)


def inside(path: str | Path, roots: Sequence[Path], exclude: Sequence[Path] = ()) -> bool:
    """The path resolves to somewhere under one of ``roots`` (and under none of ``exclude``)."""
    try:
        resolved = Path(path).expanduser().resolve()
        if any(resolved.is_relative_to(Path(x).resolve()) for x in exclude):
            return False
        return any(resolved.is_relative_to(Path(r).resolve()) for r in roots)
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass
class ArgReview:
    # Flags a member may not set without an admin's approval (order of appearance, no duplicates).
    needs_approval: list[str] = field(default_factory=list)
    # The subset that even an admin only sets after an explicit confirmation.
    denied: list[str] = field(default_factory=list)


def review_args(
    engine: Engine,
    args: str,
    roots: Sequence[Path],
    approved: Iterable[str] = (),
    exclude: Sequence[Path] = (),
) -> ArgReview:
    """Which of ``args`` need an admin.

    Flags set with the same values in ``approved`` (arguments an admin already accepted for this model or
    preset) are not asked about again; flags SlotServe sets itself are stripped at launch and so ignored.
    """
    known = {f for text in approved for f in parse_flags(text)}
    review = ArgReview()
    for flag in parse_flags(args):
        if flag in known or flag.name in engine.managed:
            continue
        if is_denied(engine, flag.name):
            review.denied.append(flag.name)
            review.needs_approval.append(flag.name)
        elif flag.name in engine.path_flags:
            if not flag.values or not all(inside(v, roots, exclude) for v in flag.values):
                review.needs_approval.append(flag.name)
        elif flag.name not in engine.user_allowed:
            review.needs_approval.append(flag.name)
    review.needs_approval = list(dict.fromkeys(review.needs_approval))
    review.denied = list(dict.fromkeys(review.denied))
    return review


def member_source_ok(path: str, roots: Sequence[Path], exclude: Sequence[Path] = ()) -> bool:
    """Where a member may point a model: inside a model library, or a Hugging Face repository id that is not
    also a local path."""
    path = path.strip()
    if inside(path, roots, exclude):
        return True
    return bool(REPO_ID_RE.match(path)) and not Path(path).exists()
