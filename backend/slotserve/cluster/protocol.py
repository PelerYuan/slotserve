"""The control plane <-> node agent protocol.

A node agent opens a WebSocket to ``/api/cluster/agent`` on the control plane with its node token
(``Authorization: Bearer <token>``). Every frame is one JSON object with a ``type``:

``hello`` (agent -> server, first frame)
    ``{"type": "hello", "protocol": 1, "agent_version": "...", "time": "<agent clock, ISO>", "info": {...},
    "inventory": {...}, "runtimes": [...], "downloads": [...]}`` - who the agent is, what the machine has and what
    it is running (and downloading) right now. Timestamps an agent sends are only used to measure its clock skew;
    the control plane records samples and events at the time it receives them.
``welcome`` (server -> agent)
    ``{"type": "welcome", "protocol": 1, "node_id": 3, "name": "gpu-b", "heartbeat_seconds": 5,
    "lanes": [{"lane_id": 2, "devices": [0]}], "server_version": "...", "server_time": "...",
    "libraries": [...], "downloads": [...]}`` - accepted; ``lanes`` are the lanes the node owns, anything
    else the agent still runs is stopped (reconciliation after a reconnect); ``downloads`` are the task ids the
    control plane still wants, anything else the agent downloads is cancelled.
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
CLOSE_RATE_LIMITED = 4006  # too many failed logins from this address; try again later
FATAL_CLOSE_CODES = {CLOSE_INVALID_TOKEN, CLOSE_REVOKED, CLOSE_PROTOCOL, CLOSE_RATE_LIMITED}

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
M_DOWNLOAD_LIST = "download.list"  # {} -> {downloads: [{task_id, status, ...}]}
M_LIBRARY_PREPARE = "library.prepare"  # {storage_id, path | volume: {name, driver_opts, labels}} -> {path}
M_TOKEN_ROTATE = "token.rotate"  # {token} -> {stored}: the agent uses the new token from now on
M_PING = "ping"  # {} -> {time}

# Events the agent sends.
E_METRICS = "metrics"  # {gpus: [...], cpu_percent, memory_used_mb, memory_total_mb, load}
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


AGENT_IMAGE = "ghcr.io/peleryuan/slotserve"


def update_commands(version: str) -> dict[str, str]:
    """How to bring a node agent to ``version`` (the control plane's), for each way of running it."""
    return {
        "compose": "docker compose -f docker-compose.node.yml pull && docker compose -f docker-compose.node.yml up -d",
        "docker": f"docker pull {AGENT_IMAGE}:{version}  # then re-create the slotserve-agent container with it",
        "pip": f"pip install --upgrade 'slotserve=={version}'  # then restart `slotserve agent`",
    }


def mismatch_message(remote: Any, side: str = "agent", server_version: str | None = None) -> str:
    supported = str(PROTOCOL_VERSION) if MIN_PROTOCOL == PROTOCOL_VERSION else f"{MIN_PROTOCOL}-{PROTOCOL_VERSION}"
    text = (
        f"protocol version mismatch: the {side} speaks version {remote!r}, this side supports {supported}. "
        "Run the same SlotServe version on the control plane and the nodes."
    )
    if server_version:
        text += f" Update the agent to {server_version}: {update_commands(server_version)['compose']}"
    return text


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    """``"0.8.1"`` -> ``(0, 8, 1)``; None when it is not a plain dotted version."""
    if not version:
        return None
    parts = []
    for piece in str(version).split("+")[0].split("-")[0].split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) or None


def version_status(agent: str | None, server: str) -> str:
    """``current`` | ``outdated`` (older than the control plane) | ``newer`` | ``unknown``."""
    a, s = version_tuple(agent), version_tuple(server)
    if a is None or s is None:
        return "unknown"
    if a == s:
        return "current"
    return "outdated" if a < s else "newer"


def fingerprint(der: bytes) -> str:
    """SHA-256 of a DER certificate as colon-separated upper-case hex (what browsers and openssl show)."""
    import hashlib

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(value: str) -> str:
    """Accepts ``AB:CD:...``, ``abcd...`` or ``sha256/AB:CD...``; returns the colon form ("" when empty)."""
    text = value.strip()
    if text.lower().startswith("sha256"):
        text = text[6:].lstrip("/:= ")
    digest = text.replace(":", "").replace(" ", "").upper()
    if not digest:
        return ""
    if len(digest) != 64 or any(c not in "0123456789ABCDEF" for c in digest):
        raise ValueError("a SHA-256 fingerprint has 64 hex digits")
    return ":".join(digest[i : i + 2] for i in range(0, 64, 2))


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
