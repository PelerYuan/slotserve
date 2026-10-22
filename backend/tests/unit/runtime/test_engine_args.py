"""Launch-argument policy tables: parsing, allowlist, denied flags, file arguments and model sources."""

from pathlib import Path

import pytest

from slotserve.runtime.engines import (
    ENGINES,
    POSITIONAL,
    Flag,
    is_denied,
    member_source_ok,
    parse_flags,
    review_args,
)
from slotserve.services import catalog

LLAMA, VLLM, SGLANG, CUSTOM = (ENGINES[e] for e in ("llama.cpp", "vllm", "sglang", "custom"))


def test_parse_flags_groups_values():
    assert parse_flags('-ngl 99 -c=8192 --seed -1 --override-tensor "a b" stray') == [
        Flag("-ngl", ("99",)),
        Flag("-c", ("8192",)),
        Flag("--seed", ("-1",)),
        Flag("--override-tensor", ("a b", "stray")),
    ]
    assert parse_flags("model.gguf --jinja") == [Flag(POSITIONAL, ("model.gguf",)), Flag("--jinja")]
    assert parse_flags("") == []
    with pytest.raises(ValueError):
        parse_flags('--chat-template "unbalanced')


@pytest.mark.parametrize(
    ("engine", "args", "needs", "denied"),
    [
        (LLAMA, "-ngl 99 -c 8192 --jinja --embedding --pooling mean", [], []),
        (LLAMA, "--log-file x", ["--log-file"], ["--log-file"]),
        (LLAMA, "--log-file=x", ["--log-file"], ["--log-file"]),
        (LLAMA, "--slot-save-path /tmp/s", ["--slot-save-path"], ["--slot-save-path"]),
        (LLAMA, "--chat-template-file t.jinja", ["--chat-template-file"], ["--chat-template-file"]),
        (LLAMA, "-hf org/repo", ["-hf"], ["-hf"]),
        (LLAMA, "--api-key secret", ["--api-key"], ["--api-key"]),
        (LLAMA, "--some-new-flag 1", ["--some-new-flag"], []),
        (LLAMA, "stray -ngl 1", [POSITIONAL], []),
        (VLLM, "--max-model-len 4096 --tensor-parallel-size 2 --runner pooling", [], []),
        (VLLM, "--trust-remote-code", ["--trust-remote-code"], ["--trust-remote-code"]),
        (VLLM, "--tool-parser-plugin p.py", ["--tool-parser-plugin"], ["--tool-parser-plugin"]),
        (VLLM, "--middleware evil.mod", ["--middleware"], ["--middleware"]),
        (VLLM, "--download-dir /x", ["--download-dir"], ["--download-dir"]),
        (VLLM, "--ssl-keyfile k", ["--ssl-keyfile"], ["--ssl-keyfile"]),
        (SGLANG, "--context-length 8192 --tp-size 2 --is-embedding", [], []),
        (SGLANG, "--trust-remote-code --tp-size 2", ["--trust-remote-code"], ["--trust-remote-code"]),
        (CUSTOM, "--anything", ["--anything"], []),
    ],
)
def test_review(engine, args, needs, denied, tmp_path):
    review = review_args(engine, args, [tmp_path])
    assert review.needs_approval == needs
    assert review.denied == denied


def test_managed_flags_are_ignored(tmp_path):
    # SlotServe strips these at launch, so they cannot do harm.
    assert review_args(LLAMA, "--port 1 --host 0.0.0.0 -m x.gguf", [tmp_path]).needs_approval == []


def test_denied_patterns():
    for flag in ("--log-disable", "--foo-file", "--foo-path", "--foo-dir", "--ssl-certfile", "--worker-cls"):
        assert is_denied(VLLM, flag), flag
    assert not is_denied(VLLM, "--max-model-len")
    # argparse accepts unambiguous prefixes
    assert is_denied(VLLM, "--trust-remote") and is_denied(SGLANG, "--trust")
    assert not is_denied(VLLM, "--tokenizer-mode")
    assert not is_denied(LLAMA, "--chat-template")  # llama.cpp takes no abbreviations
    assert not is_denied(LLAMA, "-c")


def test_file_arguments_must_be_in_a_library(tmp_path):
    models = tmp_path / "models"
    (models / "sub").mkdir(parents=True)
    data = models / "data"
    inside = models / "sub" / "mmproj.gguf"
    assert review_args(LLAMA, f"--mmproj {inside}", [models]).needs_approval == []
    assert review_args(LLAMA, f"--mmproj {tmp_path / 'x.gguf'}", [models]).needs_approval == ["--mmproj"]
    assert review_args(LLAMA, f"--mmproj {models / '..' / 'x.gguf'}", [models]).needs_approval == ["--mmproj"]
    assert review_args(LLAMA, "--mmproj", [models]).needs_approval == ["--mmproj"]
    assert review_args(LLAMA, f"-md {data / 'x'}", [models], exclude=[data]).needs_approval == ["-md"]


def test_approved_flags_are_not_asked_again(tmp_path):
    approved = ["-ngl 99 --some-flag 1 --log-file a.log"]
    review = review_args(LLAMA, "-ngl 50 --some-flag 1 --log-file a.log", [tmp_path], approved)
    assert review.needs_approval == []
    # a different value is a different request
    review = review_args(LLAMA, "--some-flag 2 --log-file b.log", [tmp_path], approved)
    assert review.needs_approval == ["--some-flag", "--log-file"]
    assert review.denied == ["--log-file"]


def test_member_model_sources(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "slotserve.db"
    db.write_bytes(b"")
    assert member_source_ok(str(models / "a.gguf"), [models])
    assert member_source_ok("Qwen/Qwen3-8B", [models])
    assert not member_source_ok(str(db), [models])
    assert not member_source_ok("/data/slotserve.db", [models])
    assert not member_source_ok("../models/x", [models])
    assert not member_source_ok("org/..", [models])
    assert not member_source_ok(str(models / "x"), [models], exclude=[models])


def test_catalog_arguments_are_all_allowed(tmp_path):
    """One-click deploys must work for members: every generated argument is on the allowlist."""
    models = tmp_path / "models"
    checked = 0
    for entry in catalog.builtin().entries:
        for variant in entry.variants:
            mmproj = str(models / variant.mmproj.rsplit("/", 1)[-1]) if variant.mmproj else None
            for engine in variant.engines:
                for gpus in (1, 2):
                    args = catalog.launch_args(entry, engine, entry.ctx, gpus, mmproj)
                    review = review_args(ENGINES[engine], args, [models])
                    assert review.needs_approval == [], (entry.id, variant.id, engine, args)
                    checked += 1
    assert checked > 50


def test_mmproj_with_spaces_is_still_inside(tmp_path):
    models = tmp_path / "my models"
    path = str(models / "mm proj.gguf")
    args = catalog.launch_args(catalog.builtin().entries[0], "llama.cpp", 4096, 1, path)
    assert review_args(LLAMA, args, [Path(models)]).needs_approval == []
