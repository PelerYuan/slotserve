"""Settings that admins can change from the web UI.

Values from the environment / ``.env`` are the defaults. An override is stored in the
``app_settings`` table and applied onto the shared ``Settings`` object, so every reader
of ``get_settings()`` (and the runtime) sees it immediately.
"""

import json
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..orm import AppSetting
from ..runtime.manager import split_args


@dataclass(frozen=True)
class Editable:
    type: type
    min: int | None = None
    max: int | None = None
    secret: bool = False


EDITABLE: dict[str, Editable] = {
    "registration_open": Editable(bool),
    "invite_code": Editable(str, min=1, max=128, secret=True),
    "max_reservation_hours": Editable(int, min=1, max=24 * 7),
    "max_advance_days": Editable(int, min=1, max=365),
    "quota_weekly_hours": Editable(int, min=0, max=24 * 7),
    "quota_upcoming": Editable(int, min=0, max=100),
    "default_llama_args": Editable(str, max=4000),
    "timezone": Editable(str, min=1, max=64),
    "public_url": Editable(str, max=300),
    "telegram_bot_token": Editable(str, max=200, secret=True),
    "telegram_proxy": Editable(str, max=300),
    "hf_endpoint": Editable(str, min=1, max=300),
    "hf_token": Editable(str, max=200, secret=True),
    "metrics_token": Editable(str, max=200, secret=True),
    "drain_seconds": Editable(int, min=0, max=600),
    "usage_retention_days": Editable(int, min=0, max=3650),
    "audit_retention_days": Editable(int, min=0, max=3650),
    "backup_keep": Editable(int, min=0, max=100),
}

# Changing one of these restarts the Telegram bot.
TELEGRAM_KEYS = {"telegram_bot_token", "telegram_proxy"}


class InvalidSetting(ValueError):
    def __init__(self, key: str, reason: str):
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


def validate(key: str, value: Any) -> Any:
    spec = EDITABLE.get(key)
    if spec is None:
        raise InvalidSetting(key, "unknown setting")
    if spec.type is bool:
        if not isinstance(value, bool):
            raise InvalidSetting(key, "expected true or false")
        return value
    if spec.type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidSetting(key, "expected an integer")
        if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
            raise InvalidSetting(key, f"must be between {spec.min} and {spec.max}")
        if key == "usage_retention_days" and 0 < value < 90:
            raise InvalidSetting(key, "must be 0 (keep forever) or at least 90")
        return value
    if not isinstance(value, str):
        raise InvalidSetting(key, "expected text")
    value = value.strip()
    if (spec.min is not None and len(value) < spec.min) or (spec.max is not None and len(value) > spec.max):
        raise InvalidSetting(key, "invalid length")
    if key == "default_llama_args":
        try:
            split_args(value)
        except ValueError as exc:
            raise InvalidSetting(key, str(exc)) from exc
    elif key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidSetting(key, "unknown time zone") from exc
    elif key in ("public_url", "hf_endpoint", "telegram_proxy") and value:
        if not value.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise InvalidSetting(key, "expected a URL")
        value = value.rstrip("/")
    return value


def snapshot_defaults(settings: Settings) -> dict[str, Any]:
    return {key: getattr(settings, key) for key in EDITABLE}


async def load_overrides(db: AsyncSession, settings: Settings) -> None:
    for row in (await db.execute(select(AppSetting))).scalars():
        if row.key not in EDITABLE:
            continue
        try:
            setattr(settings, row.key, validate(row.key, json.loads(row.value)))
        except (InvalidSetting, ValueError):
            continue  # ignore a stale or corrupt override; the default stays in effect


async def overridden_keys(db: AsyncSession) -> set[str]:
    return set((await db.execute(select(AppSetting.key))).scalars())


async def apply(db: AsyncSession, settings: Settings, defaults: dict[str, Any], changes: dict[str, Any]) -> dict:
    """Validates and stores ``changes`` (``None`` resets a key to its default). Caller commits."""
    cleaned = {key: (None if value is None else validate(key, value)) for key, value in changes.items()}
    applied: dict[str, Any] = {}
    for key, value in cleaned.items():
        row = await db.get(AppSetting, key)
        if value is None:
            if row is not None:
                await db.delete(row)
            value = defaults[key]
        elif row is None:
            db.add(AppSetting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
        setattr(settings, key, value)
        applied[key] = "***" if EDITABLE[key].secret else value
    return applied
