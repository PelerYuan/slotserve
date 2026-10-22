from fastapi import Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .errors import ApiError
from .orm import User
from .security import sudo
from .security.sessions import UID_KEY

SESSION_USER_KEY = UID_KEY


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get(SESSION_USER_KEY)
    if uid is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    user = await db.get(User, uid)
    if user is None or user.disabled_at is not None or user.approval_pending:
        request.session.clear()
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    return user


async def sudo_user(request: Request, user: User = Depends(current_user)) -> User:
    """The signed-in user, who entered their password within the last few minutes (``security/sudo.py``).
    Otherwise 403 ``sudo_required``: the frontend asks for the password (``POST /api/auth/sudo``) and retries."""
    if not sudo.is_active(request, user.id):
        raise ApiError(status.HTTP_403_FORBIDDEN, "sudo_required", seconds=sudo.WINDOW_SECONDS)
    return user


async def sudo_admin(request: Request, user: User = Depends(admin_user)) -> User:
    """An admin in sudo mode (see ``sudo_user``)."""
    return await sudo_user(request, user)


def get_lanes(request: Request):
    return request.app.state.lanes


def get_downloader(request: Request):
    return request.app.state.downloader
