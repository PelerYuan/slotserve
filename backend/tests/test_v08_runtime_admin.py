"""Admin runtime view: loading progress from the log, failure details, load history and the live log stream."""

import asyncio
import json

from conftest import make_gguf, wait_for_state
from slotserve.api import runtime_admin
from slotserve.runtime.progress import parse_progress


def test_parse_progress():
    assert parse_progress([]).percent is None
    llama = ["===== start: llama-server -m x", "load_tensors: loading model tensors", "....................."]
    p = parse_progress(llama)
    assert p.stage == "weights" and p.percent == 21
    assert parse_progress([*llama, "llama_context: n_ctx = 8192"]) == parse_progress(
        ["load_tensors:", "llama_context:"]
    )
    assert parse_progress([*llama, "llama_context: n_ctx = 8192"]).percent == 100
    vllm = [
        "Loading safetensors checkpoint shards:  25% Completed | 1/4",
        "Loading safetensors checkpoint shards:  75% Completed | 3/4",
    ]
    assert parse_progress(vllm).percent == 75 and parse_progress(vllm).stage == "weights"
    assert parse_progress(["main: server is listening on http://127.0.0.1:8081"]).stage == "ready"
    assert parse_progress(["some unrelated line 50%"]).percent is None


async def test_runtime_details_and_failures(make_client, models_dir):
    admin = await make_client("root")
    member = await make_client("bob")
    make_gguf(models_dir, "good.gguf")
    await admin.post("/api/models/scan")
    model = (await admin.get("/api/models")).json()[0]
    await admin.post(f"/api/models/{model['id']}/default")
    await wait_for_state(admin, "ready", "good")

    assert (await member.get("/api/admin/runtime")).status_code == 403
    [lane] = (await admin.get("/api/admin/runtime")).json()
    assert lane["runtime"]["state"] == "ready" and lane["engine"] == "llama.cpp"
    assert "good.gguf" in lane["command"] and isinstance(lane["pid"], int)
    assert lane["target_model"] == "good" and lane["target_reason"] == "default"
    assert lane["failure_log"] == [] and lane["exit_code"] is None

    # Break the model: the server exits at once, and everything needed to understand why is kept.
    await admin.patch(f"/api/models/{model['id']}", json={"args": "--mock-fail"})
    await admin.post(f"/api/runtime/restart?lane_id={lane['lane_id']}")
    await wait_for_state(admin, "failed")
    [lane] = (await admin.get("/api/admin/runtime")).json()
    assert lane["exit_code"] not in (None, 0)
    assert any("simulated load failure" in line for line in lane["failure_log"])
    assert lane["next_retry_at"] is not None and lane["fail_count"] >= 1
    assert "--mock-fail" in lane["command"]

    events = (await admin.get("/api/admin/runtime/events?failed=true")).json()
    assert events and not events[0]["success"]
    assert events[0]["exit_code"] == lane["exit_code"] and "--mock-fail" in events[0]["command"]
    assert "simulated load failure" in events[0]["log_excerpt"] and events[0]["lane_name"]
    ok = (await admin.get("/api/admin/runtime/events")).json()
    assert any(e["success"] and e["log_excerpt"] is None for e in ok)

    log = await admin.get(f"/api/admin/runtime/{lane['lane_id']}/log")
    assert log.status_code == 200 and "simulated load failure" in log.text
    assert (await admin.get("/api/admin/runtime/999/log")).status_code == 404


async def test_status_reports_log_progress(make_client, models_dir, app, monkeypatch):
    monkeypatch.setenv("MOCK_LOAD_SECONDS", "4")
    admin = await make_client("root")
    make_gguf(models_dir, "slow.gguf")
    await admin.post("/api/models/scan")
    model = (await admin.get("/api/models")).json()[0]
    await admin.post(f"/api/models/{model['id']}/default")
    seen = set()
    for _ in range(60):
        rt = (await admin.get("/api/status")).json()["runtime"]
        if rt["state"] == "loading" and rt["log_percent"] is not None:
            seen.add((rt["stage"], rt["log_percent"]))
        if rt["state"] == "ready":
            break
        await asyncio.sleep(0.1)
    assert any(stage == "weights" and 0 < pct < 100 for stage, pct in seen), seen
    assert rt["state"] == "ready" and rt["log_percent"] is None


async def test_log_stream(make_client, models_dir, monkeypatch):
    monkeypatch.setattr(runtime_admin, "STREAM_SECONDS", 1.5)
    monkeypatch.setattr(runtime_admin, "POLL_SECONDS", 0.1)
    admin = await make_client("root")
    member = await make_client("bob")
    make_gguf(models_dir, "good.gguf")
    await admin.post("/api/models/scan")
    model = (await admin.get("/api/models")).json()[0]
    await admin.post(f"/api/models/{model['id']}/default")
    await wait_for_state(admin, "ready", "good")
    lane_id = (await admin.get("/api/admin/runtime")).json()[0]["lane_id"]

    assert (await member.get(f"/api/admin/runtime/{lane_id}/stream")).status_code == 403
    r = await admin.get(f"/api/admin/runtime/{lane_id}/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    ids = [int(line[4:]) for line in r.text.splitlines() if line.startswith("id: ")]
    assert events[0]["reset"] is True
    lines = [line for e in events for line in e["lines"]]
    assert any("start:" in line for line in lines) and any("server is listening" in line for line in lines)

    # Resuming from the last id sends nothing that was already seen.
    again = await admin.get(f"/api/admin/runtime/{lane_id}/stream", headers={"Last-Event-ID": str(ids[-1])})
    resumed = [json.loads(line[6:]) for line in again.text.splitlines() if line.startswith("data: ")]
    assert all(not e["reset"] and not e["lines"] for e in resumed)
