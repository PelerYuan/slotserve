import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..orm import AuditLog, User


def record(db: AsyncSession, actor: User | None, action: str, target: str = "", **detail: Any) -> None:
    """Adds an audit entry to the session; it is written with the caller's commit."""
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            actor_name=actor.username if actor else "",
            action=action,
            target=target,
            detail=json.dumps(detail, default=str, ensure_ascii=False),
        )
    )
