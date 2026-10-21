import type {
  AdminApiKey,
  AdminChannels,
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
  ChannelLink,
  ChatChannel,
  ChatChannels,
  Catalog,
  CatalogDeploy,
  CatalogDeployInput,
  CatalogEntry,
  CatalogEntryInput,
  ClusterNode,
  ClusterNodeToken,
  ServerLink,
  DevInfo,
  DevProject,
  DevSession,
  DevTemplate,
  DevTemplateInput,
  DownloadTask,
  HfFile,
  Lane,
  LaneInput,
  LaneRuntime,
  LoadEvent,
  Model,
  NotifyPrefs,
  Preset,
  RequestLog,
  Reservation,
  SettingValue,
  SshKey,
  Status,
  TelegramInfo,
  Usage,
  UsageExport,
  User,
  WaitlistEntry,
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

// exported for api modules of their own (e.g. ./devenvPorts.ts)
export async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<T> {
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
  channels: () => request<ChatChannels>('GET', '/api/channels'),
  channelLink: (id: string) => request<ChannelLink>('POST', `/api/channels/${id}/link`),
  channelUnlink: (id: string) => request<ChatChannel>('DELETE', `/api/channels/${id}/link`),
  channelNotify: (id: string, notify: boolean) => request<ChatChannel>('PUT', `/api/channels/${id}`, { notify }),
  channelTest: (id: string) => request<void>('POST', `/api/channels/${id}/test`),
  calendar: () => request<CalendarLinks>('GET', '/api/calendar'),
  calendarReset: () => request<CalendarLinks>('POST', '/api/calendar/reset'),

  // config & status
  config: () => request<AppConfig>('GET', '/api/config'),
  status: () => request<Status>('GET', '/api/status'),
  logs: (lines = 300, laneId?: number | null) =>
    request<{ lines: string[] }>('GET', '/api/runtime/logs', undefined, { lines, lane_id: laneId ?? undefined }),
  restart: (laneId?: number | null) =>
    request<{ ok: boolean }>('POST', '/api/runtime/restart', undefined, { lane_id: laneId ?? undefined }),
  lanes: () => request<Lane[]>('GET', '/api/lanes'),

  // keys
  keys: () => request<ApiKey[]>('GET', '/api/keys'),
  createKey: (name: string) => request<ApiKeyCreated>('POST', '/api/keys', { name }),
  revokeKey: (id: number) => request<void>('DELETE', `/api/keys/${id}`),
  revealKey: (id: number) => request<{ key: string }>('GET', `/api/keys/${id}/reveal`),

  // models
  models: () => request<Model[]>('GET', '/api/models'),
  scanModels: () => request<{ added: string[]; skipped: number }>('POST', '/api/models/scan'),
  updateModel: (
    id: number,
    patch: Partial<Pick<Model, 'name' | 'args' | 'description' | 'engine'>> & { path?: string },
  ) => request<Model>('PATCH', `/api/models/${id}`, patch),
  createModel: (body: { name: string; engine: string; path: string; args: string; description: string }) =>
    request<Model>('POST', '/api/models', body),
  setDefault: (id: number, laneId?: number | null) =>
    request<Model>('POST', `/api/models/${id}/default`, undefined, { lane_id: laneId ?? undefined }),
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
  createDownload: (url: string, filename?: string, nodeId?: number | null) =>
    request<DownloadTask>('POST', '/api/downloads', { url, filename: filename || null, node_id: nodeId ?? null }),
  retryDownload: (id: number) => request<DownloadTask>('POST', `/api/downloads/${id}/retry`),
  removeDownload: (id: number) => request<void>('DELETE', `/api/downloads/${id}`),
  hfFiles: (repo: string, revision = 'main') =>
    request<{ repo: string; revision: string; files: HfFile[] }>('GET', '/api/downloads/hf/files', undefined, {
      repo,
      revision,
    }),
  hfDownload: (repo: string, files: string[], revision = 'main', nodeId?: number | null) =>
    request<DownloadTask[]>('POST', '/api/downloads/hf', { repo, revision, files, node_id: nodeId ?? null }),

  // model catalog
  catalog: {
    list: (ctx?: number) => request<Catalog>('GET', '/api/catalog', undefined, { ctx }),
    entry: (id: string, ctx?: number) =>
      request<CatalogEntry>('GET', `/api/catalog/${encodeURIComponent(id)}`, undefined, { ctx }),
    deploys: () => request<CatalogDeploy[]>('GET', '/api/catalog/deploys'),
    deploy: (body: CatalogDeployInput) => request<CatalogDeploy>('POST', '/api/catalog/deploy', body),
    dismiss: (id: number) => request<void>('DELETE', `/api/catalog/deploys/${id}`),
    setHidden: (id: string, hidden: boolean) =>
      request<void>('PUT', `/api/catalog/${encodeURIComponent(id)}/hidden`, { hidden }),
    createEntry: (body: CatalogEntryInput) => request<CatalogEntry>('POST', '/api/catalog/entries', body),
    updateEntry: (id: string, body: CatalogEntryInput) =>
      request<CatalogEntry>('PUT', `/api/catalog/entries/${encodeURIComponent(id)}`, body),
    deleteEntry: (id: string) => request<void>('DELETE', `/api/catalog/entries/${encodeURIComponent(id)}`),
  },

  // reservations
  reservations: (start: Date, end: Date, mine = false, laneId?: number | null) =>
    request<Reservation[]>('GET', '/api/reservations', undefined, {
      start: start.toISOString(),
      end: end.toISOString(),
      mine: mine || undefined,
      lane_id: laneId ?? undefined,
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
  usage: (days: number, scope: 'all' | 'mine' = 'all') =>
    request<Usage>('GET', '/api/usage', undefined, { days, scope, tz_offset: -new Date().getTimezoneOffset() }),
  usageExportUrl: (days: number, kind: UsageExport, scope: 'all' | 'mine' = 'all') =>
    `/api/usage/export?days=${days}&kind=${kind}&scope=${scope}&tz_offset=${-new Date().getTimezoneOffset()}`,

  // dev environments
  devenv: {
    info: () => request<DevInfo>('GET', '/api/devenv'),
    templates: () => request<DevTemplate[]>('GET', '/api/devenv/templates'),
    createTemplate: (body: DevTemplateInput) => request<DevTemplate>('POST', '/api/devenv/templates', body),
    updateTemplate: (id: number, body: Partial<DevTemplateInput>) =>
      request<DevTemplate>('PATCH', `/api/devenv/templates/${id}`, body),
    deleteTemplate: (id: number) => request<void>('DELETE', `/api/devenv/templates/${id}`),
    sessions: (everyone = false) =>
      request<DevSession[]>('GET', '/api/devenv/sessions', undefined, { everyone: everyone || undefined }),
    restart: (id: number) => request<{ ok: boolean }>('POST', `/api/devenv/sessions/${id}/restart`),
    save: (id: number, name: string) => request<{ ok: boolean }>('POST', `/api/devenv/sessions/${id}/save`, { name }),
    logs: (id: number) => request<{ text: string }>('GET', `/api/devenv/sessions/${id}/logs`),
    projects: (everyone = false) =>
      request<DevProject[]>('GET', '/api/devenv/projects', undefined, { everyone: everyone || undefined }),
    createProject: (body: { name: string; template_id: number; persist_paths?: string[] }) =>
      request<DevProject>('POST', '/api/devenv/projects', body),
    updateProject: (id: number, body: { name?: string; persist_paths?: string[] }) =>
      request<DevProject>('PATCH', `/api/devenv/projects/${id}`, body),
    deleteProject: (id: number, deleteData = false) =>
      request<void>('DELETE', `/api/devenv/projects/${id}`, undefined, { delete_data: deleteData || undefined }),
    password: () => request<{ set: boolean; login_enabled: boolean }>('GET', '/api/devenv/password'),
    setPassword: (password: string) => request<void>('PUT', '/api/devenv/password', { password }),
    clearPassword: () => request<void>('DELETE', '/api/devenv/password'),
  },
  sshKeys: () => request<SshKey[]>('GET', '/api/ssh-keys'),
  addSshKey: (name: string, public_key: string) => request<SshKey>('POST', '/api/ssh-keys', { name, public_key }),
  deleteSshKey: (id: number) => request<void>('DELETE', `/api/ssh-keys/${id}`),

  // admin
  admin: {
    stats: () => request<AdminStats>('GET', '/api/admin/stats'),
    runtime: () => request<LaneRuntime[]>('GET', '/api/admin/runtime'),
    loadEvents: (q: { laneId?: number | null; failed?: boolean; limit?: number }) =>
      request<LoadEvent[]>('GET', '/api/admin/runtime/events', undefined, {
        lane_id: q.laneId ?? undefined,
        failed: q.failed || undefined,
        limit: q.limit,
      }),
    runtimeStreamUrl: (laneId: number) => `/api/admin/runtime/${laneId}/stream`,
    runtimeLogUrl: (laneId: number) => `/api/admin/runtime/${laneId}/log`,
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
    channels: () => request<AdminChannels>('GET', '/api/admin/channels'),
    webhookTest: (id: string) =>
      request<{ error: string | null }>('POST', `/api/admin/channels/webhooks/${id}/test`),
    announce: (text: string) => request<{ targets: string[] }>('POST', '/api/admin/channels/announce', { text }),
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
    createLane: (body: LaneInput) => request<Lane>('POST', '/api/admin/lanes', body),
    updateLane: (id: number, body: LaneInput) => request<Lane>('PATCH', `/api/admin/lanes/${id}`, body),
    deleteLane: (id: number) => request<void>('DELETE', `/api/admin/lanes/${id}`),
    nodes: () => request<ClusterNode[]>('GET', '/api/admin/nodes'),
    createNode: (body: { name: string; address?: string }) =>
      request<ClusterNodeToken>('POST', '/api/admin/nodes', body),
    updateNode: (id: number, body: { name?: string; address?: string }) =>
      request<ClusterNode>('PATCH', `/api/admin/nodes/${id}`, body),
    rotateNodeToken: (id: number) => request<ClusterNodeToken>('POST', `/api/admin/nodes/${id}/token`),
    revokeNode: (id: number) => request<ClusterNode>('POST', `/api/admin/nodes/${id}/revoke`),
    deleteNode: (id: number) => request<void>('DELETE', `/api/admin/nodes/${id}`),
    serverLink: () => request<ServerLink>('GET', '/api/admin/nodes/server'),
    scanNode: (id: number) =>
      request<{ added: string[]; skipped: number }>('POST', '/api/models/scan', undefined, { node_id: id }),
    manual: (body: {
      action: 'load' | 'unload' | 'auto'
      lane_id?: number | null
      model_id?: number
      preset_id?: number | null
      hours?: number | null
    }) =>
      request<void>('POST', '/api/admin/runtime/manual', body),
  },
}
