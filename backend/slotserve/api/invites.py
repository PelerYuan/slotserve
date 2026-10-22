"""Invite links (F06, R1-11): admins hand out ``ssi-`` tokens instead of the global invite code.

A link (``/login?invite=ssi-...``) expires (default 7 days), allows a number of sign-ups (default 50), can be
revoked, and can require an admin's approval of the people who use it (they cannot sign in until approved).
Sign-up itself is ``POST /api/auth/register`` with ``invite_token``.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import admin_user
from ..errors import ApiError
from ..orm import Invite, User
from ..security import credentials
from ..security.credentials import CredentialError
from ..services.audit import record

router = APIRouter(tags=["invites"])
admin = APIRouter(prefix="/api/admin/invites")


class InviteIn(BaseModel):
    note: str = Field(default="", max_length=64)
    days: int = Field(default=credentials.INVITE_DEFAULT_DAYS, ge=1, le=credentials.INVITE_MAX_DAYS)
    max_uses: int = Field(default=credentials.INVITE_DEFAULT_USES, ge=1, le=credentials.INVITE_MAX_USES)
    requires_approval: bool = False


class InviteUserOut(BaseModel):
    id: int
    username: str
    created_at: datetime
    approval_pending: bool


class InviteOut(BaseModel):
    id: int
    prefix: str
    note: str
    created_by: str | None
    created_at: datetime
    expires_at: datetime
    max_uses: int
    uses: int
    requires_approval: bool
    last_used_at: datetime | None
    revoked_at: datetime | None
    state: str  # active | expired | used_up | revoked
    users: list[InviteUserOut] = []


class InviteCreatedOut(InviteOut):
    token: str  # shown once
    path: str  # where the link points, relative to SlotServe's address: /login?invite=<token>


class InviteCheckIn(BaseModel):
    token: str = Field(max_length=128)


class InviteCheckOut(BaseModel):
    valid: bool
    code: str | None = None  # why not: invite_invalid | invite_expired | invite_used_up | invite_revoked
    requires_approval: bool = False
    expires_at: datetime | None = None


class PendingUserOut(InviteUserOut):
    invite_id: int | None
    invite_prefix: str | None = None
    invite_note: str | None = None


def invite_path(token: str) -> str:
    return f"/login?invite={token}"


async def _invite_rows(db: AsyncSession, invites: list[Invite]) -> list[InviteOut]:
    ids = [i.id for i in invites]
    users: dict[int, list[InviteUserOut]] = {i: [] for i in ids}
    if ids:
        for u in (await db.execute(select(User).where(User.invite_id.in_(ids)).order_by(User.created_at))).scalars():
            users[u.invite_id].append(
                InviteUserOut(
                    id=u.id, username=u.username, created_at=u.created_at, approval_pending=u.approval_pending
                )
            )
    creators = {i.created_by for i in invites if i.created_by is not None}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(creators)))).all())
    return [
        InviteOut(
            id=i.id,
            prefix=i.prefix,
            note=i.note,
            created_by=names.get(i.created_by),
            created_at=i.created_at,
            expires_at=i.expires_at,
            max_uses=i.max_uses,
            uses=i.uses,
            requires_approval=i.requires_approval,
            last_used_at=i.last_used_at,
            revoked_at=i.revoked_at,
            state=credentials.invite_state(i),
            users=users[i.id],
        )
        for i in invites
    ]


@admin.get("", response_model=list[InviteOut])
async def list_invites(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """All invite links, newest first, with who signed up through each."""
    invites = (await db.execute(select(Invite).order_by(Invite.created_at.desc(), Invite.id.desc()))).scalars()
    return await _invite_rows(db, list(invites))


@admin.post("", response_model=InviteCreatedOut, status_code=201)
async def create_invite(body: InviteIn, user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """A new invite link; the token is shown only in this response."""
    invite, token = await credentials.issue_invite(
        db, user, days=body.days, max_uses=body.max_uses, requires_approval=body.requires_approval, note=body.note
    )
    record(
        db,
        user,
        "invite.create",
        invite.note or invite.prefix,
        prefix=invite.prefix,
        days=body.days,
        max_uses=body.max_uses,
        requires_approval=body.requires_approval,
    )
    await db.commit()
    (row,) = await _invite_rows(db, [invite])
    return InviteCreatedOut(**row.model_dump(), token=token, path=invite_path(token))


@admin.delete("/{invite_id}", status_code=204)
async def revoke_invite(invite_id: int, user: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """The link stops working at once; accounts made with it stay."""
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    if invite.revoked_at is None:
        await credentials.revoke_invite(db, invite)
        record(db, user, "invite.revoke", invite.note or invite.prefix, prefix=invite.prefix, uses=invite.uses)
        await db.commit()


@admin.get("/pending", response_model=list[PendingUserOut])
async def pending_users(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """People who signed up with an invite that needs approval and wait for it."""
    rows = (
        await db.execute(
            select(User, Invite.prefix, Invite.note)
            .outerjoin(Invite, Invite.id == User.invite_id)
            .where(User.approval_pending.is_(True))
            .order_by(User.created_at)
        )
    ).all()
    return [
        PendingUserOut(
            id=u.id,
            username=u.username,
            created_at=u.created_at,
            approval_pending=True,
            invite_id=u.invite_id,
            invite_prefix=prefix,
            invite_note=note,
        )
        for u, prefix, note in rows
    ]


async def _pending(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None or not user.approval_pending:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    return user


@admin.post("/pending/{user_id}/approve", response_model=InviteUserOut)
async def approve_user(user_id: int, admin_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """The person can sign in from now on."""
    user = await _pending(db, user_id)
    user.approval_pending = False
    record(db, admin_, "user.approve", user.username, invite_id=user.invite_id)
    await db.commit()
    return InviteUserOut(id=user.id, username=user.username, created_at=user.created_at, approval_pending=False)


@admin.post("/pending/{user_id}/reject", status_code=204)
async def reject_user(user_id: int, admin_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Deletes the waiting account (it never signed in, so it has nothing else)."""
    user = await _pending(db, user_id)
    record(db, admin_, "user.reject", user.username, invite_id=user.invite_id)
    await db.delete(user)
    await db.commit()


@router.post("/api/invites/check", response_model=InviteCheckOut)
async def check_invite(body: InviteCheckIn, request: Request, db: AsyncSession = Depends(get_db)):
    """For the sign-up page (no sign-in needed): whether an invite link still works, before anyone types."""
    limiter = request.app.state.limiter
    ip = request.client.host if request.client else "unknown"
    limiter.check(ip=ip)
    try:
        invite = await credentials.check_invite(db, body.token)
    except CredentialError as exc:
        if exc.code == "invite_invalid":
            limiter.failure(ip=ip)
        return InviteCheckOut(valid=False, code=exc.code)
    return InviteCheckOut(valid=True, requires_approval=invite.requires_approval, expires_at=invite.expires_at)


router.include_router(admin)
