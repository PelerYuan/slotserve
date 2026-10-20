import type {
  AdminApiKey,
  AdminSetting,
  AdminStats,
  AdminUser,
  ApiKey,
  ApiKeyCreated,
  AppConfig,
  AuditEntry,
  Backup,
  BookingInput,
  CalendarLinks,
  HfFile,
  NotifyPrefs,
  Preset,
  RequestLog,
  TelegramInfo,
  WaitlistEntry,
  DownloadTask,
  SettingValue,
  Model,
  Reservation,
  Status,
  Usage,
  User,
} from './types'

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public params: Record<string, unknown> = {},
  ) {
    super(code)
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<T> {
  let url = path
  if (query) {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) qs.set(k, String(v))
    const s = qs.toString()
    if (s) url += `?${s}`
  }
  let res: Response
  try {
    res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'network_error')
  }
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = data?.detail
    let code = 'unknown_error'
    let params: Record<string, unknown> = {}
    if (detail && typeof detail === 'object' && 'code' in detail) {
      code = detail.code
      params = detail.params ?? {}
    } else if (Array.isArray(detail)) {
      code = 'validation_error'
      params = { field: detail[0]?.loc?.slice(-1)[0] ?? '' }
    }
    if (res.status === 401 && code === 'not_authenticated') onUnauthorized?.()
    throw new ApiError(res.status, code, params)
  }
  return data as T
}

export const api = {
  // auth
  me: () => request<User>('GET', '/api/auth/me'),
  login: (username: string, password: string) => request<User>('POST', '/api/auth/login', { username, password }),
  register: (username: string, password: string, invite_code: string) =>
    request<User>('POST', '/api/auth/register', { username, password, invite_code }),
  logout: () => request<void>('POST', '/api/auth/logout'),
  changePassword: (old_password: string, new_password: string) =>
    request<void>('PUT', '/api/auth/password', { old_password, new_password }),
  preferences: (body: { locale?: string; notify_prefs?: NotifyPrefs }) =>
    request<User>('PUT', '/api/auth/preferences', body),

  // notifications & calendar
  telegram: () => request<TelegramInfo>('GET', '/api/telegram'),
  telegramLink: () => request<TelegramInfo>('POST', '/api/telegram/link'),
  telegramUnlink: () => request<TelegramInfo>('DELETE', '/api/telegram/link'),
  telegramTest: () => request<void>('POST', '/api/telegram/test'),
  calendar: () => request<CalendarLinks>('GET', '/api/calendar'),
  calendarReset: () => request<CalendarLinks>('POST', '/api/calendar/reset'),

  // config & status
  config: () => request<AppConfig>('GET', '/api/config'),
  status: () => request<Status>('GET', '/api/status'),
  logs: (lines = 300) => request<{ lines: string[] }>('GET', '/api/runtime/logs', undefined, { lines }),
  restart: () => request<{ ok: boolean }>('POST', '/api/runtime/restart'),

  // keys
  keys: () => request<ApiKey[]>('GET', '/api/keys'),
  createKey: (name: string) => request<ApiKeyCreated>('POST', '/api/keys', { name }),
  revokeKey: (id: number) => request<void>('DELETE', `/api/keys/${id}`),

  // models
  models: () => request<Model[]>('GET', '/api/models'),
  scanModels: () => request<{ added: string[]; skipped: number }>('POST', '/api/models/scan'),
  updateModel: (id: number, patch: Partial<Pick<Model, 'name' | 'args' | 'description'>>) =>
    request<Model>('PATCH', `/api/models/${id}`, patch),
  setDefault: (id: number) => request<Model>('POST', `/api/models/${id}/default`),
  clearDefault: (id: number) => request<void>('DELETE', `/api/models/${id}/default`),
  deleteModel: (id: number, deleteFile: boolean) =>
    request<void>('DELETE', `/api/models/${id}`, undefined, { delete_file: deleteFile }),
  createPreset: (modelId: number, name: string, args: string) =>
    request<Preset>('POST', `/api/models/${modelId}/presets`, { name, args }),
  updatePreset: (modelId: number, id: number, name: string, args: string) =>
    request<Preset>('PATCH', `/api/models/${modelId}/presets/${id}`, { name, args }),
  deletePreset: (modelId: number, id: number) => request<void>('DELETE', `/api/models/${modelId}/presets/${id}`),

  // downloads
  downloads: () => request<DownloadTask[]>('GET', '/api/downloads'),
  createDownload: (url: string, filename?: string) =>
    request<DownloadTask>('POST', '/api/downloads', { url, filename: filename || null }),
  retryDownload: (id: number) => request<DownloadTask>('POST', `/api/downloads/${id}/retry`),
  removeDownload: (id: number) => request<void>('DELETE', `/api/downloads/${id}`),
  hfFiles: (repo: string, revision = 'main') =>
    request<{ repo: string; revision: string; files: HfFile[] }>('GET', '/api/downloads/hf/files', undefined, {
      repo,
      revision,
    }),
  hfDownload: (repo: string, files: string[], revision = 'main') =>
    request<DownloadTask[]>('POST', '/api/downloads/hf', { repo, revision, files }),

  // reservations
  reservations: (start: Date, end: Date, mine = false) =>
    request<Reservation[]>('GET', '/api/reservations', undefined, {
      start: start.toISOString(),
      end: end.toISOString(),
      mine: mine || undefined,
    }),
  createReservation: (body: BookingInput) => request<Reservation[]>('POST', '/api/reservations', body),
  updateReservation: (id: number, body: Partial<BookingInput>) =>
    request<Reservation>('PATCH', `/api/reservations/${id}`, body),
  extendReservation: (id: number, minutes: number) =>
    request<Reservation>('POST', `/api/reservations/${id}/extend`, { minutes }),
  endReservation: (id: number, series = false) =>
    request<Reservation[]>('DELETE', `/api/reservations/${id}`, undefined, { series: series || undefined }),

  // waitlist
  waitlist: (everyone = false) =>
    request<WaitlistEntry[]>('GET', '/api/waitlist', undefined, { everyone: everyone || undefined }),
  joinWaitlist: (body: BookingInput & { auto_book: boolean }) =>
    request<WaitlistEntry>('POST', '/api/waitlist', body),
  leaveWaitlist: (id: number) => request<void>('DELETE', `/api/waitlist/${id}`),

  // usage
  usage: (days: number) =>
    request<Usage>('GET', '/api/usage', undefined, { days, tz_offset: -new Date().getTimezoneOffset() }),
  usageExportUrl: (days: number, kind: 'summary' | 'requests') =>
    `/api/usage/export?days=${days}&kind=${kind}&tz_offset=${-new Date().getTimezoneOffset()}`,

  // admin
  admin: {
    stats: () => request<AdminStats>('GET', '/api/admin/stats'),
    users: () => request<AdminUser[]>('GET', '/api/admin/users'),
    createUser: (username: string, password: string, is_admin: boolean) =>
      request<AdminUser>('POST', '/api/admin/users', { username, password, is_admin }),
    updateUser: (id: number, patch: { is_admin?: boolean; disabled?: boolean }) =>
      request<AdminUser>('PATCH', `/api/admin/users/${id}`, patch),
    resetPassword: (id: number, password: string) =>
      request<void>('POST', `/api/admin/users/${id}/password`, { password }),
    deleteUser: (id: number) => request<void>('DELETE', `/api/admin/users/${id}`),
    keys: () => request<AdminApiKey[]>('GET', '/api/admin/keys'),
    revokeKey: (id: number) => request<void>('DELETE', `/api/admin/keys/${id}`),
    reservations: (phase?: string, user_id?: number) =>
      request<Reservation[]>('GET', '/api/admin/reservations', undefined, { phase, user_id }),
    settings: () => request<AdminSetting[]>('GET', '/api/admin/settings'),
    saveSettings: (changes: Record<string, SettingValue | null>) =>
      request<AdminSetting[]>('PUT', '/api/admin/settings', changes),
    audit: (limit: number, offset: number) =>
      request<{ total: number; items: AuditEntry[] }>('GET', '/api/admin/audit', undefined, { limit, offset }),
    requests: (limit: number, offset: number, errorsOnly = false) =>
      request<{ total: number; items: RequestLog[] }>('GET', '/api/admin/requests', undefined, {
        limit,
        offset,
        errors_only: errorsOnly || undefined,
      }),
    approve: (id: number, series = false) =>
      request<Reservation[]>('POST', `/api/admin/reservations/${id}/approve`, { series }),
    reject: (id: number, note: string, series = false) =>
      request<Reservation[]>('POST', `/api/admin/reservations/${id}/reject`, { note, series }),
    backups: () => request<Backup[]>('GET', '/api/admin/backups'),
    createBackup: () => request<Backup>('POST', '/api/admin/backups'),
    backupUrl: (name: string) => `/api/admin/backups/${encodeURIComponent(name)}`,
    manual: (body: { action: 'load' | 'unload' | 'auto'; model_id?: number; preset_id?: number | null; hours?: number | null }) =>
      request<void>('POST', '/api/admin/runtime/manual', body),
  },
}
