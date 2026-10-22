"""Admin view of the model servers: live log, loading progress, failure details and load history."""

import asyncio
import json
import re

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import admin_user, get_lanes
from ..errors import ApiError
from ..orm import Lane, LlmModel, ModelLoadEvent, User
from ..runtime.lanes import LaneContext, LaneManager
from ..runtime.manager import RuntimeState
from ..schemas import LaneRuntimeOut, LoadEventOut
from ..services.diagnostics import Diagnosis, diagnose
from .status import _runtime_out

router = APIRouter(prefix="/api/admin/runtime", tags=["admin"])

STREAM_SECONDS = 60  # the browser reconnects and resumes from the last byte it saw
POLL_SECONDS = 0.5
TAIL_BYTES = 256 * 1024
LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def load_event_diagnosis(event: ModelLoadEvent) -> Diagnosis | None:
    """Likely cause and next steps of a failed load (None for a successful one), from what the event kept."""
    if event.success:
        return None
    return diagnose(event.log_excerpt, error=event.error, exit_code=event.exit_code)


def _lane(lanes: LaneManager, lane_id: int) -> LaneContext:
    ctx = lanes.contexts.get(lane_id)
    if ctx is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "lane_not_found")
    return ctx


@router.get("", response_model=list[LaneRuntimeOut])
async def runtime_overview(_: User = Depends(admin_user), lanes: LaneManager = Depends(get_lanes)):
    out = []
    for ctx in lanes.ordered():
        rt, target = ctx.runtime, ctx.scheduler.target
        out.append(
            LaneRuntimeOut(
                lane_id=ctx.id,
                lane_name=ctx.name,
                devices=ctx.devices,
                port=rt.port,
                log_file=rt.log_path.name,
                runtime=_runtime_out(rt.snapshot(), rt.log_progress()),
                engine=rt.engine,
                command=rt.command,
                pid=rt.pid,
                exit_code=rt.exit_code,
                inflight=rt.inflight,
                fail_count=rt.fail_count,
                next_retry_at=rt.next_retry_at,
                failure_log=rt.failure_log,
                target_model=target.model.name if target and target.model else None,
                target_reason=target.reason if target else None,
                diagnosis=diagnose(rt.failure_log, error=rt.error, exit_code=rt.exit_code)
                if rt.state == RuntimeState.FAILED
                else None,
            )
        )
    return out


@router.get("/events", response_model=list[LoadEventOut])
async def load_events(
    lane_id: int | None = None,
    failed: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
    _: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(ModelLoadEvent, LlmModel.name, Lane.name)
        .outerjoin(LlmModel, LlmModel.id == ModelLoadEvent.model_id)
        .outerjoin(Lane, Lane.id == ModelLoadEvent.lane_id)
        .order_by(ModelLoadEvent.started_at.desc(), ModelLoadEvent.id.desc())
        .limit(limit)
    )
    if lane_id is not None:
        stmt = stmt.where(ModelLoadEvent.lane_id == lane_id)
    if failed:
        stmt = stmt.where(ModelLoadEvent.success.is_(False))
    return [
        LoadEventOut(
            id=e.id,
            model_name=model,
            started_at=e.started_at,
            finished_at=e.finished_at,
            success=e.success,
            error=e.error,
            lane_id=e.lane_id,
            lane_name=lane,
            command=e.command,
            exit_code=e.exit_code,
            log_excerpt=e.log_excerpt,
            diagnosis=load_event_diagnosis(e),
        )
        for e, model, lane in (await db.execute(stmt)).all()
    ]


@router.get("/{lane_id}/log")
async def download_log(lane_id: int, _: User = Depends(admin_user), lanes: LaneManager = Depends(get_lanes)):
    path = _lane(lanes, lane_id).runtime.log_path
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, "log_not_found")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


def _split(data: bytes) -> tuple[list[str], bytes]:
    """Complete lines, plus the unfinished last line (llama.cpp prints progress dots without a newline)."""
    parts = LINE_BREAK.split(data)
    rest = parts.pop()
    return [p.decode("utf-8", errors="replace") for p in parts], rest


def _event(offset: int, lines: list[str], partial: bytes, reset: bool = False) -> str:
    body = {"lines": lines, "partial": partial.decode("utf-8", errors="replace"), "reset": reset}
    return f"id: {offset}\ndata: {json.dumps(body)}\n\n"


@router.get("/{lane_id}/stream")
async def stream_log(
    lane_id: int,
    request: Request,
    _: User = Depends(admin_user),
    lanes: LaneManager = Depends(get_lanes),
):
    """Server-sent events with new log lines. ``Last-Event-ID`` (a byte offset) resumes after a reconnect."""
    path = _lane(lanes, lane_id).runtime.log_path
    resume = request.headers.get("last-event-id", "")

    def size() -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def read(start: int, end: int) -> bytes:
        try:
            with path.open("rb") as f:
                f.seek(start)
                return f.read(end - start)
        except OSError:
            return b""

    async def events():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STREAM_SECONDS
        end = size()
        if resume.isdigit() and int(resume) <= end:
            pos, reset = int(resume), False
        else:
            # Fresh view: the last part of the file, starting at a line boundary.
            pos, reset = max(0, end - TAIL_BYTES), True
            if pos:
                head = await asyncio.to_thread(read, pos, end)
                cut = head.find(b"\n")
                pos += cut + 1 if cut >= 0 else 0
        buf = b""
        sent_partial = b""
        yield "retry: 1000\n\n"
        if reset:
            yield _event(pos, [], b"", reset=True)
        quiet = 0.0
        while loop.time() < deadline:
            if await request.is_disconnected():
                return
            end = size()
            if end < pos:  # the log was rotated or truncated
                pos, buf, sent_partial = 0, b"", b""
                yield _event(0, [], b"", reset=True)
            if end > pos:
                chunk = await asyncio.to_thread(read, pos, min(end, pos + 1024 * 1024))
                pos += len(chunk)
                lines, buf = _split(buf + chunk)
                if lines or buf != sent_partial:
                    # The event id points at the start of the unfinished line, so a resume repeats only that.
                    yield _event(pos - len(buf), lines, buf)
                    sent_partial = buf
                    quiet = 0.0
            quiet += POLL_SECONDS
            if quiet >= 15:
                yield ": keep-alive\n\n"
                quiet = 0.0
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
