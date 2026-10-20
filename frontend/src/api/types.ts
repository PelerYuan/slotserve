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
  series_id: string | null
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

export interface Status {
  now: string
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
  telegram_enabled: boolean
  version: string
}

export interface Usage {
  days: number
  users: {
    user_id: number
    username: string
    requests: number
    prompt_tokens: number
    completion_tokens: number
    reserved_hours: number
  }[]
  models: { model_name: string; requests: number; prompt_tokens: number; completion_tokens: number }[]
  daily: { date: string; username: string; requests: number; tokens: number }[]
}

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
  model_id: number | null
  preset_id?: number | null
  start_at: string
  end_at: string
  note?: string
  repeat_weeks?: number
  user_id?: number | null
}

export interface WaitlistEntry {
  id: number
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
