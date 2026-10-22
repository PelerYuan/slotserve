"""The in-app inbox (the bell): every notification lands here before any chat platform (services/notify.py).

Members see only their own events; admins also see the admin events (alerts, approvals and other to-dos),
grouped under ``category`` admin. Rows with no recipient (``user_id`` NULL) are shared by all admins.
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db, utcnow
from ..deps import current_user
from ..orm import Notification, User

router = APIRouter(prefix="/api/me/notifications", tags=["inbox"])

CATEGORIES = ("personal", "admin")


class NotificationOut(BaseModel):
    id: int
    event: str  # load_failed, booking_approved, check_alert, ... (title_key is the same)
    category: str  # personal | admin
    level: str  # info | success | warn | error
    title_key: str
    params: dict[str, Any]
    message: str  # rendered in the recipient's language when it was sent
    link: str | None  # a page of the web UI, e.g. /admin?tab=reservations
    interruption_id: int | None
    created_at: datetime
    read_at: datetime | None


class UnreadOut(BaseModel):
    total: int
    personal: int
    admin: int


class InboxOut(BaseModel):
    items: list[NotificationOut]
    unread: UnreadOut
    has_more: bool


class ReadIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False  # every unread message (of ``category`` if given)
    category: str | None = None


def _mine(user: User):
    if user.is_admin:
        return or_(Notification.user_id == user.id, Notification.user_id.is_(None))
    return Notification.user_id == user.id


def _out(n: Notification) -> NotificationOut:
    try:
        params = json.loads(n.params_json) if n.params_json else {}
    except ValueError:
        params = {}
    return NotificationOut(
        id=n.id,
        event=n.event,
        category=n.category,
        level=n.level,
        title_key=n.title_key,
        params=params if isinstance(params, dict) else {},
        message=n.message,
        link=n.link,
        interruption_id=n.interruption_id,
        created_at=n.created_at,
        read_at=n.read_at,
    )


async def unread_counts(db: AsyncSession, user: User) -> UnreadOut:
    rows = (
        await db.execute(
            select(Notification.category, func.count(Notification.id))
            .where(_mine(user), Notification.read_at.is_(None))
            .group_by(Notification.category)
        )
    ).all()
    counts = {c: 0 for c in CATEGORIES}
    for category, n in rows:
        counts[category if category in counts else "personal"] += n
    return UnreadOut(total=sum(counts.values()), **counts)


@router.get("", response_model=InboxOut)
async def inbox(
    unread: bool = False,
    category: str | None = Query(default=None, pattern="^(personal|admin)$"),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Newest first; ``before_id`` pages back. ``unread`` and ``category`` filter."""
    query = select(Notification).where(_mine(user))
    if unread:
        query = query.where(Notification.read_at.is_(None))
    if category:
        query = query.where(Notification.category == category)
    if before_id:
        query = query.where(Notification.id < before_id)
    rows = (await db.execute(query.order_by(Notification.id.desc()).limit(limit + 1))).scalars().all()
    return InboxOut(
        items=[_out(n) for n in rows[:limit]], unread=await unread_counts(db, user), has_more=len(rows) > limit
    )


@router.get("/unread", response_model=UnreadOut)
async def unread(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Badge counts (cheap enough to poll every few seconds)."""
    return await unread_counts(db, user)


@router.post("/read", response_model=UnreadOut)
async def mark_read(body: ReadIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Marks ``ids`` (or ``all``, optionally of one ``category``) as read; returns the new counts."""
    conditions = [_mine(user), Notification.read_at.is_(None)]
    if body.all:
        if body.category in CATEGORIES:
            conditions.append(Notification.category == body.category)
    elif body.ids:
        conditions.append(Notification.id.in_(body.ids))
    else:
        return await unread_counts(db, user)
    await db.execute(update(Notification).where(and_(*conditions)).values(read_at=utcnow()))
    await db.commit()
    return await unread_counts(db, user)
