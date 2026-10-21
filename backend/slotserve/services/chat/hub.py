"""All chat channels of one app: who to message where, restarts after settings changes, health."""

from collections.abc import Iterable

from sqlalchemy import select

from ...orm import ChatLink, User
from ..app_settings import CHAT_CHANNELS, TELEGRAM_KEYS
from ..notify import channels_off
from ..telegram import TelegramBot
from .channel import Channel
from .dingtalk import DingTalkChannel
from .feishu import FeishuChannel
from .qq import QQChannel
from .webhooks import GroupWebhooks
from .wecom import WeComChannel


class ChannelHub:
    def __init__(self, app, transports: dict | None = None):
        transports = transports or {}
        self.telegram = TelegramBot(app, transport=transports.get("telegram"))
        self.channels: dict[str, Channel] = {"telegram": self.telegram}
        for cls in (FeishuChannel, DingTalkChannel, WeComChannel, QQChannel):
            self.channels[cls.id] = cls(app, transport=transports.get(cls.id))
        self.webhooks = GroupWebhooks(app.state.settings, transport=transports.get("webhooks"))

    def get(self, channel_id: str) -> Channel | None:
        return self.channels.get(channel_id)

    def __iter__(self):
        return iter(self.channels.values())

    def reload_for(self, keys: Iterable[str]) -> None:
        keys = set(keys)
        if keys & TELEGRAM_KEYS:
            self.telegram.reload()
        for name in CHAT_CHANNELS:
            if any(k.startswith(f"{name}_") and "_webhook_" not in k for k in keys):
                self.channels[name].reload()

    async def addresses(self, db, user: User) -> list[tuple[Channel, object]]:
        """Where to send a person's notifications: enabled channels they linked and did not switch off."""
        off = channels_off(user)
        links = {
            link.channel: link
            for link in (
                await db.execute(select(ChatLink).where(ChatLink.user_id == user.id, ChatLink.address.is_not(None)))
            ).scalars()
        }
        out = []
        for channel in self.channels.values():
            if not channel.enabled or channel.id in off:
                continue
            address = channel.address_of(user, links.get(channel.id))
            if address is not None:
                out.append((channel, address))
        return out

    async def close(self) -> None:
        for channel in self.channels.values():
            await channel.close()
        await self.webhooks.close()
