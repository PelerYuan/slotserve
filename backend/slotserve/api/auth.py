from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import SESSION_USER_KEY, current_user
from ..errors import ApiError
from ..orm import User
from ..schemas import LoginIn, PasswordChangeIn, RegisterIn, UserOut
from ..security import constant_time_equals, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    if not settings.registration_open:
        raise ApiError(status.HTTP_403_FORBIDDEN, "registration_closed")
    limiter = request.app.state.limiter
    ip = _client_ip(request)
    limiter.check(ip=ip)
    if not constant_time_equals(body.invite_code.strip(), settings.invite_code):
        limiter.failure(ip=ip)
        raise ApiError(status.HTTP_403_FORBIDDEN, "invalid_invite_code")
    taken = await db.execute(select(User.id).where(func.lower(User.username) == body.username.lower()))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "username_taken")
    first_user = (await db.execute(select(func.count(User.id)))).scalar_one() == 0
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=first_user)
    db.add(user)
    await db.commit()
    request.session[SESSION_USER_KEY] = user.id
    if not first_user:
        request.app.state.notifier.emit("user_registered", username=user.username)
    return user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    limiter = request.app.state.limiter
    username, ip = body.username.strip(), _client_ip(request)
    limiter.check(username=username, ip=ip)
    user = (
        await db.execute(select(User).where(func.lower(User.username) == username.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        limiter.failure(username=username, ip=ip)
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
    limiter.success(username=username)
    if user.disabled_at is not None:
        raise ApiError(status.HTTP_403_FORBIDDEN, "account_disabled")
    request.session[SESSION_USER_KEY] = user.id
    return user


@router.post("/logout", status_code=204)
async def logout(request: Request):
    request.session.clear()


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


@router.put("/password", status_code=204)
async def change_password(
    body: PasswordChangeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    if not verify_password(body.old_password, user.password_hash):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_credentials")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
