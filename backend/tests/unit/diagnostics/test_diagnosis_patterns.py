"""Error pattern library (services/diagnostics): every pattern against real log samples in tests/samples/logs/.

Samples are named ``<pattern id>-<variant>.log`` (``unknown-*.log`` must stay unrecognized). They reproduce the
output of llama.cpp, vLLM, uvicorn, Docker, mount.nfs/mount.cifs and SlotServe itself; replace them with captures
from real machines as the sample library grows (tools/verify). A new pattern needs a sample and a row in EXPECTED.
"""

import json
import re
import time
import tomllib
from pathlib import Path

import pytest

from slotserve.services import diagnostics
from slotserve.services.diagnostics import LOCALES, catalog, diagnose, pattern_ids
from slotserve.services.diagnostics.matcher import PATTERNS_FILE, library, placeholders, scrub

# sample -> (pattern id, expected params (subset), error message, exit code)
EXPECTED: dict[str, tuple[str, dict[str, str], str | None, int | None]] = {
    "port_in_use-llamacpp-linux.log": ("port_in_use", {"port": "8081"}, None, 1),
    "port_in_use-uvicorn-windows-10048.log": (
        "port_in_use",
        {"port": "54029"},
        "llama.cpp server exited with code 3 while loading",
        3,
    ),
    "port_in_use-vllm-linux-errno98.log": ("port_in_use", {}, None, 1),
    "port_in_use-docker-port-allocated.log": ("port_in_use", {"port": "2203"}, None, None),
    "gpu_out_of_memory-llamacpp-cuda.log": ("gpu_out_of_memory", {"device": "0", "size": "32431.27 MiB"}, None, 1),
    "gpu_out_of_memory-llamacpp-kv-cache.log": ("gpu_out_of_memory", {"device": "0"}, None, 1),
    "gpu_out_of_memory-vllm-torch.log": ("gpu_out_of_memory", {"device": "0", "size": "1.02 GiB"}, None, 1),
    "context_too_long-vllm.log": ("context_too_long", {"max_len": "131072", "fit_len": "77104"}, None, 1),
    "host_out_of_memory-shell-killed.log": ("host_out_of_memory", {}, None, 137),
    "gpu_runtime_missing-docker-nvidia-driver.log": ("gpu_runtime_missing", {"driver": "nvidia"}, None, None),
    "gpu_runtime_missing-nvidia-container-cli.log": ("gpu_runtime_missing", {}, None, 125),
    "cuda_mismatch-docker-unsatisfied-condition.log": ("cuda_mismatch", {"cuda": "12.8"}, None, 125),
    "cuda_mismatch-llamacpp-driver-insufficient.log": (
        "cuda_mismatch",
        {},
        "model did not become ready within 1800s",
        None,
    ),
    "cuda_mismatch-llamacpp-no-kernel-image.log": ("cuda_mismatch", {}, None, -6),
    "engine_library_missing-linux-libcudart.log": ("engine_library_missing", {"library": "libcudart.so.12"}, None, 127),
    "engine_not_executable-spawn-permission.log": (
        "engine_not_executable",
        {"path": "/opt/llama.cpp/build/bin/llama-server"},
        None,
        None,
    ),
    "engine_not_executable-exec-format.log": ("engine_not_executable", {}, None, None),
    "engine_not_found-spawn-linux.log": ("engine_not_found", {"path": "llama-server"}, None, None),
    "engine_not_found-spawn-windows.log": ("engine_not_found", {}, None, None),
    "engine_not_found-docker-exec.log": ("engine_not_found", {"path": "llama-server"}, None, 127),
    "docker_unreachable-socket-missing.log": (
        "docker_unreachable",
        {"endpoint": "unix:///var/run/docker.sock"},
        None,
        None,
    ),
    "docker_unreachable-socket-permission.log": (
        "docker_unreachable",
        {"endpoint": "unix:///var/run/docker.sock"},
        None,
        None,
    ),
    "docker_unreachable-windows-pipe.log": (
        "docker_unreachable",
        {"endpoint": "//./pipe/dockerDesktopLinuxEngine"},
        None,
        None,
    ),
    "image_pull_failed-access-denied.log": ("image_pull_failed", {"image": "slotserve/does-not-exist"}, None, None),
    "image_pull_failed-manifest-unknown.log": (
        "image_pull_failed",
        {"image": "nvidia/cuda:12.4.0-base-ubuntu24.04"},
        None,
        None,
    ),
    "image_pull_failed-registry-timeout.log": ("image_pull_failed", {"registry": "registry-1.docker.io"}, None, None),
    "image_pull_failed-rate-limit.log": ("image_pull_failed", {}, None, None),
    "mount_helper_missing-mount-bad-option.log": ("mount_helper_missing", {}, None, 32),
    "mount_helper_missing-docker-no-such-device.log": ("mount_helper_missing", {}, None, None),
    "mount_access_denied-smb-error13.log": ("mount_access_denied", {}, None, 32),
    "mount_access_denied-docker-cifs.log": ("mount_access_denied", {"share": "//nas.lab.local/models"}, None, None),
    "mount_access_denied-nfs.log": ("mount_access_denied", {"share": "192.168.1.20:/export/datasets"}, None, 32),
    "mount_failed-nfs-timeout.log": ("mount_failed", {}, None, 32),
    "mount_failed-smb-host-down.log": ("mount_failed", {}, None, 32),
    "mount_failed-docker-connection-refused.log": ("mount_failed", {"share": "/srv/scratch"}, None, None),
    "hf_auth-gated.log": ("hf_auth", {"repo": "meta-llama/Llama-3.1-8B-Instruct"}, None, 1),
    "models_permission_denied-llamacpp.log": (
        "models_permission_denied",
        {"path": "/models/gemma-3-27b-it-Q4_K_M.gguf"},
        None,
        1,
    ),
    "models_permission_denied-vllm.log": (
        "models_permission_denied",
        {"path": "/models/Qwen2.5-7B-Instruct/config.json"},
        None,
        1,
    ),
    "model_corrupt-llamacpp-magic.log": ("model_corrupt", {}, None, 1),
    "model_corrupt-llamacpp-truncated.log": ("model_corrupt", {}, None, 1),
    "model_unsupported-llamacpp-arch.log": ("model_unsupported", {"arch": "qwen3next"}, None, 1),
    "model_unsupported-vllm-arch.log": ("model_unsupported", {"arch": "Gemma3nForConditionalGeneration"}, None, 1),
    "model_file_missing-llamacpp.log": (
        "model_file_missing",
        {"path": "/models/old/Llama-3.2-3B-Instruct-Q4_K_M.gguf"},
        None,
        1,
    ),
    "model_file_missing-vllm-config.log": ("model_file_missing", {"path": "/models/Qwen2.5-7B-Instruct"}, None, 1),
    "disk_full-docker-pull.log": ("disk_full", {}, None, None),
    "disk_full-download.log": ("disk_full", {}, None, None),
    "load_timeout-slow-nfs.log": ("load_timeout", {"seconds": "1800"}, "model did not become ready within 1800s", None),
    "file_not_found-custom-engine.log": ("file_not_found", {"path": "/data/configs/engine.yaml"}, None, 1),
    "unknown-mock-fail.log": ("unknown", {}, "llama.cpp server exited with code 1 while loading", 1),
    "unknown-windows-ggml-assert.log": (
        "unknown",
        {},
        "llama.cpp server exited with code 3221226505 while loading",
        3221226505,
    ),
}

CJK = re.compile(r"[一-鿿]")


@pytest.fixture
def logs_dir(samples_dir: Path) -> Path:
    return samples_dir / "logs"


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("sample", sorted(EXPECTED))
def test_sample_is_diagnosed(logs_dir, sample):
    want_id, want_params, error, exit_code = EXPECTED[sample]
    text = _read(logs_dir / sample)
    d = diagnose(text, error=error, exit_code=exit_code)
    assert d.id == want_id, (sample, d.evidence)
    assert d.recognized is (want_id != "unknown")
    assert want_params.items() <= d.params.items(), d.params
    # Nothing hidden: the raw exit code, error and log tail always come along.
    assert d.exit_code == exit_code and d.error == error
    assert d.log_tail == [scrub(line) for line in text.splitlines()][-20:]
    for locale in LOCALES:
        t = d.text[locale]
        assert t.cause and t.steps and "{" not in t.cause and not any("{" in s for s in t.steps)
    assert CJK.search(d.localized("zh-CN").cause) and not CJK.search(d.localized("en").cause)
    if d.recognized:
        assert d.evidence and all(line in f"{error}\n{text}" or "***" in line for line in d.evidence)
        assert d.cause_key == f"diag_{want_id}_cause" and d.step_keys[0] == f"diag_{want_id}_step1"
    else:
        assert d.evidence == []
        assert str(exit_code) in d.text["en"].cause and str(exit_code) in d.text["zh-CN"].cause


def test_every_pattern_has_a_sample_and_every_sample_is_checked(logs_dir):
    files = {p.name for p in logs_dir.glob("*.log")}
    assert files == set(EXPECTED), files ^ set(EXPECTED)
    covered = {name.split("-", 1)[0] for name in files}
    assert set(pattern_ids()) <= covered, set(pattern_ids()) - covered
    assert "unknown" in covered


def test_first_batch_required_by_the_plan_is_present():
    ids = set(pattern_ids())
    for required in (
        "port_in_use",
        "gpu_out_of_memory",
        "engine_not_found",
        "engine_not_executable",
        "cuda_mismatch",
        "docker_unreachable",
        "mount_helper_missing",
        "mount_failed",
        "model_file_missing",
        "model_corrupt",
        "model_unsupported",
        "image_pull_failed",
        "gpu_runtime_missing",
        "models_permission_denied",
        "hf_auth",
        "disk_full",
    ):
        assert required in ids


@pytest.mark.parametrize(
    "line, want",
    [
        ("OSError: [Errno 10048] error while attempting to bind on address", "port_in_use"),
        ("listen tcp 0.0.0.0:8081: bind: address already in use", "port_in_use"),
        ("RuntimeError: CUDA out of memory. Tried to allocate 20.00 MiB", "gpu_out_of_memory"),
        ("FileNotFoundError: [Errno 2] No such file or directory: '/models/x.bin'", "model_file_missing"),
        ("cp: cannot stat 'x': No such file or directory", "file_not_found"),
    ],
)
def test_product_acceptance_strings(line, want):
    """F08 acceptance 1: these log fragments get their own cause, in English and in Chinese."""
    d = diagnose([line], exit_code=1)
    assert d.id == want
    assert CJK.search(d.localized("zh-CN").cause)


def test_unknown_keeps_raw_exit_code_and_tail():
    log = [f"line {i}" for i in range(50)]
    d = diagnose(log, error="engine exited with code 3", exit_code=3, tail=10)
    assert not d.recognized and d.id == "unknown" and d.category == "unknown"
    assert d.exit_code == 3 and d.error == "engine exited with code 3" and d.log_tail == log[-10:]
    assert "3" in d.localized("en").cause and "3" in d.localized("zh").cause
    assert d.cause_key == "diag_unknown_cause" and d.params == {"exit_code": "3"}
    nothing = diagnose()
    assert not nothing.recognized and nothing.log_tail == [] and nothing.exit_code is None
    assert "{" not in nothing.localized("en").cause


@pytest.mark.parametrize(
    "code, want",
    [
        (127, "engine_not_found"),
        (9009, "engine_not_found"),
        (126, "engine_not_executable"),
        (137, "host_out_of_memory"),
        (-9, "host_out_of_memory"),
        (3221225781, "engine_library_missing"),
        (-1073741515, "engine_library_missing"),  # the same NTSTATUS, signed
        (3, "unknown"),
        (0, "unknown"),
    ],
)
def test_exit_code_only(code, want):
    assert diagnose(["no hint here"], exit_code=code).id == want


def test_log_evidence_beats_exit_code(logs_dir):
    text = _read(logs_dir / "engine_library_missing-linux-libcudart.log")
    assert diagnose(text, exit_code=127).id == "engine_library_missing"  # 127 alone would be engine_not_found
    assert diagnose(["Killed"], exit_code=127).id == "host_out_of_memory"


def test_order_specific_before_generic():
    # Permission denied on a GGUF file is about permissions, not a missing file.
    d = diagnose(["gguf_init_from_file: failed to open GGUF file '/m/a.gguf' (Permission denied)"])
    assert d.id == "models_permission_denied"
    # A full disk while pulling is a full disk.
    assert diagnose(["write /var/lib/docker/tmp/x: no space left on device"]).id == "disk_full"
    # Docker's socket missing is Docker, not some file.
    assert diagnose(error="Docker is not reachable: [Errno 2] No such file or directory").id == "docker_unreachable"


def test_secrets_are_scrubbed_from_what_is_shown(logs_dir):
    d = diagnose(_read(logs_dir / "mount_access_denied-docker-cifs.log"))
    assert d.id == "mount_access_denied"
    shown = json.dumps(d.model_dump(exclude={"error"}))
    assert "hunter2" not in shown and "password=***" in shown
    d = diagnose(
        [
            "Authorization: Bearer abc.def.ghi",
            "token=hf_abcdefghijklmnopqrstuvwxyz0123",
            "https://alice:s3cret@nas.local/share",
            "api_key: sk-proj-0123456789abcdefghij",
        ]
    )
    shown = json.dumps(d.model_dump())
    for secret in ("abc.def.ghi", "hf_abcdefghijklmnopqrstuvwxyz0123", "s3cret", "sk-proj-0123456789abcdefghij"):
        assert secret not in shown


def test_localized_picks_language():
    d = diagnose(["address already in use"])
    assert d.localized("zh-CN") == d.text["zh-CN"] == d.localized("zh") == d.localized("zh-TW")
    assert d.localized("en-US") == d.text["en"] == d.localized(None) == d.localized("de")


def test_result_is_a_copy_and_serializable():
    a = diagnose(["address already in use"], exit_code=1)
    a.params["port"] = "changed"
    a.log_tail.append("x")
    b = diagnose(["address already in use"], exit_code=1)
    assert "port" not in b.params and b.log_tail == ["address already in use"]
    json.dumps(b.model_dump())


def test_catalog_is_complete_in_both_languages():
    en, zh = catalog("en"), catalog("zh-CN")
    assert en.keys() == zh.keys() and len(en) > 3 * len(pattern_ids())
    for key in en:
        assert re.fullmatch(r"diag_[a-z0-9_]+", key), key  # i18n keys: no dots
        assert CJK.search(zh[key]) and not CJK.search(en[key]), key
        assert placeholders(en[key]) == placeholders(zh[key]), key
        if key.endswith("_generic"):
            assert not placeholders(en[key]), key


def test_data_file_rules():
    data = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
    ids = [p["id"] for p in data["patterns"]]
    assert len(ids) == len(set(ids)) and "unknown" not in ids
    for p in data["patterns"]:
        assert re.fullmatch(r"[a-z0-9_]+", p["id"])
        assert p["match"] or p.get("exit_codes")
        assert set(p["text"]) == set(LOCALES)
        assert len({len(p["text"][loc]["steps"]) for loc in LOCALES}) == 1, p["id"]
        groups = set()
        for rx in p["match"]:
            groups |= set(re.compile(rx).groupindex)
        for loc in LOCALES:
            for entry in [p["text"][loc]["cause"], *p["text"][loc]["steps"]]:
                text = entry if isinstance(entry, str) else entry["text"]
                used = placeholders(text)
                assert used <= groups | {"exit_code"}, (p["id"], used)
                # A text that needs a captured value has a generic form for when it was not captured.
                assert not used or isinstance(entry, dict), (p["id"], text)


def test_fast_on_long_logs():
    library()  # compiled once per process
    noise = [f"load_tensors: layer {i:4d} assigned to device CUDA0, is_swa = 0" for i in range(4000)]
    start = time.perf_counter()
    d = diagnose([*noise, "main: couldn't bind HTTP server socket, hostname: 127.0.0.1, port: 8099"], exit_code=1)
    assert time.perf_counter() - start < 0.5
    assert d.id == "port_in_use" and d.params["port"] == "8099"


def test_patterns_file_is_packaged():
    pyproject = Path(diagnostics.__file__).resolve().parents[3] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert "services/diagnostics/*.json" in data["tool"]["setuptools"]["package-data"]["slotserve"]
