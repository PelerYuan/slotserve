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
    locale: str | None = None


class PreferencesIn(BaseModel):
    locale: str | None = Field(default=None, pattern=r"^(en|zh-CN)$")
    notify_prefs: dict[str, bool] | None = None


class AdminUserOut(UserOut):
    disabled_at: datetime | None
    last_active_at: datetime | None
    key_count: int
    tokens_30d: int
    requests_30d: int
    upcoming_reservations: int


class AdminUserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=6, max_length=128)
    is_admin: bool = False


class AdminUserUpdateIn(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


class AdminPasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


# ---- api keys -------------------------------------------------------------


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ApiKeyOut(ORMModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revealable: bool = False  # the owner can view the full key again


class ApiKeyRevealOut(BaseModel):
    key: str


class ApiKeyCreatedOut(ApiKeyOut):
    key: str


class AdminApiKeyOut(ApiKeyOut):
    user_id: int
    username: str


# ---- models ---------------------------------------------------------------


class PresetOut(ORMModel):
    id: int
    name: str
    args: str


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    args: str = Field(default="", max_length=4000)


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
    presets: list[PresetOut] = []
    engine: str = "llama.cpp"
    default_lane_ids: list[int] = []
    # The weights alone are larger than the GPU memory: full offload (-ngl 99) will not fit.
    exceeds_vram: bool = False


class ModelUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\s/]+$")
    args: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=2000)
    engine: str | None = Field(default=None, pattern=r"^(llama\.cpp|vllm|sglang|custom)$")
    path: str | None = Field(default=None, min_length=1, max_length=1000)


class ModelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\s/]+$")
    engine: str = Field(default="llama.cpp", pattern=r"^(llama\.cpp|vllm|sglang|custom)$")
    path: str = Field(min_length=1, max_length=1000)
    args: str = Field(default="", max_length=4000)
    description: str = Field(default="", max_length=2000)


class ScanResultOut(BaseModel):
    added: list[str]
    skipped: int


# ---- downloads ------------------------------------------------------------


class DownloadCreateIn(BaseModel):
    url: str = Field(min_length=8, max_length=4000)
    filename: str | None = Field(default=None, max_length=255)


class HfFileOut(BaseModel):
    path: str  # primary file (first part for split models)
    files: list[str]  # every file to download, primary last
    size_bytes: int
    parts: int
    kind: str  # model | mmproj


class HfFilesOut(BaseModel):
    repo: str
    revision: str
    files: list[HfFileOut]


class HfDownloadIn(BaseModel):
    repo: str = Field(min_length=3, max_length=200, pattern=r"^[\w.\-]+/[\w.\-]+$")
    revision: str = Field(default="main", min_length=1, max_length=100)
    files: list[str] = Field(min_length=1, max_length=50)


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
    model_id: int | None = None  # None = exclusive (whole GPU, no model)
    preset_id: int | None = None
    start_at: datetime
    end_at: datetime
    note: str = Field(default="", max_length=500)
    repeat_weeks: int = Field(default=0, ge=0, le=52)
    user_id: int | None = None  # admins may book for someone else
    lane_id: int | None = None  # None = the first lane


class ReservationUpdateIn(BaseModel):
    """Only the fields that are sent change; ``model_id: null`` turns the booking exclusive."""

    model_id: int | None = None
    preset_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    lane_id: int | None = None


class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=500)
    series: bool = False


class ExtendIn(BaseModel):
    minutes: int = Field(ge=5, le=24 * 60)


class ReservationOut(BaseModel):
    id: int
    user_id: int
    username: str
    model_id: int | None
    model_name: str | None
    preset_id: int | None = None
    preset_name: str | None = None
    exclusive: bool = False
    series_id: str | None = None
    lane_id: int | None = None
    lane_name: str | None = None
    start_at: datetime
    end_at: datetime
    status: str
    phase: str  # upcoming | active | finished | cancelled | pending | rejected | expired
    note: str
    review_note: str | None = None
    reviewed_by: str | None = None
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
    preset_name: str | None = None
    load_started_at: datetime | None
    ready_at: datetime | None
    est_load_seconds: float | None
    error: str | None
    error_hint: str | None = None
    draining: bool = False
    # While loading: stage and weight progress read from the server's log (None when it says nothing useful).
    stage: str | None = None
    log_percent: float | None = None


class ManualOut(BaseModel):
    model_name: str | None  # None = unloaded
    preset_name: str | None
    until: datetime | None
    by: str


class LaneStatusOut(BaseModel):
    id: int
    name: str
    gpus: list[int] | None
    runtime: RuntimeOut
    reason: str | None = None
    fallback_from: str | None = None
    manual: ManualOut | None = None
    active_reservation: ReservationOut | None = None
    next_reservation: ReservationOut | None = None
    default_model: str | None = None


class LaneOut(BaseModel):
    id: int
    name: str
    gpus: list[int]
    default_model_id: int | None
    default_model_name: str | None
    position: int


class LaneIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    gpus: list[int] = Field(default_factory=list, max_length=64)
    default_model_id: int | None = None
    position: int | None = None


class StatusOut(BaseModel):
    now: datetime
    lanes: list[LaneStatusOut] = []
    runtime: RuntimeOut
    reason: str | None = None
    fallback_from: str | None = None
    manual: ManualOut | None = None
    active_reservation: ReservationOut | None
    upcoming: list[ReservationOut]
    free_access: bool
    default_model: str | None
    gpus: list[GpuOut]


class ConfigOut(BaseModel):
    slot_minutes: int
    max_reservation_hours: int
    max_advance_days: int
    registration_open: bool
    quota_weekly_hours: int = 0
    quota_upcoming: int = 0
    approval_mode: str = "off"
    approval_hours: int = 4
    telegram_enabled: bool = False
    version: str


class ManualIn(BaseModel):
    action: str = Field(pattern="^(load|unload|auto)$")
    lane_id: int | None = None
    model_id: int | None = None
    preset_id: int | None = None
    hours: float | None = Field(default=None, gt=0, le=24 * 7)  # None = until the next booking


class RequestLogOut(BaseModel):
    id: int
    created_at: datetime
    username: str
    key_name: str | None
    model_name: str
    endpoint: str
    stream: bool
    status_code: int
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int


class RequestPageOut(BaseModel):
    total: int
    items: list[RequestLogOut]


# ---- waitlist -------------------------------------------------------------


class WaitlistIn(BaseModel):
    lane_id: int | None = None
    model_id: int | None = None
    preset_id: int | None = None
    start_at: datetime
    end_at: datetime
    note: str = Field(default="", max_length=500)
    auto_book: bool = True


class WaitlistOut(BaseModel):
    id: int
    lane_id: int | None = None
    lane_name: str | None = None
    user_id: int
    username: str
    model_id: int | None
    model_name: str | None
    preset_name: str | None
    exclusive: bool
    start_at: datetime
    end_at: datetime
    note: str
    auto_book: bool
    status: str
    reservation_id: int | None
    created_at: datetime


# ---- telegram & calendar -------------------------------------------------------


class TelegramOut(BaseModel):
    enabled: bool
    bot_username: str | None
    linked: bool
    telegram_username: str | None
    link_url: str | None = None
    notify_prefs: dict[str, bool]


class CalendarOut(BaseModel):
    mine_url: str
    all_url: str


# ---- admin ----------------------------------------------------------------


class LoadEventOut(BaseModel):
    id: int
    model_name: str | None
    started_at: datetime
    finished_at: datetime
    success: bool
    error: str | None
    lane_id: int | None = None
    lane_name: str | None = None
    command: str | None = None
    exit_code: int | None = None
    log_excerpt: str | None = None


class LaneRuntimeOut(BaseModel):
    """Everything an admin may want to know about one lane's model server."""

    lane_id: int
    lane_name: str
    devices: list[int] | None
    port: int
    log_file: str
    runtime: RuntimeOut
    engine: str | None
    command: str | None
    pid: int | None
    exit_code: int | None
    inflight: int
    fail_count: int
    next_retry_at: datetime | None
    failure_log: list[str]
    target_model: str | None
    target_reason: str | None


class AdminStatsOut(BaseModel):
    users: int
    disabled_users: int
    admins: int
    active_keys: int
    upcoming_reservations: int
    pending_reservations: int = 0
    requests_24h: int
    tokens_24h: int
    failed_requests_24h: int
    models: int
    load_events: list[LoadEventOut]


class SettingOut(BaseModel):
    key: str
    value: bool | int | float | str
    default: bool | int | float | str
    overridden: bool
    secret: bool
    kind: str = "str"  # bool | int | float | str
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    choices: list[int | float | str] | None = None


class AuditOut(BaseModel):
    id: int
    actor_id: int | None
    actor_name: str
    action: str
    target: str
    detail: dict
    created_at: datetime


class AuditPageOut(BaseModel):
    total: int
    items: list[AuditOut]


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


class BackupOut(BaseModel):
    name: str
    kind: str  # daily | manual | upgrade
    size_bytes: int
    created_at: datetime
