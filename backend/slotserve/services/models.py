"""Model bookkeeping shared by the HTTP layer and services (catalog deploys, lanes)."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import Lane, LlmModel


async def sync_default_flags(db: AsyncSession) -> None:
    """Keeps ``models.is_default`` (used for sorting) in line with the lanes' default models."""
    ids = {mid for mid in (await db.execute(select(Lane.default_model_id))).scalars() if mid is not None}
    await db.execute(update(LlmModel).values(is_default=False))
    if ids:
        await db.execute(update(LlmModel).where(LlmModel.id.in_(ids)).values(is_default=True))
