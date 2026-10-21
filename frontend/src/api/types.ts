export interface User {
  id: number
  username: string
  is_admin: boolean
  created_at: string
  locale?: string | null
}

export interface ApiKey {
  id: number
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revealable: boolean
}

export interface ApiKeyCreated extends ApiKey {
  key: string
}

export interface Model {
  id: number
  name: string
  file_path: string
  file_name: string
  file_exists: boolean
  size_bytes: number
  status: 'downloading' | 'ready' | 'failed'
  is_default: boolean
  is_loaded: boolean
  args: string
  description: string
  est_load_seconds: number
  created_by: number | null
  created_by_name: string | null
  created_at: string
  presets: Preset[]
  exceeds_vram: boolean
  engine: Engine
  default_lane_ids: number[]
}

export type Engine = 'llama.cpp' | 'vllm' | 'sglang' | 'custom'

export interface Preset {
  id: number
  name: string
  args: string
}

export interface DownloadTask {
  id: number
  user_id: number | null
  username: string | null
  url: string
  filename: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  downloaded_bytes: number
  total_bytes: number | null
  speed_bps: number
  error: string | null
  model_id: number | null
  created_at: string
  finished_at: string | null
}

export type ReservationPhase = 'upcoming' | 'active' | 'finished' | 'cancelled' | 'pending' | 'rejected' | 'expired'

export interface Reservation {
  id: number
  user_id: number
  username: string
  model_id: number | null
  model_name: string | null
  preset_id: number | null
  preset_name: string | null
  exclusive: boolean
  devenv_template_id?: number | null
  devenv_template_name?: string | null
  series_id: string | null
  lane_id: number | null
  lane_name: string | null
  start_at: string
  end_at: string
  status: 'booked' | 'pending' | 'cancelled' | 'rejected' | 'expired'
  phase: ReservationPhase
  note: string
  review_note: string | null
  reviewed_by: string | null
  released_at: string | null
  created_at: string
}

export type RuntimeState = 'stopped' | 'loading' | 'ready' | 'stopping' | 'failed'

export interface Runtime {
  state: RuntimeState
  model_id: number | null
  model_name: string | null
  preset_name: string | null
  load_started_at: string | null
  ready_at: string | null
  est_load_seconds: number | null
  error: string | null
  error_hint: 'oom' | null
  draining: boolean
  stage?: 'weights' | 'context' | 'warmup' | 'ready' | null
  log_percent?: number | null
}

export interface Gpu {
  index: number
  name: string
  memory_used_mb: number
  memory_total_mb: number
  utilization_pct: number
  temperature_c: number
  power_w: number | null
}

export interface ManualOverride {
  model_name: string | null
  preset_name: string | null
  until: string | null
  by: string
}

export interface LaneStatus {
  id: number
  name: string
  gpus: number[] | null
  runtime: Runtime
  reason: string | null
  fallback_from: string | null
  manual: ManualOverride | null
  active_reservation: Reservation | null
  next_reservation: Reservation | null
  default_model: string | null
}

export interface Lane {
  id: number
  name: string
  gpus: number[]
  default_model_id: number | null
  default_model_name: string | null
  position: number
}

export interface Status {
  now: string
  lanes: LaneStatus[]
  runtime: Runtime
  reason: string | null
  fallback_from: string | null
  manual: ManualOverride | null
  active_reservation: Reservation | null
  upcoming: Reservation[]
  free_access: boolean
  default_model: string | null
  gpus: Gpu[]
}

export interface AppConfig {
  slot_minutes: number
  max_reservation_hours: number
  max_advance_days: number
  registration_open: boolean
  quota_weekly_hours: number
  quota_upcoming: number
  approval_mode: 'off' | 'long' | 'all'
  approval_hours: number
  devenv_enabled?: boolean
  telegram_enabled: boolean
  version: string
}

/** model | gpu | devenv | cpu_devenv (bookings) | idle_model | idle_devenv (shared while nobody books) | free */
export type UsageKind = 'model' | 'gpu' | 'devenv' | 'cpu_devenv' | 'idle_model' | 'idle_devenv' | 'free'

export interface UsageSeries {
  key: string
  name: string
  values: (number | null)[]
}

export interface Usage {
  days: number
  scope: 'all' | 'mine'
  busy_percent: number
  metered: boolean
  users: {
    user_id: number
    username: string
    requests: number
    prompt_tokens: number
    completion_tokens: number
    reserved_hours: number
    bookings: number
    booked_gpu_hours: number
    used_gpu_hours: number
    avg_utilization: number | null
    peak_memory_mb: number | null
    energy_wh: number
    cpu_core_hours: number
  }[]
  models: { model_name: string; requests: number; prompt_tokens: number; completion_tokens: number }[]
  daily: { date: string; username: string; requests: number; tokens: number }[]
  totals: {
    booked_gpu_hours: number
    used_gpu_hours: number
    sampled_gpu_hours: number
    avg_utilization: number | null
    energy_wh: number
    peak_memory_mb: number | null
    requests: number
    tokens: number
    cpu_core_hours: number
  }
  lanes: {
    lane_id: number | null
    lane_name: string
    gpus: number
    booked_gpu_hours: number
    used_gpu_hours: number
    avg_utilization: number | null
    energy_wh: number
    peak_memory_mb: number | null
    occupancy: Partial<Record<UsageKind, number>>
  }[]
  gpus: {
    lane_id: number | null
    lane_name: string
    gpu_index: number
    gpu_hours: number
    used_gpu_hours: number
    avg_utilization: number | null
    energy_wh: number
    peak_memory_mb: number | null
    memory_total_mb: number | null
  }[]
  kinds: { kind: UsageKind; gpu_hours: number; used_gpu_hours: number; avg_utilization: number | null; energy_wh: number }[]
  bucket_hours: number
  buckets: string[]
  hours_by_person: UsageSeries[]
  hours_by_kind: UsageSeries[]
  utilization_by_lane: UsageSeries[]
}

export type UsageExport = 'summary' | 'requests' | 'compute' | 'people'

// ---- admin ----------------------------------------------------------------

export interface AdminUser extends User {
  disabled_at: string | null
  last_active_at: string | null
  key_count: number
  tokens_30d: number
  requests_30d: number
  upcoming_reservations: number
}

export interface AdminApiKey extends ApiKey {
  user_id: number
  username: string
}

export interface LoadEvent {
  id: number
  model_name: string | null
  started_at: string
  finished_at: string
  success: boolean
  error: string | null
  lane_id?: number | null
  lane_name?: string | null
  command?: string | null
  exit_code?: number | null
  log_excerpt?: string | null
}

export interface LaneRuntime {
  lane_id: number
  lane_name: string
  devices: number[] | null
  port: number
  log_file: string
  runtime: Runtime
  engine: string | null
  command: string | null
  pid: number | null
  exit_code: number | null
  inflight: number
  fail_count: number
  next_retry_at: string | null
  failure_log: string[]
  target_model: string | null
  target_reason: string | null
}

export interface AdminStats {
  users: number
  disabled_users: number
  admins: number
  active_keys: number
  upcoming_reservations: number
  pending_reservations: number
  requests_24h: number
  tokens_24h: number
  failed_requests_24h: number
  models: number
  load_events: LoadEvent[]
}

export type SettingValue = boolean | number | string

export interface AdminSetting {
  key: string
  value: SettingValue
  default: SettingValue
  overridden: boolean
  secret: boolean
  kind: 'bool' | 'int' | 'float' | 'str'
  unit: string | null
  min: number | null
  max: number | null
  choices: (number | string)[] | null
}

export interface AuditEntry {
  id: number
  actor_id: number | null
  actor_name: string
  action: string
  target: string
  detail: Record<string, unknown>
  created_at: string
}

export interface RequestLog {
  id: number
  created_at: string
  username: string
  key_name: string | null
  model_name: string
  endpoint: string
  stream: boolean
  status_code: number
  duration_ms: number
  prompt_tokens: number
  completion_tokens: number
}

// ---- booking extras -------------------------------------------------------

export interface BookingInput {
  lane_id?: number | null
  model_id: number | null
  preset_id?: number | null
  start_at: string
  end_at: string
  note?: string
  repeat_weeks?: number
  user_id?: number | null
  devenv_template_id?: number | null
}

export interface DevInfo {
  enabled: boolean
  docker: { ok: boolean; error?: string | null; version?: string | null }
  ssh_host: string
  host_key_fingerprint: string | null
  custom_images: boolean
  saved_limit: number
  pull_minutes: number
}

export type DevWebKind = 'desktop' | 'jupyter' | 'vscode' | 'http'

// A web interface a template declares, opened at /devenv/<session>/port/<port>/
export interface DevWebPort {
  name: string
  port: number
  kind: DevWebKind
}

export interface DevTemplate {
  id: number
  name: string
  image: string
  description: string
  command: string | null
  env: Record<string, string>
  web_ports: DevWebPort[]
  keep_entrypoint: boolean
  kind: 'builtin' | 'custom' | 'saved'
  builtin_key: string | null
  owner: string | null
  mine: boolean
  enabled: boolean
  created_at: string
}

export interface DevTemplateInput {
  name: string
  image: string
  description?: string
  command?: string | null
  env?: Record<string, string>
  web_ports?: DevWebPort[]
  keep_entrypoint?: boolean
  shared?: boolean
  enabled?: boolean
}

export type DevState = 'waiting' | 'pulling' | 'ready' | 'starting' | 'running' | 'stopping' | 'stopped' | 'failed'

export interface DevSession {
  id: number
  reservation_id: number
  username: string
  template_name: string | null
  image: string
  lane_name: string | null
  state: DevState
  detail: string | null
  progress: number | null
  error: string | null
  ssh_port: number | null
  ssh_command: string | null
  web: (DevWebPort & { url: string })[]
  start_at: string | null
  end_at: string | null
  started_at: string | null
  stopped_at: string | null
}

export interface SshKey {
  id: number
  name: string
  fingerprint: string
  key_type: string
  created_at: string
}

export interface WaitlistEntry {
  id: number
  lane_id: number | null
  lane_name: string | null
  user_id: number
  username: string
  model_id: number | null
  model_name: string | null
  preset_name: string | null
  exclusive: boolean
  start_at: string
  end_at: string
  note: string
  auto_book: boolean
  status: 'waiting' | 'notified' | 'booked' | 'expired' | 'cancelled'
  reservation_id: number | null
  created_at: string
}

export type NotifyPrefs = Record<string, boolean>

export interface TelegramInfo {
  enabled: boolean
  bot_username: string | null
  linked: boolean
  telegram_username: string | null
  link_url: string | null
  notify_prefs: NotifyPrefs
}

export interface ChatChannel {
  id: string
  label: string
  linked: boolean
  account: string | null
  bot_name: string | null
  notify: boolean
  link_kind: 'url' | 'code'
  receives: boolean
}

export interface ChatChannels {
  channels: ChatChannel[]
  notify_prefs: NotifyPrefs
}

export interface ChannelLink {
  channel: ChatChannel
  code: string | null
  expires_at: string | null
  link_url: string | null
}

export interface AdminChannelStatus {
  id: string
  label: string
  switched_on: boolean
  configured: boolean
  enabled: boolean
  connected: boolean
  error: string | null
  bot_name: string | null
  receive_mode: string
  last_event_at: string | null
  linked_count: number
  callback_url?: string
  receives?: boolean
}

export interface AdminWebhookStatus {
  id: string
  label: string
  configured: boolean
  signed: boolean
  last_ok_at: string | null
  error: string | null
}

export interface AdminChannels {
  channels: AdminChannelStatus[]
  webhooks: AdminWebhookStatus[]
  group_events: string[]
  group_event_kinds: string[]
}

export interface CalendarLinks {
  mine_url: string
  all_url: string
}

export interface HfFile {
  path: string
  files: string[]
  size_bytes: number
  parts: number
  kind: 'model' | 'mmproj'
}

export interface Backup {
  name: string
  kind: 'daily' | 'manual' | 'upgrade'
  size_bytes: number
  created_at: string
}
