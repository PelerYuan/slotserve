import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import KeyValue


async def get(db: AsyncSession, key: str) -> Any:
    row = await db.get(KeyValue, key)
    return json.loads(row.value) if row else None


async def put(db: AsyncSession, key: str, value: Any) -> None:
    """Stores ``value`` (``None`` deletes the key). Caller commits."""
    row = await db.get(KeyValue, key)
    if value is None:
        if row is not None:
            await db.delete(row)
    elif row is None:
        db.add(KeyValue(key=key, value=json.dumps(value, default=str)))
    else:
        row.value = json.dumps(value, default=str)
