import json

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..errors import ApiError
from ..orm import User
from ..schemas import PreferencesIn, TelegramOut, UserOut
from ..services.notify import PREF_DEFAULTS, prefs
from ..services.telegram import new_link_token

router = APIRouter(prefix="/api", tags=["notifications"])


@router.put("/auth/preferences", response_model=UserOut)
async def set_preferences(body: PreferencesIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if body.locale is not None:
        user.locale = body.locale
    if body.notify_prefs is not None:
        merged = prefs(user)
        merged.update({k: bool(v) for k, v in body.notify_prefs.items() if k in PREF_DEFAULTS})
        user.notify_prefs = json.dumps(merged)
    await db.commit()
    return user


def _telegram_out(request: Request, user: User, link_url: str | None = None) -> TelegramOut:
    bot = request.app.state.telegram
    return TelegramOut(
        enabled=bot.enabled,
        bot_username=bot.username,
        linked=user.telegram_chat_id is not None,
        telegram_username=user.telegram_username,
        link_url=link_url,
        notify_prefs=prefs(user),
    )


@router.get("/telegram", response_model=TelegramOut)
async def telegram_status(request: Request, user: User = Depends(current_user)):
    return _telegram_out(request, user)


@router.post("/telegram/link", response_model=TelegramOut)
async def telegram_link(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Returns a one-time ``t.me`` deep link; pressing Start in Telegram completes the link."""
    bot = request.app.state.telegram
    if not bot.enabled or not bot.username:
        raise ApiError(status.HTTP_409_CONFLICT, "telegram_unavailable")
    user.telegram_link_token = new_link_token()
    await db.commit()
    return _telegram_out(request, user, f"https://t.me/{bot.username}?start={user.telegram_link_token}")


@router.delete("/telegram/link", response_model=TelegramOut)
async def telegram_unlink(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.telegram_chat_id = None
    user.telegram_username = None
    user.telegram_link_token = None
    await db.commit()
    return _telegram_out(request, user)


@router.post("/telegram/test", status_code=204)
async def telegram_test(request: Request, user: User = Depends(current_user)):
    if user.telegram_chat_id is None:
        raise ApiError(status.HTTP_409_CONFLICT, "telegram_not_linked")
    message = "✅ SlotServe → Telegram OK" if not (user.locale or "").startswith("zh") else "✅ SlotServe 通知测试成功"
    await request.app.state.telegram.send(user.telegram_chat_id, message)
