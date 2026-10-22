from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import ApiKey, User
from ..schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut, ApiKeyRevealOut
from ..security import decrypt_api_key, encrypt_api_key, generate_api_key, hash_api_key, key_display_prefix
from ..services.audit import record

router = APIRouter(prefix="/api/keys", tags=["api-keys"])

MAX_KEYS_PER_USER = 20


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    stmt = (
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)).order_by(ApiKey.created_at.desc())
    )
    reveal = get_settings().api_key_reveal
    return [
        ApiKeyOut(
            id=k.id,
            name=k.name,
            prefix=k.prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            revealable=reveal and k.key_encrypted is not None,
        )
        for k in (await db.execute(stmt)).scalars().all()
    ]


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_key(
    body: ApiKeyCreateIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    existing = (await db.execute(select(ApiKey.id).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)))).all()
    if len(existing) >= MAX_KEYS_PER_USER:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "too_many_keys", limit=MAX_KEYS_PER_USER)
    raw = generate_api_key()
    settings = get_settings()
    key = ApiKey(user_id=user.id, name=body.name.strip(), prefix=key_display_prefix(raw), key_hash=hash_api_key(raw))
    if settings.api_key_reveal:
        key.key_encrypted = encrypt_api_key(raw, settings.secret_key)
    db.add(key)
    record(db, user, "key.create", f"{user.username}/{key.name}", ip=request, prefix=key.prefix)
    await db.commit()
    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        created_at=key.created_at,
        last_used_at=None,
        revealable=key.key_encrypted is not None,
        key=raw,
    )


@router.get("/{key_id}/reveal", response_model=ApiKeyRevealOut)
async def reveal_key(key_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The full key, for its owner, while admins allow it and a copy was kept when it was created."""
    settings = get_settings()
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id or key.revoked_at is not None:
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
    if key is None or key.user_id != user.id or key.revoked_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    key.revoked_at = utcnow()
    await db.commit()
