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
    min: float | None = None
    max: float | None = None
    secret: bool = False
    unit: str | None = None  # s | min | h | d | GB | MB/s - shown next to the input
    choices: tuple | None = None

    @property
    def kind(self) -> str:
        return {bool: "bool", int: "int", float: "float"}.get(self.type, "str")


EDITABLE: dict[str, Editable] = {
    "registration_open": Editable(bool),
    "invite_code": Editable(str, min=1, max=128, secret=True),
    "max_reservation_hours": Editable(int, min=1, max=24 * 7, unit="h"),
    "max_advance_days": Editable(int, min=1, max=365, unit="d"),
    "slot_minutes": Editable(int, unit="min", choices=(5, 10, 15, 20, 30, 60)),
    "reminder_minutes": Editable(int, min=1, max=120, unit="min"),
    "quota_weekly_hours": Editable(int, min=0, max=24 * 7, unit="h"),
    "quota_upcoming": Editable(int, min=0, max=100),
    "approval_mode": Editable(str, choices=("off", "long", "all")),
    "approval_hours": Editable(int, min=1, max=24 * 7, unit="h"),
    "preload_min_minutes": Editable(int, min=0, max=120, unit="min"),
    "preload_buffer_seconds": Editable(int, min=0, max=3600, unit="s"),
    "load_timeout_seconds": Editable(int, min=30, max=7200, unit="s"),
    "stop_timeout_seconds": Editable(int, min=1, max=300, unit="s"),
    "disk_read_mbps": Editable(float, min=1, max=100_000, unit="MB/s"),
    "scheduler_interval_seconds": Editable(float, min=1, max=60, unit="s"),
    "download_min_free_gb": Editable(float, min=0, max=10_000, unit="GB"),
    "default_llama_args": Editable(str, max=4000),
    "timezone": Editable(str, min=1, max=64),
    "public_url": Editable(str, max=300),
    "telegram_bot_token": Editable(str, max=200, secret=True),
    "telegram_proxy": Editable(str, max=300),
    "hf_endpoint": Editable(str, min=1, max=300),
    "hf_token": Editable(str, max=200, secret=True),
    "metrics_token": Editable(str, max=200, secret=True),
    "drain_seconds": Editable(int, min=0, max=600, unit="s"),
    "usage_retention_days": Editable(int, min=0, max=3650, unit="d"),
    "audit_retention_days": Editable(int, min=0, max=3650, unit="d"),
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
    if spec.type in (int, float):
        numeric = (int, float) if spec.type is float else (int,)
        if isinstance(value, bool) or not isinstance(value, numeric):
            raise InvalidSetting(key, "expected a number" if spec.type is float else "expected an integer")
        value = spec.type(value)
        if spec.choices is not None and value not in spec.choices:
            raise InvalidSetting(key, "must be one of " + ", ".join(str(c) for c in spec.choices))
        if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
            raise InvalidSetting(key, f"must be between {_fmt(spec.min)} and {_fmt(spec.max)}")
        if key == "usage_retention_days" and 0 < value < 90:
            raise InvalidSetting(key, "must be 0 (keep forever) or at least 90")
        return value
    if not isinstance(value, str):
        raise InvalidSetting(key, "expected text")
    value = value.strip()
    if spec.choices is not None and value not in spec.choices:
        raise InvalidSetting(key, "must be one of " + ", ".join(spec.choices))
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


def _fmt(n: float | None) -> str:
    return "" if n is None else (str(int(n)) if float(n).is_integer() else str(n))


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
