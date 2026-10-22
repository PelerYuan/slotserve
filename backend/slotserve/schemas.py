from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .services.diagnostics import Diagnosis


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
    # Signed up with an invite that needs approval: not signed in, waits for an admin.
    approval_pending: bool = False


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
    # What the key may do: only "llm" (the /v1 API) exists; there is never an admin scope (D15).
    scope: Literal["llm"] = "llm"
    # Days until the key stops working; None = it does not expire.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyOut(ORMModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revealable: bool = False  # the owner can view the full key again
    kind: str = "api"  # api | env (a dev environment's key, security/credentials.py)
    scope: str = "llm"
    expires_at: datetime | None = None
    expired: bool = False
    last_ip: str | None = None


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
    # Admins: yes, really use flags that can run code or write files (otherwise 400 args_confirm_required).
    confirm_args: bool = False


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
    # Launch arguments waiting for an admin's approval, and the flags in them that need it.
    args_pending: str | None = None
    needs_approval: list[str] = []


class ModelArgsReviewIn(BaseModel):
    confirm_args: bool = False


class ModelUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\s/]+$")
    args: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=2000)
    engine: str | None = Field(default=None, pattern=r"^(llama\.cpp|vllm|sglang|custom)$")
    path: str | None = Field(default=None, min_length=1, max_length=1000)
    # Admins: confirm flags that can run code or write files. Members: store arguments that need an admin as a
    # request instead of failing with 400 args_need_approval.
    confirm_args: bool = False
    request_approval: bool = False


class ModelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\s/]+$")
    engine: str = Field(default="llama.cpp", pattern=r"^(llama\.cpp|vllm|sglang|custom)$")
    path: str = Field(min_length=1, max_length=1000)
    args: str = Field(default="", max_length=4000)
    description: str = Field(default="", max_length=2000)
    confirm_args: bool = False
    request_approval: bool = False


class ScanResultOut(BaseModel):
    added: list[str]
    skipped: int


# ---- downloads ------------------------------------------------------------


class DownloadCreateIn(BaseModel):
    url: str = Field(min_length=8, max_length=4000)
    filename: str | None = Field(default=None, max_length=255)
    storage_id: int | None = None  # storage hook: a model library; None = the models directory


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
    storage_id: int | None = None  # storage hook: a model library; None = the models directory


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
    storage_id: int | None = None  # storage hook
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
    devenv_template_id: int | None = None  # a dev environment booking (model_id stays None)
    devenv_project_id: int | None = None  # ... continuing a kept environment
    devenv_project_name: str | None = Field(default=None, max_length=64)  # ... keeping it as a new project


class ReservationUpdateIn(BaseModel):
    """Only the fields that are sent change; ``model_id: null`` turns the booking exclusive."""

    model_id: int | None = None
    preset_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    lane_id: int | None = None
    devenv_template_id: int | None = None
    devenv_project_id: int | None = None


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
    devenv_template_id: int | None = None
    devenv_template_name: str | None = None
    devenv_project_id: int | None = None
    devenv_project_name: str | None = None
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
    idle_mode: str = "model"  # model | devenv | none: what runs while nobody has the lane booked
    idle_template_name: str | None = None
    shared_state: str | None = None  # state of the lane's shared dev environment, when there is one


class LaneOut(BaseModel):
    id: int
    name: str
    gpus: list[int]
    default_model_id: int | None
    default_model_name: str | None
    position: int
    idle_mode: str = "model"
    idle_template_id: int | None = None
    idle_template_name: str | None = None


class LaneIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    gpus: list[int] = Field(default_factory=list, max_length=64)
    default_model_id: int | None = None
    position: int | None = None
    # What runs while the lane is not booked: model (its default model), devenv (a shared environment), none.
    # Left out on an update: unchanged.
    idle_mode: str | None = Field(default=None, pattern=r"^(model|devenv|none)$")
    idle_template_id: int | None = None


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
    devenv_enabled: bool = False
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
    source: str = "api"  # api | env | playground


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
    diagnosis: Diagnosis | None = None  # failed loads only: likely cause and next steps (services/diagnostics)


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
    diagnosis: Diagnosis | None = None  # while the lane's server is failed


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
    """One setting with its registry metadata (services/app_settings.py)."""

    key: str
    value: bool | int | float | str | None
    default: bool | int | float | str | None
    overridden: bool
    secret: bool
    kind: str = "str"  # bool | int | float | str
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    choices: list[int | float | str] | None = None
    # registry metadata (A04)
    env_var: str = ""
    group: str = "other"
    card: str | None = None
    tier: str = "advanced"  # basic | advanced | expert
    order: int = 0
    depends_on: str | None = None  # "key" (shown while that setting is on/non-empty) or "key=value"
    keywords: list[str] = []
    scope: str = "global"
    danger: str | None = None  # "root": can run commands on the host; needs sudo once enforced
    restart_required: bool = False  # deployment parameter, read-only
    has_impact: bool = False  # PUT ?dry_run=true computes who is affected
    has_value: bool = True  # for secrets: whether one is set (the value itself may be masked)
    source: str = "default"  # default | env | admin


class SettingChangeOut(BaseModel):
    key: str
    before: bool | int | float | str | None  # secrets: fingerprint "fp:xxxxxxxx" or "" (not set)
    after: bool | int | float | str | None
    reset: bool  # back to the .env default
    secret: bool = False
    danger: str | None = None


class ImpactUserOut(BaseModel):
    model_config = ConfigDict(extra="allow")  # numbers that depend on the setting (hours_this_week, ...)

    user_id: int
    username: str
    over: bool = False


class ImpactReservationOut(BaseModel):
    id: int
    user_id: int
    username: str
    lane_id: int | None
    start_at: datetime
    end_at: datetime
    status: str
    reason: str  # over_quota | over_upcoming | too_long | too_far | would_need_approval | still_pending


class ImpactOut(BaseModel):
    key: str
    text_key: str
    params: dict
    users: list[ImpactUserOut]
    reservations: list[ImpactReservationOut]


class SettingsPreviewOut(BaseModel):
    """``PUT /api/admin/settings?dry_run=true``: what a save would do. Nothing is stored."""

    dry_run: bool = True
    changes: list[SettingChangeOut]
    impacts: list[ImpactOut]
    sudo_required: bool  # the save will need sudo mode (danger settings, once enforced)
    notify_user_ids: list[int]  # people ?notify=true would message


class SettingsHistoryOut(BaseModel):
    audit_id: int
    action: str
    actor_name: str
    created_at: datetime
    before: bool | int | float | str | None
    after: bool | int | float | str | None
    reset: bool = False
    undoable: bool  # not a secret, not an undo target already superseded


class AuditOut(BaseModel):
    id: int
    actor_id: int | None
    actor_name: str
    action: str
    target: str
    detail: dict
    created_at: datetime
    ip: str = ""


class QuotaGrantIn(BaseModel):
    extra_hours: float = Field(gt=0, le=24 * 7)
    until: datetime
    starts_at: datetime | None = None  # default: now
    reason: str = Field(default="", max_length=200)


class QuotaGrantOut(BaseModel):
    id: str
    user_id: int
    extra_hours: float
    starts_at: datetime
    until: datetime
    reason: str
    by: str
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str = ""
    active: bool


class UserPolicyOut(BaseModel):
    """The limits one person books under (global settings + extra hours); 0 = unlimited."""

    user_id: int
    username: str
    exempt: bool  # admins
    quota_weekly_hours: float  # this week, including extra hours
    base_weekly_hours: int
    extra_hours: float
    quota_upcoming: int
    max_reservation_hours: int
    max_advance_days: int
    week_start: datetime
    booked_hours_this_week: float
    open_bookings: int
    grants: list[QuotaGrantOut]


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
    reserved_hours: float  # calendar hours booked
    bookings: int = 0
    booked_gpu_hours: float = 0.0  # booked hours x GPUs of the lane
    used_gpu_hours: float = 0.0  # sampled GPU time at or above the busy threshold
    avg_utilization: float | None = None  # % during their bookings; None without GPU metrics
    peak_memory_mb: int | None = None
    energy_wh: float = 0.0
    cpu_core_hours: float = 0.0  # CPU-only environments (resource "cpu")


class UsageTotals(BaseModel):
    booked_gpu_hours: float
    used_gpu_hours: float
    sampled_gpu_hours: float
    avg_utilization: float | None
    energy_wh: float
    peak_memory_mb: int | None
    requests: int
    tokens: int
    cpu_core_hours: float = 0.0


class UsageLaneRow(BaseModel):
    lane_id: int | None
    lane_name: str
    gpus: int
    booked_gpu_hours: float
    used_gpu_hours: float
    avg_utilization: float | None
    energy_wh: float
    peak_memory_mb: int | None
    occupancy: dict[str, float]  # kind -> sampled GPU-hours


class UsageGpuRow(BaseModel):
    lane_id: int | None
    lane_name: str
    gpu_index: int  # -1: the lane has no GPU metrics
    gpu_hours: float
    used_gpu_hours: float
    avg_utilization: float | None
    energy_wh: float
    peak_memory_mb: int | None
    memory_total_mb: int | None


class UsageKindRow(BaseModel):
    kind: str
    gpu_hours: float
    used_gpu_hours: float
    avg_utilization: float | None
    energy_wh: float


class UsageSeries(BaseModel):
    key: str
    name: str
    values: list[float | None]


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
    scope: str = "all"  # all | mine
    busy_percent: int = 0
    metered: bool = False  # whether any GPU metrics were recorded in the period
    totals: UsageTotals | None = None
    lanes: list[UsageLaneRow] = []
    gpus: list[UsageGpuRow] = []
    kinds: list[UsageKindRow] = []
    # Time series over ``buckets`` (local start times, YYYY-MM-DDTHH:MM, each ``bucket_hours`` long).
    bucket_hours: int = 24
    buckets: list[str] = []
    hours_by_person: list[UsageSeries] = []  # booked GPU-hours
    hours_by_kind: list[UsageSeries] = []  # sampled GPU-hours, including idle and free time
    utilization_by_lane: list[UsageSeries] = []  # average utilization %, None without metrics


class BackupOut(BaseModel):
    name: str
    kind: str  # daily | manual | upgrade
    size_bytes: int
    created_at: datetime
