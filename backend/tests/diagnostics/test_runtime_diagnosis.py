"""Failed model loads carry a diagnosis (services/diagnostics) in the admin runtime view and the load history."""

import asyncio

from conftest import make_gguf, wait_for_state


async def _lane_when(admin, check, timeout: float = 20.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    lane = None
    while asyncio.get_running_loop().time() < deadline:
        [lane] = (await admin.get("/api/admin/runtime")).json()
        if check(lane):
            return lane
        await asyncio.sleep(0.2)
    raise AssertionError(f"lane never matched; last={lane}")


async def test_failed_loads_are_diagnosed(make_client, models_dir):
    admin = await make_client("root")
    make_gguf(models_dir, "good.gguf")
    await admin.post("/api/models/scan")
    model = (await admin.get("/api/models")).json()[0]
    await admin.post(f"/api/models/{model['id']}/default")
    await wait_for_state(admin, "ready", "good")
    [lane] = (await admin.get("/api/admin/runtime")).json()
    assert lane["diagnosis"] is None

    # A recognized failure: GPU out of memory, with the cause in both languages and the matching log line.
    await admin.patch(f"/api/models/{model['id']}", json={"args": "--mock-oom"})
    await admin.post(f"/api/runtime/restart?lane_id={lane['lane_id']}")
    lane = await _lane_when(admin, lambda x: x["diagnosis"] and x["diagnosis"]["id"] == "gpu_out_of_memory")
    d = lane["diagnosis"]
    assert d["recognized"] and d["params"]["device"] == "0" and d["exit_code"] == lane["exit_code"]
    assert any("cudaMalloc failed: out of memory" in line for line in d["evidence"])
    assert d["text"]["en"]["cause"] == "GPU 0 ran out of memory while loading the model"
    assert d["text"]["zh-CN"]["cause"] == "加载模型时 GPU 0 显存不足"
    assert d["cause_key"] == "diag_gpu_out_of_memory_cause" and len(d["step_keys"]) == len(d["text"]["en"]["steps"])

    events = (await admin.get("/api/admin/runtime/events?failed=true")).json()
    assert events[0]["diagnosis"]["id"] == "gpu_out_of_memory"
    assert all(e["diagnosis"] is None for e in (await admin.get("/api/admin/runtime/events")).json() if e["success"])

    # An unknown failure keeps the raw exit code and the log tail instead of guessing.
    await admin.patch(f"/api/models/{model['id']}", json={"args": "--mock-fail"})
    await admin.post(f"/api/runtime/restart?lane_id={lane['lane_id']}")
    lane = await _lane_when(admin, lambda x: x["diagnosis"] and x["diagnosis"]["id"] == "unknown")
    d = lane["diagnosis"]
    assert not d["recognized"] and d["exit_code"] == lane["exit_code"] not in (None, 0)
    assert any("simulated load failure" in line for line in d["log_tail"])
    assert d["error"] == lane["runtime"]["error"]
    assert str(lane["exit_code"]) in d["text"]["en"]["cause"] and str(lane["exit_code"]) in d["text"]["zh-CN"]["cause"]
    events = (await admin.get("/api/admin/runtime/events?failed=true")).json()
    assert events[0]["diagnosis"]["id"] == "unknown"
