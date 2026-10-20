"""OpenAI-compatible gateway: authenticate, admit, forward to llama-server, record usage."""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from ..db import SessionLocal, utcnow
from ..orm import ApiKey, LlmModel, UsageLog, User
from ..runtime.manager import LlamaRuntime, RuntimeState
from ..security import hash_api_key
from ..services import queries

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["openai"])

USAGE_STREAM_ENDPOINTS = {"chat/completions", "completions"}
_background: set[asyncio.Task] = set()


def openai_error(status: int, message: str, code: str, headers: dict | None = None) -> JSONResponse:
    err_type = {
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        409: "invalid_request_error",
        503: "service_unavailable",
    }.get(status, "api_error")
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "code": code, "param": None}},
        status_code=status,
        headers=headers,
    )


class GatewayError(Exception):
    def __init__(self, response: JSONResponse):
        self.response = response


@dataclass
class Caller:
    user_id: int
    username: str
    api_key_id: int


@dataclass
class Admission:
    caller: Caller
    reservation_id: int | None
    model_name: str
    runtime: LlamaRuntime


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


async def authenticate(request: Request) -> Caller:
    raw = _bearer(request)
    if not raw:
        raise GatewayError(openai_error(401, "Missing API key. Use 'Authorization: Bearer <key>'.", "missing_api_key"))
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(ApiKey, User.username)
                .join(User, User.id == ApiKey.user_id)
                .where(ApiKey.key_hash == hash_api_key(raw), ApiKey.revoked_at.is_(None), User.disabled_at.is_(None))
            )
        ).first()
        if row is None:
            raise GatewayError(openai_error(401, "Invalid or revoked API key.", "invalid_api_key"))
        key, username = row
        key.last_used_at = utcnow()
        await db.commit()
        return Caller(user_id=key.user_id, username=username, api_key_id=key.id)


async def admit(request: Request, caller: Caller, requested_model: str | None) -> Admission:
    runtime: LlamaRuntime = request.app.state.runtime
    target = request.app.state.scheduler.target
    now = utcnow()
    async with SessionLocal() as db:
        active = await queries.active_reservation(db, now)
        if active is not None and active.user_id != caller.user_id:
            owner = await db.get(User, active.user_id)
            raise GatewayError(
                openai_error(
                    403,
                    f"The GPU is reserved by '{owner.username if owner else '?'}' until "
                    f"{active.end_at.isoformat()}. Book a slot to use it.",
                    "gpu_reserved",
                )
            )
        if active is not None and active.model_id is None:
            raise GatewayError(
                openai_error(
                    503,
                    "Your booking is exclusive: llama-server is stopped so the whole GPU is free for other work.",
                    "gpu_exclusive",
                )
            )

        snap = runtime.snapshot()
        if active is not None and (
            target is None
            or target.reservation is None
            or target.reservation.id != active.id
            or (snap.model_id, snap.preset_id) != target.spec
        ):
            reserved = await db.get(LlmModel, active.model_id)
            raise _unavailable(snap, f"Your reserved model '{reserved.name if reserved else '?'}' is loading.")
        if snap.state != RuntimeState.READY:
            raise _unavailable(snap, _state_message(snap))
        if snap.draining:
            raise GatewayError(
                openai_error(
                    503,
                    "The model is about to be switched; waiting for running requests to finish.",
                    "model_switching",
                    headers={"Retry-After": str(max(5, request.app.state.settings.drain_seconds))},
                )
            )

        if requested_model and requested_model != snap.model_name:
            other = (
                await db.execute(
                    select(LlmModel.id).where(LlmModel.name == requested_model, LlmModel.deleted_at.is_(None))
                )
            ).first()
            if other is not None:
                raise GatewayError(
                    openai_error(
                        409,
                        f"Model '{requested_model}' is not loaded (current: '{snap.model_name}'). "
                        "Reserve a slot to switch models.",
                        "model_not_loaded",
                    )
                )
        return Admission(
            caller=caller, reservation_id=active.id if active else None, model_name=snap.model_name, runtime=runtime
        )


def _state_message(snap) -> str:
    if snap.state == RuntimeState.LOADING:
        return f"Model '{snap.model_name}' is loading."
    if snap.state == RuntimeState.FAILED:
        return f"Model '{snap.model_name}' failed to start: {snap.error}"
    return "No model is loaded right now."


def _unavailable(snap, message: str) -> GatewayError:
    retry = 30
    if snap.state == RuntimeState.LOADING and snap.load_started_at and snap.est_load_seconds:
        elapsed = (utcnow() - snap.load_started_at).total_seconds()
        retry = max(5, math.ceil(snap.est_load_seconds - elapsed))
    return GatewayError(openai_error(503, message, "model_unavailable", headers={"Retry-After": str(retry)}))


def _record_usage(admission: Admission, endpoint: str, stream: bool, usage: dict | None, started: float, status: int):
    async def write():
        try:
            async with SessionLocal() as db:
                db.add(
                    UsageLog(
                        user_id=admission.caller.user_id,
                        api_key_id=admission.caller.api_key_id,
                        reservation_id=admission.reservation_id,
                        model_name=admission.model_name or "",
                        endpoint=endpoint,
                        stream=stream,
                        prompt_tokens=int((usage or {}).get("prompt_tokens") or 0),
                        completion_tokens=int((usage or {}).get("completion_tokens") or 0),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        status_code=status,
                    )
                )
                await db.commit()
        except Exception:
            log.exception("failed to record usage")

    task = asyncio.get_running_loop().create_task(write())
    _background.add(task)
    task.add_done_callback(_background.discard)


class _SSEUsageSniffer:
    """Watches SSE bytes passing through and remembers the last ``usage`` object."""

    def __init__(self):
        self._buf = b""
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]" or b'"usage"' not in payload:
                continue
            try:
                usage = json.loads(payload).get("usage")
            except (ValueError, AttributeError):
                continue
            if isinstance(usage, dict):
                self.usage = usage


@router.get("/models")
async def list_models(request: Request):
    try:
        await authenticate(request)
    except GatewayError as e:
        return e.response
    snap = request.app.state.runtime.snapshot()
    data = []
    if snap.model_name and snap.state in (RuntimeState.READY, RuntimeState.LOADING):
        data.append(
            {
                "id": snap.model_name,
                "object": "model",
                "created": int((snap.load_started_at or utcnow()).timestamp()),
                "owned_by": "slotserve",
                "status": snap.state.value,
            }
        )
    return {"object": "list", "data": data}


@router.post("/{path:path}")
async def proxy(path: str, request: Request):
    started = time.monotonic()
    raw = await request.body()
    body = None
    if raw and "json" in request.headers.get("content-type", "application/json"):
        try:
            body = json.loads(raw)
        except ValueError:
            return openai_error(400, "Request body is not valid JSON.", "invalid_json")
    requested = body.get("model") if isinstance(body, dict) else None

    try:
        caller = await authenticate(request)
        admission = await admit(request, caller, requested if isinstance(requested, str) else None)
    except GatewayError as e:
        return e.response

    runtime = admission.runtime
    runtime.begin_request()
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            runtime.end_request()

    try:
        return await _forward(request, path, body, raw, admission, started, release)
    except BaseException:
        release()
        raise


async def _forward(request: Request, path: str, body, raw: bytes, admission: Admission, started: float, release):
    """Relays one admitted request; ``release`` is called once the response is complete."""

    stream = isinstance(body, dict) and bool(body.get("stream"))
    changed = False
    # Engines such as vLLM insist on their served model name; route by name, then send the real one.
    if isinstance(body, dict) and admission.model_name and body.get("model") != admission.model_name:
        body["model"] = admission.model_name
        changed = True
    if stream and path in USAGE_STREAM_ENDPOINTS:
        opts = body.get("stream_options") or {}
        if isinstance(opts, dict) and "include_usage" not in opts:
            body["stream_options"] = {**opts, "include_usage": True}
            changed = True
    if changed:
        raw = json.dumps(body).encode()

    client: httpx.AsyncClient = request.app.state.http
    url = f"{admission.runtime.base_url}/v1/{path}"
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if accept := request.headers.get("accept"):
        headers["accept"] = accept
    upstream_req = client.build_request("POST", url, content=raw, headers=headers, params=request.query_params)
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        release()
        _record_usage(admission, path, stream, None, started, 502)
        return openai_error(502, f"Upstream model server error: {exc.__class__.__name__}", "upstream_error")

    content_type = upstream.headers.get("content-type", "application/json")
    if "text/event-stream" in content_type:
        sniffer = _SSEUsageSniffer()

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    sniffer.feed(chunk)
                    yield chunk
            finally:
                release()
                await upstream.aclose()
                _record_usage(admission, path, True, sniffer.usage, started, upstream.status_code)

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    try:
        content = await upstream.aread()
    finally:
        await upstream.aclose()
    usage = None
    if "json" in content_type:
        try:
            parsed = json.loads(content)
            usage = parsed.get("usage") if isinstance(parsed, dict) else None
        except ValueError:
            pass
    release()
    _record_usage(admission, path, False, usage, started, upstream.status_code)
    return Response(content=content, status_code=upstream.status_code, media_type=content_type)
