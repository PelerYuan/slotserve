import { request } from '@/api/client'
import type { AuditEntry, SettingValue } from '@/api/types'

export type Tier = 'basic' | 'advanced' | 'expert'

/** One setting with its registry metadata (backend services/app_settings.py, `GET /api/admin/settings`). */
export interface Setting {
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
  env_var: string
  group: string
  card: string | null
  tier: Tier
  order: number
  depends_on: string | null
  keywords: string[]
  scope: string
  danger: string | null
  restart_required: boolean
  has_impact: boolean
  has_value: boolean
  source: 'default' | 'env' | 'admin'
}

export interface SettingChange {
  key: string
  before: SettingValue | null
  after: SettingValue | null
  reset: boolean
  secret: boolean
  danger: string | null
}

export interface ImpactUser {
  user_id: number
  username: string
  over: boolean
  [extra: string]: unknown
}

export interface ImpactReservation {
  id: number
  user_id: number
  username: string
  lane_id: number | null
  start_at: string
  end_at: string
  status: string
  reason: string
}

export interface Impact {
  key: string
  text_key: string
  params: Record<string, unknown>
  users: ImpactUser[]
  reservations: ImpactReservation[]
}

export interface SettingsPreview {
  dry_run: true
  changes: SettingChange[]
  impacts: Impact[]
  sudo_required: boolean
  notify_user_ids: number[]
}

export interface SettingHistoryEntry {
  audit_id: number
  action: string
  actor_name: string
  created_at: string
  before: SettingValue | null
  after: SettingValue | null
  reset: boolean
  undoable: boolean
}

export interface AuditQuery {
  limit?: number
  offset?: number
  actor_id?: number | null
  action?: string | null
  target?: string | null
  since?: string | null
  until?: string | null
}

export interface QuotaGrant {
  id: string
  user_id: number
  extra_hours: number
  starts_at: string
  until: string
  reason: string
  by: string
  created_at: string
  revoked_at: string | null
  revoked_by: string
  active: boolean
}

export interface UserPolicy {
  user_id: number
  username: string
  exempt: boolean
  quota_weekly_hours: number
  base_weekly_hours: number
  extra_hours: number
  quota_upcoming: number
  max_reservation_hours: number
  max_advance_days: number
  week_start: string
  booked_hours_this_week: number
  open_bookings: number
  grants: QuotaGrant[]
}

export interface SignInSession {
  id: number
  created_at: string
  last_seen_at: string
  expires_at: string
  ip: string | null
  user_agent: string | null
  current: boolean
}

type Changes = Record<string, SettingValue | null>

function auditParams(q: AuditQuery): Record<string, unknown> {
  return {
    limit: q.limit,
    offset: q.offset,
    actor_id: q.actor_id ?? undefined,
    action: q.action || undefined,
    target: q.target || undefined,
    since: q.since || undefined,
    until: q.until || undefined,
  }
}

export const settingsApi = {
  /** Editable settings; `deployment` adds the read-only deployment parameters (restart to apply). */
  list: (deployment = false) =>
    request<Setting[]>('GET', '/api/admin/settings', undefined, {
      include_deployment: deployment || undefined,
      mask_secrets: true,
    }),
  preview: (changes: Changes) => request<SettingsPreview>('PUT', '/api/admin/settings', changes, { dry_run: true }),
  save: (changes: Changes, notify = false) =>
    request<Setting[]>('PUT', '/api/admin/settings', changes, { notify: notify || undefined }),
  history: (key: string) => request<SettingHistoryEntry[]>('GET', `/api/admin/settings/${encodeURIComponent(key)}/history`),
  undoPreview: (auditId: number) =>
    request<SettingsPreview>('POST', `/api/admin/audit/${auditId}/undo`, undefined, { dry_run: true }),
  undo: (auditId: number, notify = false) =>
    request<Setting[]>('POST', `/api/admin/audit/${auditId}/undo`, undefined, { notify: notify || undefined }),

  audit: (q: AuditQuery) =>
    request<{ total: number; items: AuditEntry[] }>('GET', '/api/admin/audit', undefined, auditParams(q)),
  auditActions: () => request<string[]>('GET', '/api/admin/audit/actions'),
  auditCsvUrl: (q: AuditQuery) => {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(auditParams({ ...q, limit: undefined, offset: undefined })))
      if (v !== undefined && v !== null) qs.set(k, String(v))
    const s = qs.toString()
    return `/api/admin/audit.csv${s ? `?${s}` : ''}`
  },

  policy: (userId: number) => request<UserPolicy>('GET', `/api/admin/users/${userId}/policy`),
  addGrant: (userId: number, body: { extra_hours: number; until: string; reason?: string }) =>
    request<QuotaGrant>('POST', `/api/admin/users/${userId}/quota-grants`, body),
  revokeGrant: (userId: number, grantId: string) =>
    request<void>('DELETE', `/api/admin/users/${userId}/quota-grants/${encodeURIComponent(grantId)}`),

  sessions: () => request<SignInSession[]>('GET', '/api/auth/sessions'),
  endSession: (id: number) => request<void>('DELETE', `/api/auth/sessions/${id}`),
  endOtherSessions: () => request<{ revoked: number }>('POST', '/api/auth/sessions/revoke-others'),
}
