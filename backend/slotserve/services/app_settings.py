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
from ..runtime.engines import split_args


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
    "api_key_reveal": Editable(bool),
    "devenv_enabled": Editable(bool),
    "devenv_ssh_host": Editable(str, max=255),
    "devenv_ssh_ports": Editable(str, min=1, max=32),
    "devenv_cpus": Editable(float, min=0, max=1024, unit="CPU"),
    "devenv_memory_gb": Editable(float, min=0, max=8192, unit="GB"),
    "devenv_shm_gb": Editable(float, min=0, max=1024, unit="GB"),
    "devenv_custom_images": Editable(bool),
    "devenv_allowed_registries": Editable(str, max=500),
    "devenv_saved_images": Editable(int, min=0, max=100),
    "devenv_pull_minutes": Editable(int, min=0, max=24 * 60, unit="min"),
    "devenv_password_login": Editable(bool),
    "devenv_storage_dir": Editable(str, max=500),
    "devenv_projects": Editable(int, min=0, max=100),
    "devenv_project_days": Editable(int, min=0, max=3650, unit="d"),
    # devenv_cpu: environments without a GPU
    "devenv_cpu_enabled": Editable(bool),
    "devenv_cpu_per_user": Editable(int, min=1, max=100),
    "devenv_cpu_cores_per_gpu": Editable(int, min=0, max=1024, unit="CPU"),
    "devenv_cpu_cores": Editable(float, min=0, max=1024, unit="CPU"),
    "devenv_cpu_memory_gb": Editable(float, min=0, max=8192, unit="GB"),
    "devenv_cpu_memory_pool_gb": Editable(float, min=0, max=65536, unit="GB"),
    # direct ports and the web origin of environments (services/devenv_ports.py, gateway/devenv_web_auth.py)
    "devenv_public_ports_enabled": Editable(bool),
    "devenv_public_ports": Editable(str, min=1, max=32),
    "devenv_public_port_limit": Editable(int, min=0, max=1000),
    "devenv_forwarder_image": Editable(str, min=1, max=255),
    "devenv_web_url": Editable(str, max=300),
    "devenv_web_compat": Editable(bool),
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
    "vllm_cmd": Editable(str, min=1, max=1000),
    "sglang_cmd": Editable(str, min=1, max=1000),
    "custom_engine_cmd": Editable(str, max=2000),
    "custom_engine_health": Editable(str, min=1, max=200),
    "timezone": Editable(str, min=1, max=64),
    "public_url": Editable(str, max=300),
    "telegram_bot_token": Editable(str, max=200, secret=True),
    "telegram_proxy": Editable(str, max=300),
    "chat_default_locale": Editable(str, choices=("en", "zh-CN")),
    "feishu_enabled": Editable(bool),
    "feishu_app_id": Editable(str, max=100),
    "feishu_app_secret": Editable(str, max=200, secret=True),
    "feishu_domain": Editable(str, min=1, max=200),
    "dingtalk_enabled": Editable(bool),
    "dingtalk_client_id": Editable(str, max=100),
    "dingtalk_client_secret": Editable(str, max=200, secret=True),
    "dingtalk_robot_code": Editable(str, max=100),
    "wecom_enabled": Editable(bool),
    "wecom_corp_id": Editable(str, max=100),
    "wecom_agent_id": Editable(str, max=32),
    "wecom_secret": Editable(str, max=200, secret=True),
    "wecom_token": Editable(str, max=100, secret=True),
    "wecom_aes_key": Editable(str, max=64, secret=True),
    "qq_enabled": Editable(bool),
    "qq_app_id": Editable(str, max=100),
    "qq_app_secret": Editable(str, max=200, secret=True),
    "qq_sandbox": Editable(bool),
    "feishu_webhook_url": Editable(str, max=500, secret=True),
    "feishu_webhook_secret": Editable(str, max=200, secret=True),
    "dingtalk_webhook_url": Editable(str, max=500, secret=True),
    "dingtalk_webhook_secret": Editable(str, max=200, secret=True),
    "wecom_webhook_url": Editable(str, max=500, secret=True),
    "group_events": Editable(str, max=200),
    "hf_endpoint": Editable(str, min=1, max=300),
    "hf_token": Editable(str, max=200, secret=True),
    "metrics_token": Editable(str, max=200, secret=True),
    "drain_seconds": Editable(int, min=0, max=600, unit="s"),
    "usage_retention_days": Editable(int, min=0, max=3650, unit="d"),
    "usage_sample_days": Editable(int, min=0, max=3650, unit="d"),
    "usage_busy_percent": Editable(int, min=1, max=100, unit="%"),
    "audit_retention_days": Editable(int, min=0, max=3650, unit="d"),
    "backup_keep": Editable(int, min=0, max=100),
    "backup_target": Editable(str, max=500),
    "backup_hour": Editable(int, min=-1, max=23, unit="h"),
}

# Changing one of these restarts the Telegram bot.
TELEGRAM_KEYS = {"telegram_bot_token", "telegram_proxy"}
# Changing a key that starts with "<channel>_" restarts that chat channel.
CHAT_CHANNELS = ("feishu", "dingtalk", "wecom", "qq")
GROUP_EVENT_KINDS = ("admin_alerts", "approvals", "announcements")


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
    if key in ("default_llama_args", "vllm_cmd", "sglang_cmd", "custom_engine_cmd"):
        try:
            split_args(value)
        except ValueError as exc:
            raise InvalidSetting(key, str(exc)) from exc
    if key == "backup_target" and value:
        from .maintenance import BackupError, parse_target

        try:
            parse_target(value)
        except BackupError as exc:
            raise InvalidSetting(key, "an absolute path or storage:<id>") from exc
    if key in ("devenv_ssh_ports", "devenv_public_ports"):
        from .devenv import parse_port_range

        try:
            parse_port_range(value)
        except ValueError as exc:
            raise InvalidSetting(key, str(exc)) from exc
    if key == "devenv_storage_dir" and value:
        if not value.startswith("/") or ".." in value.split("/"):
            raise InvalidSetting(key, "expected an absolute path on the Docker host")
        value = value.rstrip("/") or "/"
        if value == "/":
            raise InvalidSetting(key, "expected an absolute path on the Docker host")
    if key == "custom_engine_health" and not value.startswith("/"):
        raise InvalidSetting(key, "must start with /")
    if key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidSetting(key, "unknown time zone") from exc
    elif key == "group_events":
        kinds = [k.strip() for k in value.split(",") if k.strip()]
        if unknown := [k for k in kinds if k not in GROUP_EVENT_KINDS]:
            raise InvalidSetting(key, "unknown kind " + ", ".join(unknown))
        value = ",".join(dict.fromkeys(kinds))
    elif key.endswith("_webhook_url") and value:
        if not value.lower().startswith("https://"):
            raise InvalidSetting(key, "expected an https:// URL")
    elif key == "feishu_domain":
        if not value.lower().startswith(("http://", "https://")):
            raise InvalidSetting(key, "expected a URL")
        value = value.rstrip("/")
    elif key == "wecom_aes_key" and value and len(value) != 43:
        raise InvalidSetting(key, "must be 43 characters")
    elif key == "devenv_forwarder_image":
        from .devenv import IMAGE_RE

        if not IMAGE_RE.match(value):
            raise InvalidSetting(key, "expected an image reference")
    elif key in ("public_url", "hf_endpoint", "telegram_proxy", "devenv_web_url") and value:
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
