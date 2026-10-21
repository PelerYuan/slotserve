"""One-line node installers: ``/install/node.sh`` (Linux, macOS) and ``/install/node.ps1`` (Windows).

The scripts are templates in ``resources/install`` with this server's address, the node's token and the image
filled in, so a machine joins with ``curl -fsSL '<server>/install/node.sh?token=...' | bash`` or
``irm '<server>/install/node.ps1?token=...' | iex``. They are public (the machine has no session): a request
with a token that belongs to no node is refused, and without a token the script asks for one.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..errors import ApiError
from ..orm import Node
from . import protocol as p

router = APIRouter(tags=["nodes"])
TEMPLATES = Path(__file__).resolve().parent.parent / "resources" / "install"
AGENT_IMAGE = "ghcr.io/peleryuan/slotserve:latest"
TOKEN_RE = re.compile(r"^" + re.escape(p.TOKEN_PREFIX) + r"[A-Za-z0-9_-]{16,128}$")
SAFE_URL = re.compile(r"^https?://[A-Za-z0-9._~:/\[\]%@-]+$")


def server_url(request: Request) -> str:
    return (get_settings().public_url or str(request.base_url)).rstrip("/")


def one_liners(server: str, token: str) -> tuple[str, str]:
    """(Linux / macOS, Windows PowerShell) commands that install a node with this token."""
    sh = f"curl -fsSL {shlex.quote(f'{server}/install/node.sh?token={token}')} | bash"
    ps1 = f"irm '{server}/install/node.ps1?token={token}' | iex"
    return sh, ps1


async def _render(request: Request, name: str, token: str) -> str:
    server = server_url(request)
    if not SAFE_URL.match(server):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_public_url")
    if token:
        from .hub import hash_token

        if not TOKEN_RE.match(token):
            raise ApiError(status.HTTP_404_NOT_FOUND, "invalid_token")
        async with SessionLocal() as db:
            node = (await db.execute(select(Node).where(Node.token_hash == hash_token(token)))).scalar_one_or_none()
        if node is None or node.revoked_at is not None:
            raise ApiError(status.HTTP_404_NOT_FOUND, "invalid_token")
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in (("SERVER", server), ("TOKEN", token), ("IMAGE", AGENT_IMAGE)):
        text = text.replace(f"__SLOTSERVE_{key}__", value)
    return text


@router.get("/install/node.sh", response_class=PlainTextResponse, include_in_schema=False)
async def install_sh(request: Request, token: str = Query(default="", max_length=200)):
    text = (await _render(request, "node.sh", token)).replace("\r\n", "\n")  # bash chokes on CRLF
    return PlainTextResponse(
        text, media_type="text/x-shellscript; charset=utf-8", headers={"cache-control": "no-store"}
    )


@router.get("/install/node.ps1", response_class=PlainTextResponse, include_in_schema=False)
async def install_ps1(request: Request, token: str = Query(default="", max_length=200)):
    text = (await _render(request, "node.ps1", token)).replace("\r\n", "\n").replace("\n", "\r\n")
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8", headers={"cache-control": "no-store"})
