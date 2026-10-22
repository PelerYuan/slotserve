from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
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
    # JSON list of chat channels (telegram, feishu, ...) the person switched notifications off for.
    channels_off: Mapped[str | None] = mapped_column(Text, nullable=True)
    ical_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # SSH password for dev environments (separate from the web login), as a SHA-512-crypt hash only.
    devenv_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Signed up with an invite that asks for an admin's approval: cannot sign in until approved.
    approval_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # The invite (``invites.id``) used to sign up; no foreign key, the invite may be deleted later.
    invite_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
    # security/credentials.py: api (made by its owner) | env (issued to one dev environment)
    kind: Mapped[str] = mapped_column(String(8), default="api", server_default="api")
    # What the key may do; "llm" = the OpenAI-compatible /v1 API. Never an admin scope (D15).
    scope: Mapped[str] = mapped_column(String(64), default="llm", server_default="llm")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # kind env: the dev environment the key belongs to; it stops working when the environment stops.
    devenv_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LoginSession(Base):
    """A signed-in browser (security/sessions.py). The cookie holds a random token; only its hash is stored."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON: what request.session holds
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # logout | password | disabled | revoked | replaced
    revoked_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Invite(Base):
    """An invite link (``ssi-...``) admins hand out instead of the global invite code (security/credentials.py)."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    max_uses: Mapped[int] = mapped_column(Integer, default=50)
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # People who sign up with it wait for an admin's approval before they can sign in.
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
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
    # Launch arguments the owner asked for that need an admin's approval first; ``args`` stays in use until then.
    args_pending: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # storage hook: a model library (Storage with purpose "models") to download into; NULL = MODELS_DIR
    storage_id: Mapped[int | None] = mapped_column(ForeignKey("storages.id", ondelete="SET NULL"), nullable=True)


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
    # ... continuing a kept dev environment (its container is paused between bookings).
    devenv_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_projects.id", ondelete="SET NULL"), nullable=True, index=True
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
    # api (an API key) | env (a dev environment's key) | playground (the web page, no key)
    source: Mapped[str] = mapped_column(String(16), default="api", server_default="api")


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


class ChatLink(Base):
    """A person's account on a chat platform other than Telegram (Telegram uses the columns on ``users``).

    While ``address`` is empty the row only carries a pending bind ``code``.
    """

    __tablename__ = "chat_links"
    __table_args__ = (UniqueConstraint("user_id", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)  # feishu | dingtalk | wecom | qq
    address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)  # platform user id
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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
    # What runs while nobody has the lane booked: model (the default model; also NULL) | devenv | none
    idle_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # idle_mode devenv: the template of the shared environment everyone can log into.
    idle_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_templates.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def mode(self) -> str:
        return self.idle_mode or "model"

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
    # JSON list of extra paths inside the container that are kept between bookings (besides /workspace).
    persist_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DevProject(Base):
    """A dev environment someone keeps: its container is stopped ("paused") between bookings, not removed."""

    __tablename__ = "devenv_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_templates.id", ondelete="SET NULL"), nullable=True
    )
    # The image the container is (re)created from: the template's, or a snapshot taken when the GPUs changed.
    image: Mapped[str] = mapped_column(String(255))
    persist_paths: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    # new (no container yet) | running | paused | missing (the container disappeared)
    state: Mapped[str] = mapped_column(String(16), default="new")
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # What the container was created with; a different lane or paths mean recreating it from a snapshot.
    container_devices: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON: the web interfaces the container was created with {token, ports, base_session} (kept across bookings)
    container_web: Mapped[str | None] = mapped_column(Text, nullable=True)
    # storage hook: signature of the managed storage mounts the container was created with (services/storage.py)
    container_storage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # devenv_cpu: when a booking of it ends, keep it running without a GPU instead of pausing it
    cpu_after_booking: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)


class DevSession(Base):
    """The container behind one dev environment booking, or the shared one of an idle lane."""

    __tablename__ = "devenv_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # booking | shared (an idle lane's environment: no booking, no owner, everyone has an account)
    # | cpu (started by its owner without a GPU and without a booking; see services/devenv_cpu.py)
    kind: Mapped[str | None] = mapped_column(String(16), default="booking", nullable=True)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("devenv_projects.id", ondelete="SET NULL"), nullable=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_templates.id", ondelete="SET NULL"), nullable=True
    )
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    image: Mapped[str] = mapped_column(String(255))
    # waiting | pulling | ready | starting | running | stopping | stopped | failed
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


class DevPort(Base):
    """A container port of a dev environment: one someone added, or a declared one whose setting was changed.

    ``mode`` direct: published on ``host_port`` of the host through a forwarder container (no SlotServe login);
    login: opened through SlotServe's web proxy. A kept environment's ports belong to its project (the host port
    stays while it is paused); any other environment's to its session, until the booking ends.
    """

    __tablename__ = "devenv_ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("devenv_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    container_port: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(32), default="")
    mode: Mapped[str] = mapped_column(String(8), default="login")  # direct | login
    declared: Mapped[bool] = mapped_column(Boolean, default=False)  # the template declares this port
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # forwarder first ran
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# What a resource was doing: bookings, shared use while nobody books, or nothing.
# ``cpu_devenv`` is reserved for CPU-only dev environments (resource "cpu").
BOOKING_KINDS = ("model", "gpu", "devenv", "cpu_devenv")
IDLE_KINDS = ("idle_model", "idle_devenv")
USAGE_KINDS = (*BOOKING_KINDS, *IDLE_KINDS, "free")
USAGE_RESOURCES = ("gpu", "cpu")


class UsageSample(Base):
    """What one resource did over a stretch of time: a row per minute, rolled up per hour later.

    ``resource`` "gpu": one row per GPU of a lane; ``gpu_index`` -1 stands for a lane without GPU
    metrics (no nvidia-smi), where occupancy is still recorded. ``resource`` "cpu" (CPU-only
    environments, sampled by services/devenv_cpu.py) uses ``gpu_index`` -1, ``util_seconds`` for CPU % (of the
    environment's core limit), ``cpu_core_seconds`` for cores in use and ``mem_peak_mb`` for RAM.
    """

    __tablename__ = "usage_samples"
    __table_args__ = (Index("ix_usage_samples_start_lane", "start", "lane_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    start: Mapped[datetime] = mapped_column(UTCDateTime)
    hourly: Mapped[bool] = mapped_column(Boolean, default=False)  # a roll-up of minute samples
    seconds: Mapped[int] = mapped_column(Integer)  # time this row stands for
    resource: Mapped[str] = mapped_column(String(8), default="gpu")  # see USAGE_RESOURCES
    lane_id: Mapped[int | None] = mapped_column(ForeignKey("lanes.id", ondelete="SET NULL"), nullable=True)
    gpu_index: Mapped[int] = mapped_column(Integer, default=-1)
    kind: Mapped[str] = mapped_column(String(16))  # see USAGE_KINDS
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metered_seconds: Mapped[int] = mapped_column(Integer, default=0)  # time with GPU metrics
    util_seconds: Mapped[float] = mapped_column(Float, default=0.0)  # utilization % x seconds
    busy_seconds: Mapped[int] = mapped_column(Integer, default=0)  # time at or above the "busy" threshold
    energy_wh: Mapped[float] = mapped_column(Float, default=0.0)
    mem_peak_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mem_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_core_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # resource "cpu" only


class Storage(Base):
    """Storage admins manage in the panel (services/storage.py): a directory on the Docker host, an NFS export or
    an SMB/CIFS share, used for one purpose: shared data, personal spaces, kept project data or a model library."""

    __tablename__ = "storages"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(8))  # host | nfs | smb
    purpose: Mapped[str] = mapped_column(String(16))  # shared | personal | projects | models
    host_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # host: directory on the Docker host
    server: Mapped[str | None] = mapped_column(String(255), nullable=True)  # nfs / smb: host name or address
    remote_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # nfs: export path; smb: share[/dir]
    options: Mapped[str | None] = mapped_column(Text, nullable=True)  # extra mount options (comma separated)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)  # smb
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # smb, encrypted with SECRET_KEY
    mount_path: Mapped[str | None] = mapped_column(String(255), nullable=True)  # in containers (shared, models)
    read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    access: Mapped[str] = mapped_column(String(16), default="everyone")  # everyone | users | admins
    user_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list (access "users")
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # models: where SlotServe sees it
    quota_gb: Mapped[float | None] = mapped_column(Float, nullable=True)  # personal: soft limit per person
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: the last check
    usage: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {user id: bytes} (personal)
    warned: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: user ids told they are over quota
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
