"""Server-side sessions (R1-S3): the cookie holds a random token, the session lives in the ``sessions`` table.

``request.session`` works as before (a dict: ``uid``, the sudo entry of ``sudo.py``), but a session can now be
ended from the server: changing the password signs out the person's other browsers, disabling an account or an
admin's password reset signs out all of them, and people can list and end their own sessions.

The middleware loads the session for the request and, when the response starts, writes what changed:

* the session gets a user (sign-in) or another user -> a new session row and token (no session fixation);
* the session is emptied (sign-out, ``session.clear()``) -> the row is revoked and the cookie deleted;
* the data changed (sudo) -> the row is updated;
* otherwise ``last_seen_at`` and the 30-day expiry move on at most every ``TOUCH_SECONDS``.

It also refuses requests to SlotServe's API (``/api/``) that carry the session cookie together with
``Authorization: Bearer`` (400 ``mixed_credentials``): a bearer request is exempt from the CSRF checks, so it must
not also act with the cookie. Elsewhere the two never meet: ``/v1`` and ``/metrics`` only read the bearer token
(the cookie is ignored there), dev environment apps under ``/devenv/`` send their own headers.

SlotServe runs in one process; loaded sessions are cached for ``CACHE_SECONDS`` and every change made here
(``revoke_*``) drops the cache entries, so a revocation applies at once.
"""

import copy
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

from .. import db as dbmod
from ..db import utcnow
from ..orm import LoginSession, User

log = logging.getLogger(__name__)

SESSION_COOKIE = "slotserve_session"
UID_KEY = "uid"
SESSION_DAYS = 30  # sliding: a session unused for this long ends
TOUCH_SECONDS = 300
CACHE_SECONDS = 30.0
PRUNE_DAYS = 30  # ended sessions are deleted after this long
SCOPE_KEY = "slotserve.session"  # scope entry: the loaded _Entry (or None)
ROTATE_KEY = "slotserve.session_rotate"  # scope entry set by sign_in()
CHECKED_PREFIX = "/api/"  # where cookie and bearer token must not come together


@dataclass
class _Entry:
    id: int
    user_id: int
    token_hash: str
    data: dict
    last_seen_at: datetime
    loaded: float = field(default_factory=time.monotonic)


_cache: dict[str, _Entry] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _forget(*, ids: set[int] | None = None, user_id: int | None = None) -> None:
    for key, entry in list(_cache.items()):
        if (ids is not None and entry.id in ids) or (user_id is not None and entry.user_id == user_id):
            _cache.pop(key, None)


def sign_in(conn: HTTPConnection, user_id: int) -> None:
    """Starts a fresh session for ``user_id`` (sign-in, sign-up): earlier session data is dropped and the
    response carries a new token, also when the browser was signed in as the same person before."""
    conn.session.clear()
    conn.session[UID_KEY] = user_id
    conn.scope[ROTATE_KEY] = True


def current_id(conn: HTTPConnection) -> int | None:
    """The id of the session this request came with (None without a valid session)."""
    entry = conn.scope.get(SCOPE_KEY)
    return entry.id if entry is not None else None


async def revoke_user(db: AsyncSession, user_id: int, reason: str, *, except_id: int | None = None) -> int:
    """Ends the user's sessions (all of them, or all but ``except_id``); the caller commits."""
    stmt = update(LoginSession).where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
    if except_id is not None:
        stmt = stmt.where(LoginSession.id != except_id)
    result = await db.execute(stmt.values(revoked_at=utcnow(), revoked_reason=reason))
    _forget(user_id=user_id)
    return result.rowcount or 0


async def revoke_one(db: AsyncSession, user_id: int, session_id: int, reason: str = "revoked") -> bool:
    """Ends one of the user's sessions; False when there is no such live session."""
    result = await db.execute(
        update(LoginSession)
        .where(LoginSession.id == session_id, LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    _forget(ids={session_id})
    return (result.rowcount or 0) == 1


async def active_sessions(db: AsyncSession, user_id: int) -> list[LoginSession]:
    now = utcnow()
    stmt = (
        select(LoginSession)
        .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None), LoginSession.expires_at > now)
        .order_by(LoginSession.last_seen_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


def _cookie_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == name and v.strip():
                return v.strip()
    return None


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


async def _load(token: str) -> _Entry | None:
    token_hash = _hash(token)
    cached = _cache.get(token_hash)
    if cached is not None and time.monotonic() - cached.loaded < CACHE_SECONDS:
        return cached
    _cache.pop(token_hash, None)
    async with dbmod.SessionLocal() as db:
        row = (
            await db.execute(
                select(LoginSession)
                .join(User, User.id == LoginSession.user_id)
                .where(
                    LoginSession.token_hash == token_hash,
                    LoginSession.revoked_at.is_(None),
                    LoginSession.expires_at > utcnow(),
                    User.disabled_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    try:
        data = json.loads(row.data or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[UID_KEY] = row.user_id
    entry = _Entry(id=row.id, user_id=row.user_id, token_hash=token_hash, data=data, last_seen_at=row.last_seen_at)
    if len(_cache) > 5000:
        _cache.clear()
    _cache[token_hash] = entry
    return entry


class ServerSessionMiddleware:
    """ASGI middleware providing ``scope["session"]`` from the ``sessions`` table (see the module docstring)."""

    def __init__(self, app, *, https_only: bool = False, session_cookie: str = SESSION_COOKIE):
        self.app = app
        self.cookie = session_cookie
        self.flags = "httponly; samesite=lax" + ("; secure" if https_only else "")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        token = _cookie_value(headers, self.cookie)
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and token is not None
            and path.startswith(CHECKED_PREFIX)
            and _header(headers, b"authorization").strip().lower().startswith("bearer ")
        ):
            await _refuse_mixed(send)
            return
        entry = await _load(token) if token else None
        scope["session"] = copy.deepcopy(entry.data) if entry is not None else {}
        scope[SCOPE_KEY] = entry
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return
        original = copy.deepcopy(scope["session"])

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                try:
                    cookie = await self._commit(scope, entry, original, token)
                except Exception:  # never turn a finished request into a 500 because of bookkeeping
                    log.exception("could not save the session")
                    cookie = None
                if cookie is not None:
                    MutableHeaders(scope=message).append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _set(self, token: str) -> str:
        return f"{self.cookie}={token}; path=/; Max-Age={SESSION_DAYS * 86400}; {self.flags}"

    def _delete(self) -> str:
        return f"{self.cookie}=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.flags}"

    async def _commit(self, scope, entry: _Entry | None, original: dict, token: str | None) -> str | None:
        session = scope["session"]
        uid = session.get(UID_KEY)
        if not isinstance(uid, int) or not session:
            if entry is not None:
                async with dbmod.SessionLocal() as db:
                    await revoke_one(db, entry.user_id, entry.id, "logout")
                    await db.commit()
                return self._delete()
            return self._delete() if token is not None else None
        now = utcnow()
        if entry is None or token is None or entry.user_id != uid or scope.get(ROTATE_KEY):
            token = secrets.token_urlsafe(32)
            async with dbmod.SessionLocal() as db:
                if entry is not None:
                    await revoke_one(db, entry.user_id, entry.id, "replaced")
                await db.execute(
                    delete(LoginSession).where(
                        LoginSession.user_id == uid,
                        or_(
                            LoginSession.revoked_at < now - timedelta(days=PRUNE_DAYS),
                            LoginSession.expires_at < now - timedelta(days=PRUNE_DAYS),
                        ),
                    )
                )
                db.add(
                    LoginSession(
                        token_hash=_hash(token),
                        user_id=uid,
                        data=json.dumps(session, default=str),
                        created_at=now,
                        last_seen_at=now,
                        expires_at=now + timedelta(days=SESSION_DAYS),
                        ip=_client_ip(scope),
                        user_agent=_header(scope.get("headers", []), b"user-agent")[:255] or None,
                    )
                )
                await db.commit()
            return self._set(token)
        changed = session != original
        stale = (now - entry.last_seen_at).total_seconds() >= TOUCH_SECONDS
        if not changed and not stale:
            return None
        values = {"last_seen_at": now, "expires_at": now + timedelta(days=SESSION_DAYS), "ip": _client_ip(scope)}
        if changed:
            values["data"] = json.dumps(session, default=str)
        async with dbmod.SessionLocal() as db:
            result = await db.execute(
                update(LoginSession)
                .where(LoginSession.id == entry.id, LoginSession.revoked_at.is_(None))
                .values(**values)
            )
            await db.commit()
        if (result.rowcount or 0) == 0:  # ended meanwhile (e.g. by this very request)
            _forget(ids={entry.id})
            return None
        entry.data = copy.deepcopy(session)
        entry.last_seen_at = now
        return self._set(token) if stale else None  # the browser keeps the cookie as long as the session


def _client_ip(scope) -> str | None:
    client = scope.get("client")
    return client[0][:64] if client else None


async def _refuse_mixed(send) -> None:
    body = json.dumps(
        {"detail": {"code": "mixed_credentials", "params": {}}},
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))],
        }
    )
    await send({"type": "http.response.body", "body": body})
