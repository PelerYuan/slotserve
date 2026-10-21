"""Chat channels: linking accounts, per-channel switches, the WeCom callback URL and the admin overview."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import admin_user, current_user
from ..errors import ApiError
from ..orm import ChatLink, User
from ..services.app_settings import GROUP_EVENT_KINDS
from ..services.audit import record
from ..services.chat.channel import Channel
from ..services.chat.hub import ChannelHub
from ..services.chat.webhooks import TARGETS
from ..services.chat.wecom import WeComCryptoError
from ..services.notify import channels_off, lang, prefs

router = APIRouter(prefix="/api", tags=["channels"])

TEST_MESSAGE = {"en": "✅ SlotServe → {channel} OK", "zh-CN": "✅ SlotServe 通知测试成功（{channel}）"}
WECOM_CALLBACK = "/api/channels/wecom/callback"


class ChannelOut(BaseModel):
    id: str
    label: str
    linked: bool
    account: str | None
    bot_name: str | None
    notify: bool
    link_kind: str  # url: open a link (Telegram) | code: send a bind code to the bot
    receives: bool  # whether the bot can answer commands (WeCom needs its callback set up)


class ChannelsOut(BaseModel):
    channels: list[ChannelOut]
    notify_prefs: dict[str, bool]


class ChannelLinkOut(BaseModel):
    channel: ChannelOut
    code: str | None = None
    expires_at: datetime | None = None
    link_url: str | None = None


class ChannelUpdateIn(BaseModel):
    notify: bool


class AnnounceIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _hub(request: Request) -> ChannelHub:
    return request.app.state.channels


def _channel(request: Request, channel_id: str) -> Channel:
    channel = _hub(request).get(channel_id)
    if channel is None or not channel.enabled:
        raise ApiError(status.HTTP_409_CONFLICT, "channel_unavailable")
    return channel


async def _out(db, channel: Channel, user: User) -> ChannelOut:
    link = await channel.link_of(db, user)
    address = channel.address_of(user, link if link is not None and link.address else None)
    account = user.telegram_username if channel.id == "telegram" else (link.display_name if link else None)
    return ChannelOut(
        id=channel.id,
        label=channel.label,
        linked=address is not None,
        account=account if address is not None else None,
        bot_name=channel.bot_name,
        notify=channel.id not in channels_off(user),
        link_kind="url" if channel.id == "telegram" else "code",
        receives=getattr(channel, "receives", True),
    )


@router.get("/channels", response_model=ChannelsOut)
async def list_channels(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The chat channels admins turned on, with this person's link and switch for each."""
    out = [await _out(db, c, user) for c in _hub(request) if c.enabled]
    return ChannelsOut(channels=out, notify_prefs=prefs(user))


@router.post("/channels/{channel_id}/link", response_model=ChannelLinkOut)
async def start_link(
    channel_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Telegram: a one-time deep link. Other platforms: a code to send to the bot (valid 30 minutes)."""
    channel = _channel(request, channel_id)
    if channel.id == "telegram" and not channel.bot_name:
        raise ApiError(status.HTTP_409_CONFLICT, "telegram_unavailable")
    started = await channel.start_link(db, user)
    await db.commit()
    return ChannelLinkOut(channel=await _out(db, channel, user), **started)


@router.delete("/channels/{channel_id}/link", response_model=ChannelOut)
async def unlink(
    channel_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    channel = _hub(request).get(channel_id)
    if channel is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    await channel.unlink(db, user)
    await db.commit()
    return await _out(db, channel, user)


@router.put("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: str,
    body: ChannelUpdateIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Switches notifications on this channel on or off; which notifications is shared by all channels."""
    channel = _hub(request).get(channel_id)
    if channel is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    off = channels_off(user)
    if body.notify:
        off.discard(channel.id)
    else:
        off.add(channel.id)
    user.channels_off = json.dumps(sorted(off)) if off else None
    await db.commit()
    return await _out(db, channel, user)


@router.post("/channels/{channel_id}/test", status_code=204)
async def test_message(
    channel_id: str, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    channel = _channel(request, channel_id)
    address = channel.address_of(user, await channel.link_of(db, user))
    if address is None:
        raise ApiError(status.HTTP_409_CONFLICT, "channel_not_linked")
    if not await channel.send(address, TEST_MESSAGE[lang(user)].format(channel=channel.label)):
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "channel_send_failed", channel=channel.label)


# ---- WeCom callback URL (signed and encrypted; no session) ------------------------------


@router.get(WECOM_CALLBACK.removeprefix("/api"), include_in_schema=False)
async def wecom_verify(
    request: Request,
    msg_signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    echostr: str = Query(""),
):
    try:
        return PlainTextResponse(_hub(request).get("wecom").verify_url(msg_signature, timestamp, nonce, echostr))
    except WeComCryptoError as exc:
        return PlainTextResponse(str(exc), status_code=403)


@router.post(WECOM_CALLBACK.removeprefix("/api"), include_in_schema=False)
async def wecom_receive(
    request: Request, msg_signature: str = Query(""), timestamp: str = Query(""), nonce: str = Query("")
):
    channel = _hub(request).get("wecom")
    try:
        message = channel.receive(msg_signature, timestamp, nonce, await request.body())
    except WeComCryptoError as exc:
        return PlainTextResponse(str(exc), status_code=403)
    channel.accept(message)
    return PlainTextResponse("")  # the answer is sent with the API, so nothing has to be encrypted here


# ---- admin -----------------------------------------------------------------------------------


@router.get("/admin/channels")
async def admin_channels(request: Request, _: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Health of every chat channel and group webhook, and how many people linked each channel."""
    hub = _hub(request)
    counts = dict(
        (
            await db.execute(
                select(ChatLink.channel, func.count()).where(ChatLink.address.is_not(None)).group_by(ChatLink.channel)
            )
        ).all()
    )
    counts["telegram"] = (
        await db.execute(select(func.count()).select_from(User).where(User.telegram_chat_id.is_not(None)))
    ).scalar_one()
    settings = request.app.state.settings
    base = (settings.public_url or str(request.base_url)).rstrip("/")
    channels = []
    for channel in hub:
        item = channel.status()
        item["linked_count"] = counts.get(channel.id, 0)
        if channel.id == "wecom":
            item["callback_url"] = base + WECOM_CALLBACK
        channels.append(item)
    selected = [k for k in (settings.group_events or "").split(",") if k]
    return {
        "channels": channels,
        "webhooks": hub.webhooks.status(),
        "group_events": selected,
        "group_event_kinds": list(GROUP_EVENT_KINDS),
    }


@router.post("/admin/channels/webhooks/{target}/test")
async def test_webhook(target: str, request: Request, _: User = Depends(admin_user)):
    hub = _hub(request)
    if target not in TARGETS or target not in hub.webhooks.configured_ids():
        raise ApiError(status.HTTP_404_NOT_FOUND, "not_found")
    locale = request.app.state.settings.chat_default_locale
    return {"error": await hub.webhooks.test(target, TEST_MESSAGE[locale].format(channel=TARGETS[target][0]))}


@router.post("/admin/channels/announce", status_code=202)
async def announce(
    body: AnnounceIn, request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
):
    """Posts an announcement to the group webhooks (when announcements are among the group events)."""
    settings = request.app.state.settings
    if "announcements" not in (settings.group_events or "").split(","):
        raise ApiError(status.HTTP_409_CONFLICT, "announcements_off")
    targets = _hub(request).webhooks.configured_ids()
    if not targets:
        raise ApiError(status.HTTP_409_CONFLICT, "no_group_webhooks")
    request.app.state.notifier.emit("announcement", text=body.text.strip())
    record(db, admin, "channels.announce", body.text.strip()[:80])
    await db.commit()
    return {"targets": targets}
