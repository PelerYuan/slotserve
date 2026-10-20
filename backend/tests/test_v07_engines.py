"""Inference engines: vLLM, SGLang and custom commands, driven through the mock server."""

import sys
from pathlib import Path

import pytest

from conftest import MOCK_SERVER, make_gguf, wait_for_state
from slotserve.runtime.engines import ENGINES, strip_flags

PY = f'"{sys.executable}" "{MOCK_SERVER}"'


def test_command_lines(app):
    s = app.state.settings
    vllm = ENGINES["vllm"].command(s, "Qwen/Qwen3-8B", "qwen", "--port 1 --max-model-len 8192", "127.0.0.1", 9000)
    assert vllm[:2] == [*ENGINES["vllm"].base_command(s)][:2] or vllm[0].endswith("vllm")
    assert "Qwen/Qwen3-8B" in vllm and vllm[vllm.index("--served-model-name") + 1] == "qwen"
    assert vllm.count("--port") == 1 and "8192" in vllm
    sg = ENGINES["sglang"].command(s, "/m", "x", "", "h", 1)
    assert sg[-8:] == ["--model-path", "/m", "--host", "h", "--port", "1", "--served-model-name", "x"]
    assert strip_flags(["--port=3", "-c", "4"], frozenset({"--port"})) == ["-c", "4"]


@pytest.mark.parametrize(
    ("engine", "setting", "template"),
    [
        ("vllm", "vllm_cmd", f"{PY} serve"),
        ("sglang", "sglang_cmd", PY),
        (
            "custom",
            "custom_engine_cmd",
            f"{PY} --model-path {{model}} --host {{host}} --port {{port}} --served-model-name {{name}}",
        ),
    ],
)
async def test_engine_end_to_end(engine, setting, template, make_client, models_dir):
    admin = await make_client("root")
    make_gguf(models_dir, "base.gguf")
    await admin.post("/api/models/scan")
    base = (await admin.get("/api/models")).json()[0]
    await admin.post(f"/api/models/{base['id']}/default")
    r = await admin.put("/api/admin/settings", json={setting: template})
    assert r.status_code == 200, r.text

    r = await admin.post(
        "/api/models", json={"name": f"{engine}-model", "engine": engine, "path": "org/some-model", "args": "--foo 1"}
    )
    assert r.status_code == 201, r.text
    model = r.json()
    assert model["engine"] == engine and model["file_exists"] is True

    await admin.post("/api/admin/runtime/manual", json={"action": "load", "model_id": model["id"], "hours": 1})
    await wait_for_state(admin, "ready", f"{engine}-model")
    key = (await admin.post("/api/keys", json={"name": "k"})).json()["key"]
    # no "model" in the request: the gateway fills in the served name, which these engines insist on
    r = await admin.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    assert f"[{engine}-model]" in r.json()["choices"][0]["message"]["content"]


async def test_create_model_validation(make_client, models_dir: Path):
    admin = await make_client("root")
    r = await admin.post("/api/models", json={"name": "x", "engine": "llama.cpp", "path": "/nope/missing.gguf"})
    assert r.json()["detail"]["code"] == "model_path_invalid"
    gguf = make_gguf(models_dir, "manual.gguf")
    r = await admin.post("/api/models", json={"name": "manual", "path": str(gguf)})
    assert r.status_code == 201 and r.json()["engine"] == "llama.cpp" and r.json()["file_exists"]
    r = await admin.post("/api/models", json={"name": "manual", "path": str(gguf)})
    assert r.json()["detail"]["code"] == "model_name_taken"
    r = await admin.post("/api/models", json={"name": "y", "engine": "ollama", "path": "x"})
    assert r.status_code == 422
    r = await admin.patch(f"/api/models/{(await admin.get('/api/models')).json()[0]['id']}", json={"engine": "vllm"})
    assert r.json()["engine"] == "vllm"
