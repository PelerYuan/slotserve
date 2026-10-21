"""Turns events into per-person messages and sends them through the Telegram bot."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..config import Settings
from ..db import SessionLocal, utcnow
from ..orm import DevTemplate, Lane, LlmModel, ModelPreset, OutboxItem, Reservation, User

log = logging.getLogger(__name__)

# Preference key -> default. Every event maps to one key below.
PREF_DEFAULTS: dict[str, bool] = {
    "reminders": True,
    "model_ready": True,
    "booking_changes": True,
    "waitlist": True,
    "downloads": True,
    "admin_alerts": True,
}

EVENT_PREF = {
    "reminder_start": "reminders",
    "reminder_end": "reminders",
    "model_ready": "model_ready",
    "exclusive_ready": "model_ready",
    "devenv_ready": "model_ready",
    "devenv_failed": "booking_changes",
    "booking_created": "booking_changes",
    "booking_changed": "booking_changes",
    "booking_cancelled": "booking_changes",
    "booking_released": "booking_changes",
    "booking_approved": "booking_changes",
    "booking_rejected": "booking_changes",
    "booking_expired": "booking_changes",
    "approval_requested": "admin_alerts",
    "fallback": "booking_changes",
    "waitlist_free": "waitlist",
    "waitlist_booked": "waitlist",
    "download_done": "downloads",
    "download_failed": "downloads",
    "load_failed": "admin_alerts",
    "fallback_admin": "admin_alerts",
    "user_registered": "admin_alerts",
    "disk_low": "admin_alerts",
}

ADMIN_EVENTS = {"load_failed", "user_registered", "disk_low"}
RETRY_SECONDS = (30, 120, 600, 1800, 7200)  # then the message is dropped

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "reminder_start": "⏰ Your booking starts at {start}: {what}.",
        "reminder_end": "⌛ Your booking ends at {end}. Need more time?",
        "model_ready": "✅ {what} is loaded and ready for your booking.",
        "exclusive_ready": "✅ The GPU is yours until {end}. llama-server is stopped.",
        "devenv_ready": "🖥 Your dev environment is ready until {end}:\nssh -p {port} root@{host}",
        "devenv_failed": "⚠️ Your dev environment ({what}) could not start: {error}",
        "devenv": "Dev environment",
        "booking_created": "📅 {by} booked {when} for you: {what}.",
        "booking_changed": "✏️ {by} changed your booking: {when}, {what}.",
        "booking_cancelled": "❌ {by} cancelled your booking {when}.",
        "booking_released": "⏹ {by} ended your booking early.",
        "fallback": "⚠️ {model} failed to load, so {default} is serving your booking for now. "
        "SlotServe will retry automatically.",
        "fallback_admin": "⚠️ {model} failed to load for {user}'s booking; falling back to {default}.",
        "waitlist_free": "🔔 The time you waited for is free: {when}. Book it in SlotServe.",
        "waitlist_booked": "🎉 Booked from the waitlist: {when}, {what}.",
        "download_done": "📦 Download finished: {filename}.",
        "download_failed": "📦 Download failed: {filename} ({error}).",
        "load_failed": "🔥 {model} failed to load: {error}",
        "user_registered": "👤 New account: {username}.",
        "disk_low": "💾 Low disk space in the models folder: {free} GB free.",
        "exclusive": "whole GPU (no model)",
        "extend30": "+30 min",
        "extend60": "+1 hour",
        "release": "End now",
        "approval_requested": "📝 {user} asks for {when}: {what}{series}.",
        "series_suffix": " (weekly, {count} bookings)",
        "booking_approved": "✅ Your booking {when} was approved.",
        "booking_rejected": "🚫 Your booking {when} was not approved.{reason}",
        "booking_expired": "⌛ Your booking request for {when} expired before an admin reviewed it.",
        "reason": " Reason: {note}",
        "approve": "Approve",
        "reject": "Reject",
    },
    "zh-CN": {
        "reminder_start": "⏰ 你的预约将在 {start} 开始：{what}。",
        "reminder_end": "⌛ 你的预约将在 {end} 结束，需要延长吗？",
        "model_ready": "✅ {what} 已加载完成，可以开始使用。",
        "exclusive_ready": "✅ 整张 GPU 归你使用到 {end}，llama-server 已停止。",
        "devenv_ready": "🖥 开发环境已就绪，可用到 {end}：\nssh -p {port} root@{host}",
        "devenv_failed": "⚠️ 你的开发环境（{what}）没能启动：{error}",
        "devenv": "开发环境",
        "booking_created": "📅 {by} 替你预约了 {when}：{what}。",
        "booking_changed": "✏️ {by} 修改了你的预约：{when}，{what}。",
        "booking_cancelled": "❌ {by} 取消了你的预约 {when}。",
        "booking_released": "⏹ {by} 提前结束了你的预约。",
        "fallback": "⚠️ {model} 加载失败，暂时由 {default} 为你的预约提供服务，系统会自动重试。",
        "fallback_admin": "⚠️ {user} 预约的 {model} 加载失败，已回退到 {default}。",
        "waitlist_free": "🔔 你候补的时段空出来了：{when}。请到 SlotServe 预约。",
        "waitlist_booked": "🎉 候补成功，已为你预约：{when}，{what}。",
        "download_done": "📦 下载完成：{filename}。",
        "download_failed": "📦 下载失败：{filename}（{error}）。",
        "load_failed": "🔥 {model} 加载失败：{error}",
        "user_registered": "👤 新用户注册：{username}。",
        "disk_low": "💾 模型目录磁盘空间不足：剩余 {free} GB。",
        "exclusive": "整卡（不加载模型）",
        "extend30": "延长 30 分钟",
        "extend60": "延长 1 小时",
        "release": "立即结束",
        "approval_requested": "📝 {user} 申请预约 {when}：{what}{series}。",
        "series_suffix": "（每周重复，共 {count} 次）",
        "booking_approved": "✅ 你的预约 {when} 已通过审批。",
        "booking_rejected": "🚫 你的预约 {when} 未通过审批。{reason}",
        "booking_expired": "⌛ 你申请的预约 {when} 在审批前已到开始时间，已自动失效。",
        "reason": "理由：{note}",
        "approve": "通过",
        "reject": "拒绝",
    },
}


def lang(user: User | None) -> str:
    return "zh-CN" if user is not None and (user.locale or "").startswith("zh") else "en"


def text(recipient: User | None, key: str, /, **params: Any) -> str:
    template = MESSAGES[lang(recipient)].get(key) or MESSAGES["en"][key]
    return template.format(**params)


def prefs(user: User) -> dict[str, bool]:
    stored: dict = {}
    if user.notify_prefs:
        try:
            stored = json.loads(user.notify_prefs)
        except ValueError:
            stored = {}
    return {k: bool(stored.get(k, v)) for k, v in PREF_DEFAULTS.items()}


class Notifier:
    """Queue of events; ``emit`` never blocks, a worker formats and delivers them."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=1000)
        self.bot = None  # set by the app when the Telegram bot exists
        self.sent: list[tuple[int, str]] = []  # (chat_id, text) - kept for tests and debugging

    def emit(self, event: str, **data: Any) -> None:
        try:
            self.queue.put_nowait((event, data))
        except asyncio.QueueFull:
            log.warning("notification queue full, dropping %s", event)

    async def run(self) -> None:
        while True:
            event, data = await self.queue.get()
            try:
                await self.dispatch(event, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("failed to deliver %s", event)

    # ---- formatting ---------------------------------------------------------

    def fmt_time(self, dt: datetime) -> str:
        try:
            tz = ZoneInfo(self.settings.timezone)
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("UTC")
        return dt.astimezone(tz).strftime("%m-%d %H:%M")

    def fmt_window(self, r: Reservation) -> str:
        end = self.fmt_time(r.end_at)
        start = self.fmt_time(r.start_at)
        return f"{start}–{end[-5:]}" if start[:5] == end[:5] else f"{start} – {end}"

    def ssh_host(self) -> str:
        """Host name for ssh commands: the dev environment setting, else the public URL's host."""
        if self.settings.devenv_ssh_host:
            return self.settings.devenv_ssh_host
        from urllib.parse import urlsplit

        return urlsplit(self.settings.public_url).hostname or "<server>" if self.settings.public_url else "<server>"

    async def describe(self, db, r: Reservation, user: User | None) -> str:
        if r.devenv_template_id is not None:
            template = await db.get(DevTemplate, r.devenv_template_id)
            name = f"{text(user, 'devenv')} · {template.name if template else '?'}"
        elif r.model_id is None:
            name = text(user, "exclusive")
        else:
            model = await db.get(LlmModel, r.model_id)
            name = model.name if model else "?"
            if r.preset_id is not None and (preset := await db.get(ModelPreset, r.preset_id)) is not None:
                name = f"{name} ({preset.name})"
        lanes = (await db.execute(select(Lane.id, Lane.name))).all()
        if len(lanes) > 1 and r.lane_id is not None:
            name = f"{dict(lanes).get(r.lane_id, '?')} · {name}"
        return name

    # ---- delivery -----------------------------------------------------------

    async def dispatch(self, event: str, data: dict) -> None:
        async with SessionLocal() as db:
            if event in ADMIN_EVENTS:
                admins = (
                    await db.execute(select(User).where(User.is_admin.is_(True), User.disabled_at.is_(None)))
                ).scalars()
                for admin in admins:
                    await self._deliver(admin, event, text(admin, event, **_clean(data)))
                return
            if event == "fallback":
                await self._fallback(db, data)
                return
            if event == "approval_requested":
                await self._approval_requested(db, data)
                return
            user = await db.get(User, data["user_id"]) if data.get("user_id") else None
            if user is None:
                return
            params = dict(_clean(data))
            buttons = None
            if (rid := data.get("reservation_id")) is not None:
                r = await db.get(Reservation, rid)
                if r is None:
                    return
                params.update(
                    start=self.fmt_time(r.start_at),
                    end=self.fmt_time(r.end_at),
                    when=self.fmt_window(r),
                    what=await self.describe(db, r, user),
                )
                if event == "booking_rejected" and r.review_note:
                    params["reason"] = text(user, "reason", note=r.review_note)
                params.setdefault("reason", "")
                if event == "devenv_ready":
                    params["host"] = self.ssh_host()
                if event == "reminder_end":
                    buttons = [
                        [
                            (text(user, "extend30"), f"ext:{r.id}:30"),
                            (text(user, "extend60"), f"ext:{r.id}:60"),
                            (text(user, "release"), f"rel:{r.id}"),
                        ]
                    ]
            await self._deliver(user, event, text(user, event, **params), buttons)

    async def _approval_requested(self, db, data: dict) -> None:
        r = await db.get(Reservation, data["reservation_id"])
        owner = await db.get(User, data["owner_id"])
        if r is None or r.status != "pending":
            return
        admins = (await db.execute(select(User).where(User.is_admin.is_(True), User.disabled_at.is_(None)))).scalars()
        for admin in admins:
            count = int(data.get("count") or 1)
            message = text(
                admin,
                "approval_requested",
                user=owner.username if owner else "?",
                when=self.fmt_window(r),
                what=await self.describe(db, r, admin),
                series=text(admin, "series_suffix", count=count) if count > 1 else "",
            )
            buttons = [[(text(admin, "approve"), f"apr:{r.id}"), (text(admin, "reject"), f"rej:{r.id}")]]
            await self._deliver(admin, "approval_requested", message, buttons)

    async def _fallback(self, db, data: dict) -> None:
        owner = await db.get(User, data["user_id"])
        params = {"model": data.get("model"), "default": data.get("default"), "user": owner.username if owner else "?"}
        if owner is not None:
            await self._deliver(owner, "fallback", text(owner, "fallback", **params))
        admins = (await db.execute(select(User).where(User.is_admin.is_(True), User.disabled_at.is_(None)))).scalars()
        for admin in admins:
            if owner is None or admin.id != owner.id:
                await self._deliver(admin, "fallback_admin", text(admin, "fallback_admin", **params))

    async def _deliver(self, user: User, event: str, message: str, buttons=None) -> None:
        if user.telegram_chat_id is None or user.disabled_at is not None:
            return
        if not prefs(user).get(EVENT_PREF.get(event, "admin_alerts"), True):
            return
        self.sent.append((user.telegram_chat_id, message))
        del self.sent[:-200]
        if self.bot is not None and not await self.bot.send(user.telegram_chat_id, message, buttons):
            await self._queue_retry(user.telegram_chat_id, message, buttons)

    # ---- outbox: retries and restarts ----------------------------------------

    async def _queue_retry(self, chat_id: int, message: str, buttons, attempts: int = 1) -> None:
        async with SessionLocal() as db:
            db.add(
                OutboxItem(
                    kind="message",
                    payload=json.dumps({"chat_id": chat_id, "text": message, "buttons": buttons}),
                    attempts=attempts,
                    next_attempt_at=utcnow() + timedelta(seconds=RETRY_SECONDS[0]),
                )
            )
            await db.commit()

    async def retry_due(self, db) -> None:
        """Re-sends messages whose earlier delivery failed (called by the housekeeper)."""
        if self.bot is None or not self.bot.enabled:
            return
        due = (
            (
                await db.execute(
                    select(OutboxItem)
                    .where(OutboxItem.kind == "message", OutboxItem.next_attempt_at <= utcnow())
                    .order_by(OutboxItem.id)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        for item in due:
            data = json.loads(item.payload)
            buttons = [[tuple(b) for b in row] for row in data["buttons"]] if data.get("buttons") else None
            if await self.bot.send(data["chat_id"], data["text"], buttons):
                await db.delete(item)
            elif item.attempts >= len(RETRY_SECONDS):
                log.warning("dropping notification after %s attempts", item.attempts)
                await db.delete(item)
            else:
                item.next_attempt_at = utcnow() + timedelta(seconds=RETRY_SECONDS[item.attempts])
                item.attempts += 1
        await db.commit()

    async def save_pending(self) -> int:
        """At shutdown: keeps events that were not delivered yet."""
        items = []
        while not self.queue.empty():
            event, data = self.queue.get_nowait()
            items.append(OutboxItem(kind="event", payload=json.dumps({"event": event, "data": data}, default=str)))
        if items:
            async with SessionLocal() as db:
                db.add_all(items)
                await db.commit()
        return len(items)

    async def restore_pending(self) -> int:
        """At startup: puts events saved by the previous process back on the queue."""
        async with SessionLocal() as db:
            rows = (await db.execute(select(OutboxItem).where(OutboxItem.kind == "event"))).scalars().all()
            for row in rows:
                payload = json.loads(row.payload)
                self.emit(payload["event"], **payload["data"])
                await db.delete(row)
            await db.commit()
        return len(rows)


def _clean(data: dict) -> dict:
    return {k: ("" if v is None else v) for k, v in data.items()}
