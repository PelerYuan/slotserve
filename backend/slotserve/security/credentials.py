"""The credential family (A06): one set of rules for every secret SlotServe hands out.

=================  ===============  ========================================================================
credential         prefix           rules
=================  ===============  ========================================================================
API key            ``sk-ss-``       made by its owner; scope ``llm`` (the ``/v1`` API); optional expiry
environment key    ``sk-ss-env-``   issued to one dev environment; ``llm`` only; dies when the environment
                                    stops (and after ``ENV_KEY_MAX_HOURS`` at the latest, extended with it)
invite             ``ssi-``         admin-made sign-up link: expires, limited uses, revocable, optional
                                    approval of the people who used it
PAT                ``sk-ss-pat-``   reserved (M2, F12); never an admin scope (D15)
=================  ===============  ========================================================================

Every credential is stored as a SHA-256 hash only (API keys may keep an encrypted copy, ``api_key_reveal``),
can be revoked, and never appears in a URL query, except invites: they are limited and short-lived.
Callers own the transaction: the functions here add or change rows, the caller commits.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import utcnow
from ..orm import ApiKey, DevSession, Invite, User

API_KEY_PREFIX = "sk-ss-"
ENV_KEY_PREFIX = "sk-ss-env-"
PAT_PREFIX = "sk-ss-pat-"  # reserved for platform tokens (F12)
INVITE_PREFIX = "ssi-"

KIND_API = "api"
KIND_ENV = "env"

SCOPE_LLM = "llm"
# Scopes an API key may carry. There is no admin scope and there never will be (D15): admin actions need a
# signed-in browser session (and sudo for the dangerous ones).
KEY_SCOPES = frozenset({SCOPE_LLM})

ENV_KEY_MAX_HOURS = 24
# Dev environment states in which its key works (the key is issued while the container is being created).
ENV_ACTIVE_STATES = frozenset({"starting", "running"})

INVITE_DEFAULT_DAYS = 7
INVITE_MAX_DAYS = 90
INVITE_DEFAULT_USES = 50
INVITE_MAX_USES = 1000


class CredentialError(Exception):
    """A credential that cannot be used; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def new_secret(prefix: str, nbytes: int = 24) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def digest(raw: str) -> str:
    """The stored form of a credential (same as API keys always used, so existing keys keep working)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def display_prefix(raw: str, prefix: str = API_KEY_PREFIX) -> str:
    """What lists show of a credential: its type prefix and four more characters."""
    return raw[: len(prefix) + 4]


def normalize_scope(scope: str) -> str:
    """A key's scope string (space separated). Raises ValueError for unknown scopes, admin included."""
    parts = sorted({p for p in scope.split() if p})
    if not parts:
        raise ValueError("empty scope")
    unknown = [p for p in parts if p not in KEY_SCOPES]
    if unknown:
        raise ValueError(f"scope not allowed: {', '.join(unknown)}")
    return " ".join(parts)


def has_scope(granted: str, wanted: str) -> bool:
    return wanted in granted.split()


# ---- API keys --------------------------------------------------------------------------------------------------


async def issue_api_key(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    scope: str = SCOPE_LLM,
    expires_at: datetime | None = None,
    encrypt=None,
) -> tuple[ApiKey, str]:
    """A new API key for ``user``; returns the row (added, not committed) and the key, shown once.
    ``encrypt(raw) -> str`` keeps a viewable copy (``api_key_reveal``)."""
    raw = new_secret(API_KEY_PREFIX)
    key = ApiKey(
        user_id=user.id,
        name=name,
        prefix=display_prefix(raw),
        key_hash=digest(raw),
        kind=KIND_API,
        scope=normalize_scope(scope),
        expires_at=expires_at,
    )
    if encrypt is not None:
        key.key_encrypted = encrypt(raw)
    db.add(key)
    await db.flush()
    return key, raw


@dataclass(frozen=True)
class VerifiedKey:
    key_id: int
    user_id: int
    username: str
    kind: str
    scope: str
    devenv_session_id: int | None
    last_used_at: datetime | None


async def verify_api_key(db: AsyncSession, raw: str, *, scope: str = SCOPE_LLM) -> VerifiedKey:
    """The key behind ``raw`` when it may be used for ``scope`` now; otherwise CredentialError with code
    ``invalid_api_key`` (unknown, revoked, owner disabled, environment stopped), ``expired_api_key`` or
    ``insufficient_scope``."""
    row = (
        await db.execute(
            select(ApiKey, User.username, User.disabled_at, User.approval_pending)
            .join(User, User.id == ApiKey.user_id)
            .where(ApiKey.key_hash == digest(raw))
        )
    ).first()
    if row is None:
        raise CredentialError("invalid_api_key", "Invalid or revoked API key.")
    key, username, disabled_at, pending = row
    if key.revoked_at is not None or disabled_at is not None or pending:
        raise CredentialError("invalid_api_key", "Invalid or revoked API key.")
    if key.expires_at is not None and key.expires_at <= utcnow():
        raise CredentialError("expired_api_key", "This API key has expired.")
    if key.kind == KIND_ENV:
        state = None
        if key.devenv_session_id is not None:
            state = (
                await db.execute(select(DevSession.state).where(DevSession.id == key.devenv_session_id))
            ).scalar_one_or_none()
        if state not in ENV_ACTIVE_STATES:
            raise CredentialError("invalid_api_key", "This environment key ended with its environment.")
    if not has_scope(key.scope, scope):
        raise CredentialError("insufficient_scope", f"This key cannot be used for '{scope}'.")
    return VerifiedKey(
        key_id=key.id,
        user_id=key.user_id,
        username=username,
        kind=key.kind,
        scope=key.scope,
        devenv_session_id=key.devenv_session_id,
        last_used_at=key.last_used_at,
    )


# ---- environment keys (F11; used by the platform_env contributor, W1-11) --------------------------------------


def _env_expiry(until: datetime | None) -> datetime:
    cap = utcnow() + timedelta(hours=ENV_KEY_MAX_HOURS)
    return cap if until is None or until > cap else until


async def issue_env_key(db: AsyncSession, devenv_session_id: int, *, until: datetime | None = None) -> str:
    """A fresh ``sk-ss-env-`` key for one dev environment, acting as its owner, scope ``llm`` only.

    Earlier keys of the environment are revoked (a restart gets a new one). The key works while the environment
    is starting or running and until ``until`` (the booking's end), at most ``ENV_KEY_MAX_HOURS`` from now;
    ``extend_env_keys`` moves that on. Shared environments of idle lanes have no owner and get no key
    (CredentialError ``env_key_shared``). Returns the key; the caller commits and writes it into the container
    (never into Docker ``Env``, R1-11).
    """
    s = await db.get(DevSession, devenv_session_id)
    if s is None:
        raise CredentialError("not_found", "no such environment")
    if s.kind == "shared" or s.user_id is None:
        raise CredentialError("env_key_shared", "shared environments get no key")
    await revoke_env_keys(db, devenv_session_id)
    raw = new_secret(ENV_KEY_PREFIX)
    db.add(
        ApiKey(
            user_id=s.user_id,
            name=f"environment {devenv_session_id}",
            prefix=display_prefix(raw, ENV_KEY_PREFIX),
            key_hash=digest(raw),
            kind=KIND_ENV,
            scope=SCOPE_LLM,
            expires_at=_env_expiry(until),
            devenv_session_id=devenv_session_id,
        )
    )
    await db.flush()
    return raw


async def extend_env_keys(db: AsyncSession, devenv_session_id: int, until: datetime) -> int:
    """Moves the end of the environment's live keys to ``until`` (capped as in ``issue_env_key``)."""
    result = await db.execute(
        update(ApiKey)
        .where(ApiKey.devenv_session_id == devenv_session_id, ApiKey.kind == KIND_ENV, ApiKey.revoked_at.is_(None))
        .values(expires_at=_env_expiry(until))
    )
    return result.rowcount or 0


async def revoke_env_keys(db: AsyncSession, devenv_session_id: int) -> int:
    """Revokes the environment's keys (the environment stopped). Returns how many were live."""
    result = await db.execute(
        update(ApiKey)
        .where(ApiKey.devenv_session_id == devenv_session_id, ApiKey.kind == KIND_ENV, ApiKey.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return result.rowcount or 0


# ---- invites (F06, R1-11) ------------------------------------------------------------------------------------


async def issue_invite(
    db: AsyncSession,
    creator: User,
    *,
    days: int = INVITE_DEFAULT_DAYS,
    max_uses: int = INVITE_DEFAULT_USES,
    requires_approval: bool = False,
    note: str = "",
) -> tuple[Invite, str]:
    """A new invite link token; returns the row (added) and the token, shown once."""
    if not 1 <= days <= INVITE_MAX_DAYS or not 1 <= max_uses <= INVITE_MAX_USES:
        raise ValueError("days or max_uses out of range")
    raw = new_secret(INVITE_PREFIX)
    invite = Invite(
        token_hash=digest(raw),
        prefix=display_prefix(raw, INVITE_PREFIX),
        note=note.strip(),
        created_by=creator.id,
        expires_at=utcnow() + timedelta(days=days),
        max_uses=max_uses,
        uses=0,
        requires_approval=requires_approval,
    )
    db.add(invite)
    await db.flush()
    return invite, raw


def invite_state(invite: Invite, now: datetime | None = None) -> str:
    """active | expired | used_up | revoked"""
    now = now or utcnow()
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    if invite.uses >= invite.max_uses:
        return "used_up"
    return "active"


INVITE_ERRORS = {
    "revoked": "invite_revoked",
    "expired": "invite_expired",
    "used_up": "invite_used_up",
}


async def check_invite(db: AsyncSession, raw: str) -> Invite:
    """The usable invite behind ``raw``; CredentialError ``invite_invalid`` / ``invite_expired`` /
    ``invite_used_up`` / ``invite_revoked`` otherwise."""
    raw = raw.strip()
    invite = None
    if raw.startswith(INVITE_PREFIX):
        invite = (await db.execute(select(Invite).where(Invite.token_hash == digest(raw)))).scalar_one_or_none()
    if invite is None:
        raise CredentialError("invite_invalid")
    state = invite_state(invite)
    if state != "active":
        raise CredentialError(INVITE_ERRORS[state])
    return invite


async def consume_invite(db: AsyncSession, invite_id: int) -> bool:
    """Counts one use, atomically: False when the invite was used up, expired or revoked meanwhile."""
    now = utcnow()
    result = await db.execute(
        update(Invite)
        .where(
            Invite.id == invite_id,
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
            Invite.uses < Invite.max_uses,
        )
        .values(uses=Invite.uses + 1, last_used_at=now)
    )
    return (result.rowcount or 0) == 1


async def revoke_invite(db: AsyncSession, invite: Invite) -> None:
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
