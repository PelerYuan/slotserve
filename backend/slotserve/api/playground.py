"""Playground (F05, D16): chat with a model from the web page, signed in, without creating an API key.

``POST /api/playground/chat`` uses the browser session (and so CsrfGuard), runs through the gateway's admission
and metering as the signed-in person with ``source=playground`` (gateway/proxy.py ``playground_chat``) and only
reaches a model that is already serving where the person may use it now: it never loads or switches a model and
never takes a booked GPU. Admins can switch it off, limit requests per person and minute (enforced here) and
restrict it to the default model.
"""

import math
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db import SessionLocal
from ..deps import current_user
from ..errors import ApiError
from ..gateway.proxy import GatewayError, playground_chat
from ..orm import LlmModel, User

router = APIRouter(prefix="/api/playground", tags=["playground"])

WINDOW_SECONDS = 60.0


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=32_000)


class ChatIn(BaseModel):
    """A subset of the OpenAI chat completion request; other fields are refused."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=128)  # None: the model the gateway would pick
    messages: list[Message] = Field(min_length=1, max_length=50)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    stream: bool = False


class PlaygroundOut(BaseModel):
    enabled: bool
    rate_per_minute: int
    default_model_only: bool
    default_model: str | None = None


class RateLimiter:
    """Requests per person in a sliding minute (in memory: SlotServe is one process)."""

    def __init__(self):
        self._hits: dict[int, deque[float]] = {}

    def hit(self, user_id: int, limit: int) -> float | None:
        """Counts a request; returns the seconds to wait instead when the limit is reached."""
        if limit <= 0:
            return None
        now = time.monotonic()
        q = self._hits.setdefault(user_id, deque())
        while q and q[0] <= now - WINDOW_SECONDS:
            q.popleft()
        if len(q) >= limit:
            return q[0] + WINDOW_SECONDS - now
        q.append(now)
        return None


def _limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "playground_limiter", None)
    if limiter is None:
        limiter = request.app.state.playground_limiter = RateLimiter()
    return limiter


async def _default_model() -> str | None:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(LlmModel.name).where(LlmModel.is_default.is_(True), LlmModel.deleted_at.is_(None)).limit(1)
            )
        ).scalar_one_or_none()


@router.get("", response_model=PlaygroundOut)
async def playground_state(request: Request, _: User = Depends(current_user)):
    """Whether the Playground is on and its limits (the page shows "copy code" instead when it is off)."""
    s = request.app.state.settings
    return PlaygroundOut(
        enabled=s.playground_enabled,
        rate_per_minute=s.playground_rate_per_minute,
        default_model_only=s.playground_default_model_only,
        default_model=await _default_model(),
    )


@router.post("/chat")
async def chat(body: ChatIn, request: Request, user: User = Depends(current_user)):
    """One chat completion; the response is the model's OpenAI-style JSON (or its event stream with
    ``stream: true``). Errors: 403 ``playground_disabled`` / ``playground_model_not_allowed``, 429
    ``playground_rate_limited`` (``seconds``), and the gateway's own codes (``model_not_loaded`` 409,
    ``gpu_reserved`` 403, ``model_unavailable`` / ``model_switching`` / ``gpu_exclusive`` 503) with ``message``
    and, when the gateway sends one, ``retry_after``."""
    settings = request.app.state.settings
    if not settings.playground_enabled:
        raise ApiError(status.HTTP_403_FORBIDDEN, "playground_disabled")
    wait = _limiter(request).hit(user.id, settings.playground_rate_per_minute)
    if wait is not None:
        raise ApiError(status.HTTP_429_TOO_MANY_REQUESTS, "playground_rate_limited", seconds=max(1, math.ceil(wait)))
    payload = body.model_dump(exclude_none=True)
    if settings.playground_default_model_only:
        default = await _default_model()
        if default is None or payload.get("model", default) != default:
            raise ApiError(status.HTTP_403_FORBIDDEN, "playground_model_not_allowed", model=default)
        payload["model"] = default
    try:
        return await playground_chat(request, user, payload)
    except GatewayError as exc:
        code, message = exc.detail
        params = {"message": message}
        if retry := exc.response.headers.get("retry-after"):
            params["retry_after"] = int(retry)
        raise ApiError(exc.status_code, code, **params) from None
