from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, PrimaryKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notify_prefs: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {kind: bool}
    ical_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Encrypted with SECRET_KEY, only while admins allow viewing keys again (setting api_key_reveal).
    key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LlmModel(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    file_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # downloading | ready | failed
    status: Mapped[str] = mapped_column(String(16), default="ready")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    args: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # llama.cpp (NULL) | vllm | sglang | custom; file_path is then a directory or repository id
    engine: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ModelPreset(Base):
    """An alternative set of launch arguments for a model."""

    __tablename__ = "model_presets"
    __table_args__ = (UniqueConstraint("model_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    args: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(255))
    # queued | running | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speed_bps: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CatalogEntryRow(Base):
    """A model catalog entry an admin added (same shape as the built-in catalog file)."""

    __tablename__ = "catalog_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[str] = mapped_column(Text)  # JSON, services.catalog.Entry
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CatalogDeploy(Base):
    """A one-click deployment from the catalog: downloads, then a model registered with launch arguments."""

    __tablename__ = "catalog_deploys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    entry_id: Mapped[str] = mapped_column(String(64))
    variant_id: Mapped[str] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(16))
    model_name: Mapped[str] = mapped_column(String(128))
    args: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # The GGUF file (first part) or the directory the model is registered with.
    target_path: Mapped[str] = mapped_column(Text)
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    set_default: Mapped[bool] = mapped_column(Boolean, default=False)
    task_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of download_tasks ids
    # downloading | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="downloading", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # NULL = exclusive booking: llama-server is stopped and the whole GPU is free for other work.
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("model_presets.id", ondelete="SET NULL"), nullable=True)
    series_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True, index=True)
    # A dev environment booking: the GPU is handed to a container built from this template.
    devenv_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_templates.id", ondelete="SET NULL"), nullable=True
    )
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # booked | pending (awaiting approval) | cancelled | rejected | expired
    # (upcoming / active / finished is derived from time for booked rows)
    status: Mapped[str] = mapped_column(String(16), default="booked", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    api_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True
    )
    model_name: Mapped[str] = mapped_column(String(128), default="")
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(128))
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class ModelLoadEvent(Base):
    __tablename__ = "model_load_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    # What was run and how it ended, so a failure can be understood after the log has moved on.
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(Base):
    """Admin-edited overrides for a subset of Settings (see services/app_settings.py)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Kept so the log still reads well after the actor is deleted.
    actor_name: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(160), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), nullable=True)
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("model_presets.id", ondelete="SET NULL"), nullable=True)
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    note: Mapped[str] = mapped_column(Text, default="")
    auto_book: Mapped[bool] = mapped_column(Boolean, default=True)
    # waiting | notified | booked | expired | cancelled
    status: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class SentNotification(Base):
    """De-duplicates one-off notifications (reminders, fallbacks) across scheduler ticks and restarts."""

    __tablename__ = "sent_notifications"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class UsageDaily(Base):
    """Per-day roll-up of usage_logs rows that are past the retention period (UTC dates)."""

    __tablename__ = "usage_daily"
    __table_args__ = (PrimaryKeyConstraint("date", "user_id", "model_name"),)

    date: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    model_name: Mapped[str] = mapped_column(String(128))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class KeyValue(Base):
    """Small runtime state that must survive restarts (e.g. the admin's manual model choice)."""

    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OutboxItem(Base):
    """Notifications waiting to be (re)sent: undelivered messages and events saved at shutdown."""

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # message | event
    payload: Mapped[str] = mapped_column(Text)  # JSON
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# Statuses that occupy calendar time (and count towards quotas).
HOLDING_STATUSES = ("booked", "pending")


class Lane(Base):
    """A group of GPUs with its own inference process, scheduler and booking calendar."""

    __tablename__ = "lanes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Comma-separated CUDA indices ("0,1"); empty = do not restrict (no GPUs detected).
    gpu_indices: Mapped[str] = mapped_column(String(64), default="")
    default_model_id: Mapped[int | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def devices(self) -> list[int] | None:
        return [int(i) for i in self.gpu_indices.split(",") if i.strip()] if self.gpu_indices else None


class SshKey(Base):
    """Public keys a person logs into dev environments with."""

    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DevTemplate(Base):
    """An image to start dev environments from: built in, added by someone, or saved from a running one."""

    __tablename__ = "devenv_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str | None] = mapped_column(Text, nullable=True)  # main process; empty = keep running idle
    env: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {name: value}
    # Web interfaces the environment serves, opened through SlotServe's proxy at /devenv/<session>/port/<port>/:
    # JSON [{name, port, kind}], kind desktop | jupyter | vscode | http. keep_entrypoint: start the image's own
    # entrypoint (e.g. an init system) and add SSH with ``docker exec`` instead of SlotServe's start script.
    web_ports: Mapped[str | None] = mapped_column(Text, nullable=True)
    keep_entrypoint: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    kind: Mapped[str] = mapped_column(String(16), default="custom")  # builtin | custom | saved
    builtin_key: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DevSession(Base):
    """The container behind one dev environment booking."""

    __tablename__ = "devenv_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_templates.id", ondelete="SET NULL"), nullable=True
    )
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    image: Mapped[str] = mapped_column(String(255))
    # waiting | pulling | starting | running | stopping | stopped | failed
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # pull progress, saving, ...
    progress: Mapped[float | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON while running: how SlotServe reaches the container's ports {token, ports, container, ip, published}
    web: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
