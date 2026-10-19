from fastapi import Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .errors import ApiError
from .orm import User

SESSION_USER_KEY = "uid"


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    uid = request.session.get(SESSION_USER_KEY)
    if uid is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    user = await db.get(User, uid)
    if user is None:
        request.session.clear()
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise ApiError(status.HTTP_403_FORBIDDEN, "admin_required")
    return user


def get_runtime(request: Request):
    return request.app.state.runtime


def get_scheduler(request: Request):
    return request.app.state.scheduler


def get_downloader(request: Request):
    return request.app.state.downloader
