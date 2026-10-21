"""Model catalog: built-in data, memory estimates, lane fit, one-click deploy and admin entries."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from conftest import make_gguf
from slotserve.schemas import GpuOut
from slotserve.services import catalog

GIB = 1024**3


def _gpu(index: int, mb: int) -> GpuOut:
    return GpuOut(
        index=index, name="Test GPU", memory_used_mb=0, memory_total_mb=mb, utilization_pct=0, temperature_c=30
    )


@pytest.fixture
def two_gpus(monkeypatch):
    """GPU 0 with 8 GiB, GPU 1 with 24 GiB."""

    async def fake():
        return [_gpu(0, 8192), _gpu(1, 24576)]

    monkeypatch.setattr("slotserve.runtime.gpu.gpu_stats", fake)
    monkeypatch.setattr("slotserve.api.lanes.gpu_stats", fake)


# ---- fake Hugging Face ---------------------------------------------------------------


class FakeHub:
    def __init__(self):
        self.repos: dict[str, dict[str, bytes]] = {}
        self.missing: set[str] = set()
        self.auth: list[str | None] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body: bytes, ctype="application/octet-stream", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                hub.auth.append(self.headers.get("Authorization"))
                path = unquote(urlparse(self.path).path)
                if path.startswith("/api/models/") and "/tree/" in path:
                    repo = path[len("/api/models/") :].split("/tree/")[0]
                    if repo not in hub.repos:
                        return self._send(404, b"{}", "application/json")
                    tree = [
                        {"type": "file", "path": p, "size": len(b), "lfs": {"size": len(b)}}
                        for p, b in hub.repos[repo].items()
                    ]
                    return self._send(200, json.dumps(tree).encode(), "application/json")
                parts = path.lstrip("/").split("/")
                if len(parts) >= 5 and parts[2] == "resolve":
                    repo, file = "/".join(parts[:2]), "/".join(parts[4:])
                    data = hub.repos.get(repo, {}).get(file)
                    if data is None or file in hub.missing:
                        return self._send(404, b"not found")
                    rng = self.headers.get("Range")
                    if rng:
                        start = int(rng.split("=")[1].split("-")[0])
                        body = data[start:]
                        return self._send(
                            206, body, headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}
                        )
                    return self._send(200, data)
                self._send(404, b"")

        return Handler


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


async def _wait_deploy(client, deploy_id: int, want: str = "done", timeout: float = 20.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = next(d for d in (await client.get("/api/catalog/deploys")).json() if d["id"] == deploy_id)
        if last["status"] == want:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"deploy never reached {want}: {last}")


# ---- built-in data, estimates, fit -----------------------------------------------------


def test_builtin_catalog_is_valid():
    data = catalog.load_file(catalog.CATALOG_FILE)
    assert len(data.entries) >= 30
    for task in catalog.TASKS:
        assert sum(task in e.tasks for e in data.entries) >= 3, task
    for e in data.entries:
        assert e.description.en and e.description.zh and e.license and e.source_repo, e.id
        assert e.arch is not None or "embedding" in e.tasks, e.id
        for v in e.variants:
            assert v.size_bytes > 0, (e.id, v.id)
            if v.format == "safetensors":
                assert v.file_count and not v.files
            if v.mmproj:
                assert v.mmproj_size_bytes > 0
            assert v.engines == (["llama.cpp"] if v.format == "gguf" else ["vllm", "sglang"])
    assert any(len(v.files) > 1 for e in data.entries for v in e.variants)  # split GGUF files are covered


def test_catalog_validation_rejects_bad_entries():
    base = {
        "id": "x",
        "name": "X",
        "tasks": ["chat"],
        "params_b": 1,
        "context_length": 4096,
        "variants": [{"id": "q", "format": "gguf", "repo": "o/r", "quant": "Q4", "files": ["x.gguf"]}],
    }
    catalog.Entry.model_validate(base)
    for bad in (
        {"tasks": ["dance"]},
        {"variants": [{"id": "q", "format": "gguf", "repo": "o/r", "quant": "Q4", "files": ["../x.gguf"]}]},
        {"variants": [{"id": "q", "format": "gguf", "repo": "o/r", "quant": "Q4", "files": []}]},
        {"variants": [{"id": "q", "format": "safetensors", "repo": "o/r", "quant": "BF16", "files": ["a.gguf"]}]},
        {"id": "Bad Id"},
        {"extra_args": {"bash": "rm"}},
    ):
        with pytest.raises(ValueError):
            catalog.Entry.model_validate({**base, **bad})


def test_estimate_and_fit():
    entry = next(e for e in catalog.builtin().entries if e.id == "qwen3-8b")
    q4 = entry.variant("q4_k_m")
    est = catalog.estimate(entry, q4)
    assert est["ctx"] == 8192
    assert est["kv_cache"] == 2 * 36 * 8 * 128 * 8192 * 2
    assert est["total"] == est["weights"] + est["kv_cache"] + est["overhead"]
    assert catalog.estimate(entry, q4, 32768)["kv_cache"] == 4 * est["kv_cache"]
    assert catalog.estimate(entry, q4, 10**6)["ctx"] == entry.context_length  # clamped

    gpus = [_gpu(0, 8192), _gpu(1, 24576)]
    assert catalog.lane_memory([0], gpus) == 8 * GIB
    assert catalog.lane_memory(None, gpus) == 32 * GIB  # a lane without GPUs sees all of them
    assert catalog.lane_memory([5], gpus) is None and catalog.lane_memory([0], []) is None

    total = est["total"]
    assert catalog.fit(total, None, q4) == "unknown"
    assert catalog.fit(total, int(total / 0.95) + 1, q4) == "fits"
    assert catalog.fit(total, total, q4) == "tight"
    assert catalog.fit(total, total - 1, q4) == "no"
    bf16 = entry.variant("bf16")
    big = catalog.estimate(entry, bf16)["total"]
    assert catalog.fit(big, int(big / 0.93), bf16) == "tight"  # vLLM only gets 90 % of the memory

    assert catalog.launch_args(entry, "llama.cpp", 8192) == "-ngl 99 -c 8192"
    assert catalog.launch_args(entry, "vllm", 16384, gpu_count=2) == "--max-model-len 16384 --tensor-parallel-size 2"
    assert catalog.launch_args(entry, "sglang", 4096) == "--context-length 4096"
    emb = next(e for e in catalog.builtin().entries if e.id == "qwen3-embedding-0.6b")
    assert catalog.launch_args(emb, "llama.cpp", 8192).endswith("--embedding --pooling last")


def test_safetensors_file_selection():
    v = catalog.Variant(id="bf16", format="safetensors", repo="o/r", quant="BF16")
    tree = [
        {"type": "file", "path": p, "size": 1}
        for p in (
            "config.json",
            "model-00001-of-00002.safetensors",
            "tokenizer.json",
            "README.md",
            "consolidated.safetensors",
            "original/model.safetensors",
            "metal/model.bin",
            ".gitattributes",
            "chat_template.jinja",
        )
    ]
    assert [p for p, _ in catalog.select_files(tree, v)] == [
        "chat_template.jinja",
        "config.json",
        "model-00001-of-00002.safetensors",
        "tokenizer.json",
    ]
    mistral = v.model_copy(update={"include": ["consolidated.safetensors"], "exclude": ["model-*.safetensors"]})
    assert "consolidated.safetensors" in dict(catalog.select_files(tree, mistral))
    assert "model-00001-of-00002.safetensors" not in dict(catalog.select_files(tree, mistral))


# ---- API ---------------------------------------------------------------------------------


async def _lanes_small_and_big(admin) -> tuple[int, int]:
    first = (await admin.get("/api/lanes")).json()[0]
    r = await admin.patch(f"/api/admin/lanes/{first['id']}", json={"name": "Big", "gpus": [1]})
    assert r.status_code == 200, r.text
    r = await admin.post("/api/admin/lanes", json={"name": "Small", "gpus": [0]})
    assert r.status_code == 201, r.text
    return first["id"], r.json()["id"]


async def test_catalog_lists_fit_per_lane(two_gpus, make_client):
    admin = await make_client("root")
    bob = await make_client("bob")
    big, small = await _lanes_small_and_big(admin)
    data = (await bob.get("/api/catalog")).json()
    assert {lane["name"]: lane["memory_bytes"] for lane in data["lanes"]} == {"Big": 24 * GIB, "Small": 8 * GIB}
    entries = {e["id"]: e for e in data["entries"]}
    assert len(entries) >= 30 and data["updated"]
    q4 = next(v for v in entries["qwen3-8b"]["variants"] if v["id"] == "q4_k_m")
    assert {f["lane_id"]: f["status"] for f in q4["fits"]} == {big: "fits", small: "fits"}
    q8_32b = next(v for v in entries["qwen3-32b"]["variants"] if v["id"] == "q8_0")
    assert {f["lane_id"]: f["status"] for f in q8_32b["fits"]} == {big: "no", small: "no"}
    q4_14b = next(v for v in entries["qwen3-14b"]["variants"] if v["id"] == "q4_k_m")
    assert {f["lane_id"]: f["status"] for f in q4_14b["fits"]} == {big: "fits", small: "no"}
    assert not q4["downloaded"] and q4["models"] == []

    # a longer context grows the KV cache
    long = (await bob.get("/api/catalog/qwen3-8b", params={"ctx": 32768})).json()
    q4_long = next(v for v in long["variants"] if v["id"] == "q4_k_m")
    assert q4_long["estimate"]["kv_cache"] == 4 * q4["estimate"]["kv_cache"]
    assert (await bob.get("/api/catalog/nope")).status_code == 404


async def test_catalog_without_gpu_info(monkeypatch, make_client):
    async def none():
        return []

    monkeypatch.setattr("slotserve.runtime.gpu.gpu_stats", none)
    bob = await make_client("bob")
    data = (await bob.get("/api/catalog")).json()
    assert all(lane["memory_bytes"] is None for lane in data["lanes"])
    assert {f["status"] for e in data["entries"] for v in e["variants"] for f in v["fits"]} == {"unknown"}


async def test_deploy_file_already_on_disk(two_gpus, make_client, models_dir):
    admin = await make_client("root")
    bob = await make_client("bob")
    big, _small = await _lanes_small_and_big(admin)
    make_gguf(models_dir, "Qwen3-8B-Q4_K_M.gguf")
    listed = (await bob.get("/api/catalog/qwen3-8b")).json()
    assert next(v for v in listed["variants"] if v["id"] == "q4_k_m")["downloaded"]

    r = await bob.post("/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m"})
    assert r.status_code == 201, r.text
    dep = r.json()
    assert dep["status"] == "done" and dep["files"] == 0 and dep["model_id"]
    model = next(m for m in (await bob.get("/api/models")).json() if m["id"] == dep["model_id"])
    assert model["name"] == "qwen3-8b-q4_k_m" and model["engine"] == "llama.cpp"
    assert model["args"] == "-ngl 99 -c 8192" and model["description"].startswith("General-purpose")

    # deploying again re-uses the model; only admins may make it a lane default
    r = await bob.post(
        "/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "set_default": True}
    )
    assert r.status_code == 403
    r = await admin.post(
        "/api/catalog/deploy",
        json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "set_default": True, "lane_id": big},
    )
    assert r.json()["model_id"] == dep["model_id"]
    lanes = {lane["id"]: lane for lane in (await admin.get("/api/lanes")).json()}
    assert lanes[big]["default_model_id"] == dep["model_id"]
    assert len((await bob.get("/api/models")).json()) == 1
    listed = (await bob.get("/api/catalog/qwen3-8b")).json()
    assert next(v for v in listed["variants"] if v["id"] == "q4_k_m")["models"][0]["id"] == dep["model_id"]

    r = await bob.post("/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m", "engine": "vllm"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "catalog_engine_mismatch"


async def test_deploy_checks_token_and_space(two_gpus, monkeypatch, make_client, hub):
    admin = await make_client("root")
    bob = await make_client("bob")
    # gated repository without a token
    r = await bob.post("/api/catalog/deploy", json={"entry_id": "llama-3.1-8b-instruct", "variant_id": "bf16"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "hf_token_required"

    await admin.put("/api/admin/settings", json={"hf_endpoint": hub.url})
    hub.repos["Qwen/Qwen3-8B-GGUF"] = {"Qwen3-8B-Q4_K_M.gguf": b"GGUF"}

    class Usage:
        free = 10**9

    monkeypatch.setattr("slotserve.services.catalog_deploy.shutil.disk_usage", lambda _p: Usage)
    r = await bob.post("/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m"})
    assert r.status_code == 507 and r.json()["detail"]["code"] == "insufficient_disk"
    assert (await bob.get("/api/downloads")).json() == []


def _custom_entry() -> dict:
    return {
        "id": "tiny-chat",
        "name": "Tiny Chat",
        "publisher": "Test",
        "license": "MIT",
        "tasks": ["chat", "vision"],
        "params_b": 0.1,
        "context_length": 4096,
        "source_repo": "org/tiny",
        "description": {"en": "A tiny test model.", "zh": "测试模型"},
        "variants": [
            {
                "id": "q4",
                "format": "gguf",
                "repo": "org/tiny-GGUF",
                "quant": "Q4_K_M",
                "files": ["sub/tiny-Q4-00001-of-00002.gguf"],
                "mmproj": "mmproj-tiny.gguf",
            },
            {"id": "bf16", "format": "safetensors", "repo": "org/tiny", "quant": "BF16"},
        ],
    }


async def test_custom_entry_deploy_through_downloader(two_gpus, app, make_client, models_dir, hub):
    admin = await make_client("root")
    bob = await make_client("bob")
    big, small = await _lanes_small_and_big(admin)
    await admin.put("/api/admin/settings", json={"hf_endpoint": hub.url, "hf_token": "hf_secret"})
    hub.repos["org/tiny-GGUF"] = {
        "sub/tiny-Q4-00001-of-00002.gguf": b"GGUF" + b"\1" * 300,
        "sub/tiny-Q4-00002-of-00002.gguf": b"\2" * 200,
        "mmproj-tiny.gguf": b"GGUF" + b"\3" * 50,
        "README.md": b"hi",
    }
    config = {"num_hidden_layers": 4, "num_attention_heads": 8, "num_key_value_heads": 2, "hidden_size": 256}
    hub.repos["org/tiny"] = {
        "config.json": json.dumps(config).encode(),
        "model.safetensors": b"\0" * 400,
        "tokenizer.json": b"{}",
        "README.md": b"readme",
        "original/consolidated.pth": b"\0" * 10,
    }

    # only admins manage entries; ids of built-in entries are taken
    assert (await bob.post("/api/catalog/entries", json=_custom_entry())).status_code == 403
    taken = await admin.post("/api/catalog/entries", json={**_custom_entry(), "id": "qwen3-8b"})
    assert taken.status_code == 409
    missing = _custom_entry()
    missing["variants"][0]["mmproj"] = "nope.gguf"
    r = await admin.post("/api/catalog/entries", json=missing)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "catalog_file_missing"

    r = await admin.post("/api/catalog/entries", json=_custom_entry())
    assert r.status_code == 201, r.text
    entry = r.json()
    gguf, st = entry["variants"]
    assert gguf["files"] == ["sub/tiny-Q4-00001-of-00002.gguf", "sub/tiny-Q4-00002-of-00002.gguf"]
    assert gguf["size_bytes"] == 504 and gguf["mmproj_size_bytes"] == 54 and gguf["download_bytes"] == 558
    assert st["size_bytes"] == len(json.dumps(config)) + 400 + 2 and st["file_count"] == 3
    assert entry["arch"] == {"layers": 4, "kv_heads": 2, "head_dim": 32} and entry["source"] == "custom"
    assert "Bearer hf_secret" in hub.auth
    assert any(e["id"] == "tiny-chat" for e in (await bob.get("/api/catalog")).json()["entries"])

    # llama.cpp variant: three files through the download queue, then one model with --mmproj
    r = await bob.post("/api/catalog/deploy", json={"entry_id": "tiny-chat", "variant_id": "q4", "lane_id": small})
    assert r.status_code == 201, r.text
    dep = r.json()
    assert dep["status"] == "downloading" and dep["files"] == 3
    names = [d["filename"] for d in (await bob.get("/api/downloads")).json()]
    assert sorted(names) == ["mmproj-tiny.gguf", "tiny-Q4-00001-of-00002.gguf", "tiny-Q4-00002-of-00002.gguf"]
    done = await _wait_deploy(bob, dep["id"])
    assert done["downloaded_bytes"] == 558 and done["total_bytes"] == 558
    models = (await bob.get("/api/models")).json()
    assert len(models) == 1  # the downloader did not register the GGUF on its own
    model = models[0]
    assert model["id"] == done["model_id"] and model["name"] == "tiny-chat-q4" and model["size_bytes"] == 504
    mmproj = str((models_dir / "mmproj-tiny.gguf").resolve())
    assert model["args"] == f"-ngl 99 -c 4096 --mmproj {mmproj}" or model["args"].endswith(f'--mmproj "{mmproj}"')
    assert all(d["model_id"] == model["id"] for d in (await bob.get("/api/downloads")).json())

    # vLLM variant on a lane, as its default: a folder with just the files the engine needs
    r = await admin.post(
        "/api/catalog/deploy",
        json={"entry_id": "tiny-chat", "variant_id": "bf16", "engine": "vllm", "lane_id": big, "set_default": True},
    )
    assert r.status_code == 201, r.text
    dep2 = await _wait_deploy(admin, r.json()["id"])
    folder = models_dir / "org--tiny"
    assert sorted(p.name for p in folder.iterdir()) == ["config.json", "model.safetensors", "tokenizer.json"]
    vllm = next(m for m in (await admin.get("/api/models")).json() if m["id"] == dep2["model_id"])
    assert vllm["engine"] == "vllm" and vllm["args"] == "--max-model-len 4096"
    assert Path(vllm["file_path"]) == folder.resolve() and vllm["default_lane_ids"] == [big]

    # deploying again only re-uses what is there
    again = (await bob.post("/api/catalog/deploy", json={"entry_id": "tiny-chat", "variant_id": "q4"})).json()
    assert again["status"] == "done" and again["model_id"] == model["id"] and again["files"] == 0
    listed = (await bob.get("/api/catalog/tiny-chat")).json()
    assert [v["models"][0]["id"] for v in listed["variants"]] == [model["id"], vllm["id"]]

    # admins hide entries (members no longer see them) and delete their own
    assert (await bob.put("/api/catalog/qwen3-8b/hidden", json={"hidden": True})).status_code == 403
    assert (await admin.put("/api/catalog/qwen3-8b/hidden", json={"hidden": True})).status_code == 204
    assert not any(e["id"] == "qwen3-8b" for e in (await bob.get("/api/catalog")).json()["entries"])
    assert next(e for e in (await admin.get("/api/catalog")).json()["entries"] if e["id"] == "qwen3-8b")["hidden"]
    r = await bob.post("/api/catalog/deploy", json={"entry_id": "qwen3-8b", "variant_id": "q4_k_m"})
    assert r.status_code == 404
    await admin.put("/api/catalog/qwen3-8b/hidden", json={"hidden": False})
    assert any(e["id"] == "qwen3-8b" for e in (await bob.get("/api/catalog")).json()["entries"])

    edited = {**_custom_entry(), "name": "Tiny Chat 2"}
    r = await admin.put("/api/catalog/entries/tiny-chat", json=edited)
    assert r.status_code == 200 and r.json()["name"] == "Tiny Chat 2"
    assert (await admin.delete("/api/catalog/entries/tiny-chat")).status_code == 204
    assert not any(e["id"] == "tiny-chat" for e in (await admin.get("/api/catalog")).json()["entries"])
    assert (await admin.delete("/api/catalog/entries/qwen3-8b")).status_code == 404


async def test_failed_download_stalls_then_retry_finishes(two_gpus, make_client, models_dir, hub):
    admin = await make_client("root")
    await admin.put("/api/admin/settings", json={"hf_endpoint": hub.url})
    hub.repos["Qwen/Qwen3-Embedding-0.6B-GGUF"] = {"Qwen3-Embedding-0.6B-Q8_0.gguf": b"GGUF" + b"\0" * 100}
    hub.missing.add("Qwen3-Embedding-0.6B-Q8_0.gguf")
    r = await admin.post("/api/catalog/deploy", json={"entry_id": "qwen3-embedding-0.6b", "variant_id": "q8_0"})
    assert r.status_code == 201, r.text
    stalled = await _wait_deploy(admin, r.json()["id"], "stalled")
    assert "404" in stalled["error"]

    hub.missing.clear()
    task = (await admin.get("/api/downloads")).json()[0]
    assert (await admin.post(f"/api/downloads/{task['id']}/retry")).status_code == 200
    done = await _wait_deploy(admin, stalled["id"])
    model = next(m for m in (await admin.get("/api/models")).json() if m["id"] == done["model_id"])
    assert model["args"] == "-ngl 99 -c 8192 --embedding --pooling last"

    # dismissing a deploy removes it from the list
    assert (await admin.delete(f"/api/catalog/deploys/{done['id']}")).status_code == 204
    assert (await admin.get("/api/catalog/deploys")).json() == []
