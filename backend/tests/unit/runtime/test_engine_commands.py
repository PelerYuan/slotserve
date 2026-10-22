"""How each engine's server command line is built (argument policy itself: test_engine_args.py, W0-02)."""

import pytest

from slotserve.runtime.engines import DEFAULT_ENGINE, ENGINES, engine_for, split_args, strip_flags

EXE = "no-such-engine-binary-w008"  # neither a file nor on PATH: left as written


@pytest.fixture
def cfg(settings):
    return settings(
        llama_server_cmd=f"{EXE} --threads 4",
        vllm_cmd=f"{EXE} serve",
        sglang_cmd=f"{EXE} -m sglang.launch_server",
        custom_engine_cmd=f"{EXE} --weights {{model}} --listen {{host}}:{{port}} --tag {{name}}",
        custom_engine_health="/ready",
    )


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("llama.cpp", "--threads 4 -m /m/q.gguf --host 127.0.0.1 --port 8081 --alias q -c 4096"),
        ("vllm", "serve /m/q.gguf --host 127.0.0.1 --port 8081 --served-model-name q -c 4096"),
        (
            "sglang",
            "-m sglang.launch_server --model-path /m/q.gguf --host 127.0.0.1 --port 8081 --served-model-name q -c 4096",
        ),
        ("custom", "--weights /m/q.gguf --listen 127.0.0.1:8081 --tag q -c 4096"),
    ],
)
def test_command_per_engine(cfg, engine, expected):
    assert ENGINES[engine].command(cfg, "/m/q.gguf", "q", "-c 4096", "127.0.0.1", 8081) == [EXE, *expected.split()]


def test_user_args_cannot_override_managed_flags(cfg):
    cmd = ENGINES["llama.cpp"].command(cfg, "/m/q.gguf", "q", "--port 1 --host=0.0.0.0 -ngl 99 -a other", "h", 2)
    assert cmd[-2:] == ["-ngl", "99"]
    assert cmd.count("--port") == 1 and "0.0.0.0" not in " ".join(cmd) and "other" not in cmd


def test_strip_flags_handles_both_value_forms():
    managed = frozenset({"--port", "--host"})
    assert strip_flags(["--port", "1", "--host=x", "--ctx", "8", "--port"], managed) == ["--ctx", "8"]


def test_split_args_keeps_quoted_values_together():
    assert split_args('--chat-template "a b" -c 8') == ["--chat-template", "a b", "-c", "8"]
    assert split_args("") == []


def test_health_path_and_engine_lookup(cfg):
    assert ENGINES["custom"].health_path(cfg) == "/ready"
    assert ENGINES["vllm"].health_path(cfg) == "/health"
    assert engine_for(type("M", (), {"engine": None})()).id == DEFAULT_ENGINE
    assert engine_for(type("M", (), {"engine": "removed-engine"})()).id == DEFAULT_ENGINE
    assert engine_for(type("M", (), {"engine": "sglang"})()).id == "sglang"
    assert [e.uses_gguf for e in ENGINES.values()] == [True, False, False, False]
