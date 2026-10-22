"""A person's own API keys (``sk-ss-``): scope ``llm``, optional expiry; made through security/credentials.py.

Keys of dev environments (``sk-ss-env-``) are issued and ended with their environment and are not listed here.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import ApiKey, User
from ..schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut, ApiKeyRevealOut
from ..security import credentials, decrypt_api_key, encrypt_api_key
from ..services.audit import record

router = APIRouter(prefix="/api/keys", tags=["api-keys"])

MAX_KEYS_PER_USER = 20


def key_out(k: ApiKey, *, reveal: bool) -> ApiKeyOut:
    now = utcnow()
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        prefix=k.prefix,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
        revealable=reveal and k.key_encrypted is not None,
        kind=k.kind,
        scope=k.scope,
        expires_at=k.expires_at,
        expired=k.expires_at is not None and k.expires_at <= now,
        last_ip=k.last_ip,
    )


def _own_keys(user: User):
    return select(ApiKey).where(
        ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None), ApiKey.kind == credentials.KIND_API
    )


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    reveal = get_settings().api_key_reveal
    keys = (await db.execute(_own_keys(user).order_by(ApiKey.created_at.desc()))).scalars().all()
    return [key_out(k, reveal=reveal) for k in keys]


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_key(
    body: ApiKeyCreateIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    existing = (await db.execute(_own_keys(user).with_only_columns(ApiKey.id))).all()
    if len(existing) >= MAX_KEYS_PER_USER:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "too_many_keys", limit=MAX_KEYS_PER_USER)
    settings = get_settings()
    expires_at = utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    encrypt = (lambda raw: encrypt_api_key(raw, settings.secret_key)) if settings.api_key_reveal else None
    key, raw = await credentials.issue_api_key(
        db, user, name=body.name.strip(), scope=body.scope, expires_at=expires_at, encrypt=encrypt
    )
    record(db, user, "key.create", f"{user.username}/{key.name}", ip=request, prefix=key.prefix, scope=key.scope)
    await db.commit()
    return ApiKeyCreatedOut(**key_out(key, reveal=settings.api_key_reveal).model_dump(), key=raw)


@router.get("/{key_id}/reveal", response_model=ApiKeyRevealOut)
async def reveal_key(key_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The full key, for its owner, while admins allow it and a copy was kept when it was created."""
    settings = get_settings()
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id or key.revoked_at is not None or key.kind != credentials.KIND_API:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if not settings.api_key_reveal:
        raise ApiError(status.HTTP_403_FORBIDDEN, "key_reveal_disabled")
    raw = decrypt_api_key(key.key_encrypted, settings.secret_key) if key.key_encrypted else None
    if raw is None:
        raise ApiError(status.HTTP_409_CONFLICT, "key_not_stored")
    return ApiKeyRevealOut(key=raw)


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id or key.revoked_at is not None or key.kind != credentials.KIND_API:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    key.revoked_at = utcnow()
    record(db, user, "key.revoke", f"{user.username}/{key.name}", prefix=key.prefix)
    await db.commit()
