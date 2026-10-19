from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..errors import ApiError
from ..orm import ApiKey, User
from ..schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut
from ..security import generate_api_key, hash_api_key, key_display_prefix

router = APIRouter(prefix="/api/keys", tags=["api-keys"])

MAX_KEYS_PER_USER = 20


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    stmt = (
        select(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .order_by(ApiKey.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_key(body: ApiKeyCreateIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    existing = (
        await db.execute(select(ApiKey.id).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)))
    ).all()
    if len(existing) >= MAX_KEYS_PER_USER:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "too_many_keys", limit=MAX_KEYS_PER_USER)
    raw = generate_api_key()
    key = ApiKey(user_id=user.id, name=body.name.strip(), prefix=key_display_prefix(raw), key_hash=hash_api_key(raw))
    db.add(key)
    await db.commit()
    return ApiKeyCreatedOut(
        id=key.id, name=key.name, prefix=key.prefix, created_at=key.created_at, last_used_at=None, key=raw
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user.id or key.revoked_at is not None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    key.revoked_at = utcnow()
    await db.commit()
