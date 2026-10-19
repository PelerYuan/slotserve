from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth -----------------------------------------------------------------


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=6, max_length=128)
    invite_code: str


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class UserOut(ORMModel):
    id: int
    username: str
    is_admin: bool
    created_at: datetime


# ---- api keys -------------------------------------------------------------


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ApiKeyOut(ORMModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str


# ---- models ---------------------------------------------------------------


class ModelOut(BaseModel):
    id: int
    name: str
    file_path: str
    file_name: str
    file_exists: bool
    size_bytes: int
    status: str
    is_default: bool
    is_loaded: bool
    args: str
    description: str
    est_load_seconds: float
    created_by: int | None
    created_by_name: str | None
    created_at: datetime


class ModelUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\s/]+$")
    args: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=2000)


class ScanResultOut(BaseModel):
    added: list[str]
    skipped: int


# ---- downloads ------------------------------------------------------------


class DownloadCreateIn(BaseModel):
    url: str = Field(min_length=8, max_length=4000)
    filename: str | None = Field(default=None, max_length=255)


class DownloadOut(ORMModel):
    id: int
    user_id: int | None
    username: str | None = None
    url: str
    filename: str
    status: str
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: float
    error: str | None
    model_id: int | None
    created_at: datetime
    finished_at: datetime | None


# ---- reservations ---------------------------------------------------------


class ReservationCreateIn(BaseModel):
    model_id: int
    start_at: datetime
    end_at: datetime
    note: str = Field(default="", max_length=500)


class ReservationOut(BaseModel):
    id: int
    user_id: int
    username: str
    model_id: int
    model_name: str
    start_at: datetime
    end_at: datetime
    status: str
    phase: str  # upcoming | active | finished | cancelled
    note: str
    released_at: datetime | None
    created_at: datetime


# ---- status ---------------------------------------------------------------


class GpuOut(BaseModel):
    index: int
    name: str
    memory_used_mb: int
    memory_total_mb: int
    utilization_pct: int
    temperature_c: int
    power_w: float | None = None


class RuntimeOut(BaseModel):
    state: str  # stopped | loading | ready | stopping | failed
    model_id: int | None
    model_name: str | None
    load_started_at: datetime | None
    ready_at: datetime | None
    est_load_seconds: float | None
    error: str | None


class StatusOut(BaseModel):
    now: datetime
    runtime: RuntimeOut
    active_reservation: ReservationOut | None
    upcoming: list[ReservationOut]
    free_access: bool
    default_model: str | None
    gpus: list[GpuOut]


class ConfigOut(BaseModel):
    slot_minutes: int
    max_reservation_hours: int
    max_advance_days: int
    version: str


# ---- usage ----------------------------------------------------------------


class UsageUserRow(BaseModel):
    user_id: int
    username: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    reserved_hours: float


class UsageModelRow(BaseModel):
    model_name: str
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageDayRow(BaseModel):
    date: str
    username: str
    requests: int
    tokens: int


class UsageOut(BaseModel):
    days: int
    users: list[UsageUserRow]
    models: list[UsageModelRow]
    daily: list[UsageDayRow]
