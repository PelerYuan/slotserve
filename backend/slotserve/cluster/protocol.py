"""The control plane <-> node agent protocol.

A node agent opens a WebSocket to ``/api/cluster/agent`` on the control plane with its node token
(``Authorization: Bearer <token>``). Every frame is one JSON object with a ``type``:

``hello`` (agent -> server, first frame)
    ``{"type": "hello", "protocol": 1, "agent_version": "...", "info": {...}, "inventory": {...},
    "runtimes": [...]}`` - who the agent is, what the machine has and what it is running right now.
    ``info.hardware`` (optional): CPU model and cores, memory, disks, OS, Docker and GPU driver (``system.hardware``).
``welcome`` (server -> agent)
    ``{"type": "welcome", "protocol": 1, "node_id": 3, "name": "gpu-b", "heartbeat_seconds": 5,
    "lanes": [{"lane_id": 2, "devices": [0]}]}`` - accepted; ``lanes`` are the lanes the node owns, anything
    else the agent still runs is stopped (reconciliation after a reconnect).
``error`` (server -> agent, then the socket closes)
    ``{"type": "error", "code": "protocol_mismatch" | "invalid_token" | "revoked" | ..., "message": "..."}``
``request`` / ``response`` (both directions; RPC with request ids and timeouts)
    ``{"type": "request", "id": "<uuid>", "method": "runtime.ensure", "params": {...}}``
    ``{"type": "response", "id": "<uuid>", "ok": true, "result": {...}}`` or
    ``{"type": "response", "id": "<uuid>", "ok": false, "error": {"code": "...", "message": "..."}}``
``event`` (agent -> server, no reply)
    ``{"type": "event", "event": "metrics" | "runtime" | "load_event" | "inventory" | "download", "data": {...}}``

The protocol version is a single integer. The server accepts agents speaking any version between
``MIN_PROTOCOL`` and ``PROTOCOL_VERSION``; otherwise both sides report a clear version mismatch.
New optional fields do not need a new version; removed or changed fields and methods do.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MIN_PROTOCOL = 1
AGENT_PATH = "/api/cluster/agent"
TOKEN_PREFIX = "ssn-"

# WebSocket close codes (4000-4999 are free for applications).
CLOSE_INVALID_TOKEN = 4001
CLOSE_REVOKED = 4002
CLOSE_PROTOCOL = 4003
CLOSE_REPLACED = 4004  # another connection with the same token took over
CLOSE_BAD_HELLO = 4005
FATAL_CLOSE_CODES = {CLOSE_INVALID_TOKEN, CLOSE_REVOKED, CLOSE_PROTOCOL}

# Methods the server calls on the agent.
M_RUNTIME_ENSURE = "runtime.ensure"  # {lane_id, devices, model, preset, est_load_seconds, force} -> runtime
M_RUNTIME_REMOVE = "runtime.remove"  # {lane_id} -> {}
M_RUNTIME_LIST = "runtime.list"  # {} -> {runtimes: [...]}
M_LANES_SYNC = "lanes.sync"  # {lanes: [{lane_id, devices}]} -> {removed: [...]}
M_LOG_READ = "log.read"  # {lane_id, start, end} -> {size, data (base64)}
M_LOG_TAIL = "log.tail"  # {lane_id, lines} -> {lines}
M_MODELS_SCAN = "models.scan"  # {} -> {files: [{path, name, size_bytes}]}
M_MODELS_STAT = "models.stat"  # {path, engine} -> {path, size_bytes, exists}
M_MODELS_DELETE = "models.delete"  # {path} -> {deleted}
M_DOWNLOAD_START = "download.start"  # {task_id, url, filename, headers, min_free_gb} -> {}
M_DOWNLOAD_CANCEL = "download.cancel"  # {task_id} -> {}
M_PING = "ping"  # {} -> {time}

# Events the agent sends.
E_METRICS = "metrics"  # {gpus, cpu_percent, memory_used_mb, memory_total_mb, load, net_rx_bps, net_tx_bps, disks}
E_RUNTIME = "runtime"  # one lane runtime's state (see RemoteRuntime.apply)
E_LOAD_EVENT = "load_event"  # a finished load attempt (ModelLoadEvent fields)
E_INVENTORY = "inventory"  # {gpus, cpu_cores, memory_total_mb, models: [...], ...}
E_DOWNLOAD = "download"  # {task_id, status, downloaded_bytes, total_bytes, speed_bps, error, path}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RemoteError(Exception):
    """An RPC failed on the other side (or never got an answer)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def compatible(version: Any) -> bool:
    return isinstance(version, int) and MIN_PROTOCOL <= version <= PROTOCOL_VERSION


def mismatch_message(remote: Any, side: str = "agent") -> str:
    supported = str(PROTOCOL_VERSION) if MIN_PROTOCOL == PROTOCOL_VERSION else f"{MIN_PROTOCOL}-{PROTOCOL_VERSION}"
    return (
        f"protocol version mismatch: the {side} speaks version {remote!r}, this side supports {supported}. "
        "Run the same SlotServe version on the control plane and the nodes."
    )


def new_id() -> str:
    return uuid.uuid4().hex


def request(method: str, params: dict | None = None, rid: str | None = None) -> dict:
    return {"type": "request", "id": rid or new_id(), "method": method, "params": params or {}}


def response_ok(rid: str, result: Any = None) -> dict:
    return {"type": "response", "id": rid, "ok": True, "result": result if result is not None else {}}


def response_error(rid: str, code: str, message: str) -> dict:
    return {"type": "response", "id": rid, "ok": False, "error": {"code": code, "message": message}}


def event(name: str, data: dict) -> dict:
    return {"type": "event", "event": name, "data": data}


def error(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def encode(message: dict) -> str:
    return json.dumps(message, separators=(",", ":"), default=str)


def decode(text: str | bytes) -> dict:
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("bad_frame", f"not JSON: {exc}") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("bad_frame", "frame is not an object with a type")
    return message


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str | None) -> bytes:
    return base64.b64decode(text or "")
