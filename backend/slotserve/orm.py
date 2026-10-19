from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
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
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


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


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id", ondelete="RESTRICT"), index=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # booked | cancelled  (upcoming / active / finished is derived from time)
    status: Mapped[str] = mapped_column(String(16), default="booked", index=True)
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
