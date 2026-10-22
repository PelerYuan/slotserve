"""Per-person limits (F07): the global booking limits plus temporary extra hours an admin grants one person.

``policy_for`` is the one place that answers "what may this person book" (A03 ``policy_for``); booking checks
(``booking.check_quota``) and the change preview (``settings_impact``) read it. Grants live in the key-value store
(``quota_grants:<user id>``), one JSON list per person; a grant adds ``extra_hours`` to the weekly quota of every
quota week that overlaps ``[starts_at, until)`` and stops counting by itself after ``until``. Policy profiles
(M2) will move this into tables.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db import utcnow
from ..orm import User
from . import kv

KEEP_EXPIRED = timedelta(days=180)  # expired or revoked grants stay visible this long


def _key(user_id: int) -> str:
    return f"quota_grants:{user_id}"


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    from .booking import aware

    return aware(datetime.fromisoformat(value))


@dataclass(frozen=True)
class Grant:
    id: str
    user_id: int
    extra_hours: float
    starts_at: datetime
    until: datetime
    reason: str
    by: str
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str = ""

    def counts_for(self, start: datetime, end: datetime) -> bool:
        """Whether the grant applies to the period ``[start, end)`` (a quota week)."""
        return self.revoked_at is None and self.starts_at < end and self.until > start

    def active(self, at: datetime) -> bool:
        return self.revoked_at is None and self.starts_at <= at < self.until

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "extra_hours": self.extra_hours,
            "starts_at": self.starts_at.isoformat(),
            "until": self.until.isoformat(),
            "reason": self.reason,
            "by": self.by,
            "created_at": self.created_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_by": self.revoked_by,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Grant":
        return cls(
            id=str(d["id"]),
            user_id=int(d["user_id"]),
            extra_hours=float(d["extra_hours"]),
            starts_at=_dt(d["starts_at"]),
            until=_dt(d["until"]),
            reason=str(d.get("reason") or ""),
            by=str(d.get("by") or ""),
            created_at=_dt(d["created_at"]),
            revoked_at=_dt(d.get("revoked_at")),
            revoked_by=str(d.get("revoked_by") or ""),
        )


async def grants(db: AsyncSession, user_id: int) -> list[Grant]:
    """All grants of a person that are still worth showing (newest first)."""
    raw = await kv.get(db, _key(user_id)) or []
    out = []
    for item in raw:
        try:
            out.append(Grant.from_json(item))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda g: g.created_at, reverse=True)


async def _save(db: AsyncSession, user_id: int, items: list[Grant]) -> None:
    now = utcnow()
    keep = [g for g in items if (g.revoked_at or g.until) > now - KEEP_EXPIRED]
    await kv.put(db, _key(user_id), [g.to_json() for g in keep] or None)


async def add_grant(
    db: AsyncSession,
    user: User,
    extra_hours: float,
    until: datetime,
    reason: str,
    by: User,
    starts_at: datetime | None = None,
) -> Grant:
    """Gives ``user`` ``extra_hours`` on top of the weekly quota until ``until``. Caller commits."""
    now = utcnow()
    grant = Grant(
        id=uuid.uuid4().hex[:12],
        user_id=user.id,
        extra_hours=float(extra_hours),
        starts_at=starts_at or now,
        until=until,
        reason=reason.strip(),
        by=by.username,
        created_at=now,
    )
    await _save(db, user.id, [grant, *await grants(db, user.id)])
    return grant


async def revoke_grant(db: AsyncSession, user: User, grant_id: str, by: User) -> Grant | None:
    """Ends a grant now; ``None`` when there is no such (unrevoked) grant. Caller commits."""
    items = await grants(db, user.id)
    found = None
    for i, g in enumerate(items):
        if g.id == grant_id and g.revoked_at is None:
            found = Grant(**{**g.__dict__, "revoked_at": utcnow(), "revoked_by": by.username})
            items[i] = found
    if found is not None:
        await _save(db, user.id, items)
    return found


async def delete_all(db: AsyncSession, user_id: int) -> None:
    await kv.put(db, _key(user_id), None)


def extra_hours(items: list[Grant], start: datetime, end: datetime) -> float:
    """Extra weekly hours the grants give for the quota week ``[start, end)``."""
    return sum(g.extra_hours for g in items if g.counts_for(start, end))


@dataclass(frozen=True)
class Limits:
    """What one person may book now. 0 means unlimited, as in the settings."""

    exempt: bool  # admins: no quota
    quota_weekly_hours: float  # including the extra hours of the current week
    base_weekly_hours: int
    extra_hours: float
    quota_upcoming: int
    max_reservation_hours: int
    max_advance_days: int
    grants: list[Grant]


async def policy_for(db: AsyncSession, settings: Settings, user: User, at: datetime | None = None) -> Limits:
    from .booking import week_bounds

    at = at or utcnow()
    items = await grants(db, user.id)
    start, end = week_bounds(settings, at)
    extra = extra_hours(items, start, end) if settings.quota_weekly_hours else 0.0
    return Limits(
        exempt=user.is_admin,
        quota_weekly_hours=(settings.quota_weekly_hours + extra) if settings.quota_weekly_hours else 0,
        base_weekly_hours=settings.quota_weekly_hours,
        extra_hours=extra,
        quota_upcoming=settings.quota_upcoming,
        max_reservation_hours=settings.max_reservation_hours,
        max_advance_days=settings.max_advance_days,
        grants=items,
    )
