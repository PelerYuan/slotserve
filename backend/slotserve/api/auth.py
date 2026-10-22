import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import SESSION_USER_KEY, current_user
from ..errors import ApiError
from ..orm import User
from ..schemas import LoginIn, PasswordChangeIn, RegisterIn, UserOut
from ..security import constant_time_equals, hash_password, sudo, verify_password
from ..security.setup_code import SetupCode, admin_exists
from ..services.audit import record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
DEFAULT_INVITE_CODE = "change-me"


class RegisterBody(RegisterIn):
    invite_code: str = ""
    # Required (instead of the invite code) while no admin exists; see security/setup_code.py.
    setup_code: str = Field(default="", max_length=64)


class SudoIn(BaseModel):
    password: str


class SetupOut(BaseModel):
    setup_required: bool


class SudoOut(BaseModel):
    active: bool
    expires_at: datetime | None = None
    window_seconds: int = sudo.WINDOW_SECONDS


def invite_code_usable(code: str) -> bool:
    """The default invite code is public; with it only the first admin can sign up."""
    return code.strip() not in ("", DEFAULT_INVITE_CODE)


@router.get("/setup", response_model=SetupOut)
async def setup_state(db: AsyncSession = Depends(get_db)):
    """Whether the first admin account still has to be created (sign-up then asks for the setup code)."""
    return SetupOut(setup_required=not await admin_exists(db))


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: RegisterBody, request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    limiter = request.app.state.limiter
    ip = _client_ip(request)
    setup: SetupCode = request.app.state.setup_code
    if not await admin_exists(db):
        async with setup.lock:
            if not await admin_exists(db):
                return await _register_first_admin(body, request, db, setup, ip)
    if not settings.registration_open:
        raise ApiError(status.HTTP_403_FORBIDDEN, "registration_closed")
    if not invite_code_usable(settings.invite_code):
        raise ApiError(status.HTTP_403_FORBIDDEN, "invite_code_not_set")
    limiter.check(ip=ip)
    if not constant_time_equals(body.invite_code.strip(), settings.invite_code):
        limiter.failure(ip=ip)
        raise ApiError(status.HTTP_403_FORBIDDEN, "invalid_invite_code")
    await _check_username(db, body.username)
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=False)
    db.add(user)
    await db.commit()
    request.session[SESSION_USER_KEY] = user.id
    request.app.state.notifier.emit("user_registered", username=user.username)
    return user


async def _register_first_admin(body: RegisterBody, request: Request, db: AsyncSession, setup: SetupCode, ip: str):
    limiter = request.app.state.limiter
    limiter.check(ip=ip)
    if not body.setup_code.strip():
        raise ApiError(status.HTTP_403_FORBIDDEN, "setup_code_required")
    if not setup.verify(body.setup_code):
        limiter.failure(ip=ip)
        raise ApiError(status.HTTP_403_FORBIDDEN, "invalid_setup_code")
    await _check_username(db, body.username)
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=True)
    db.add(user)
    await db.flush()
    record(db, user, "auth.first_admin", user.username)
    await db.commit()
    setup.consume()
    log.info("first admin account %r created; the setup code is no longer valid", user.username)
    request.session[SESSION_USER_KEY] = user.id
    sudo.grant(request, user.id)
    return user


async def _check_username(db: AsyncSession, username: str) -> None:
    taken = await db.execute(select(User.id).where(func.lower(User.username) == username.lower()))
    if taken.first() is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "username_taken")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    limiter = request.app.state.limiter
    username, ip = body.username.strip(), _client_ip(request)
    limiter.check(username=username, ip=ip)
    user = (await db.execute(select(User).where(func.lower(User.username) == username.lower()))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        limiter.failure(username=username, ip=ip)
        record(db, user, "auth.login_failed", username[:64], ip=ip)
        await db.commit()
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
    limiter.success(username=username)
    if user.disabled_at is not None:
        raise ApiError(status.HTTP_403_FORBIDDEN, "account_disabled")
    request.session[SESSION_USER_KEY] = user.id
    sudo.grant(request, user.id)  # the password was just entered
    record(db, user, "auth.login", user.username, ip=ip)
    await db.commit()
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


@router.get("/sudo", response_model=SudoOut)
async def sudo_state(request: Request, user: User = Depends(current_user)):
    """Whether sudo mode is active (password entered within the last few minutes)."""
    until = sudo.expires_at(request, user.id)
    return SudoOut(active=until is not None, expires_at=_utc(until))


@router.post("/sudo", response_model=SudoOut)
async def sudo_enter(body: SudoIn, request: Request, user: User = Depends(current_user)):
    """Enter sudo mode by giving the password again; needed by dangerous actions (``Depends(sudo_user)``)."""
    limiter = request.app.state.limiter
    ip = _client_ip(request)
    limiter.check(username=user.username, ip=ip)
    if not verify_password(body.password, user.password_hash):
        limiter.failure(username=user.username, ip=ip)
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
    limiter.success(username=user.username)
    return SudoOut(active=True, expires_at=_utc(sudo.grant(request, user.id)))


def _utc(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=UTC)
