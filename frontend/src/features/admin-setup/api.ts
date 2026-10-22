import { request } from '@/api/client'

export type CheckLevel = 'ok' | 'info' | 'warn' | 'error'

/** What went wrong, in both UI languages (backend services/diagnostics). Show `text[locale]`. */
export interface Diagnosis {
  id: string
  recognized: boolean
  category: string
  doc: string | null
  cause_key: string
  step_keys: string[]
  params: Record<string, string>
  text: Record<string, { cause: string; steps: string[] }>
  evidence: string[]
  exit_code: number | null
  error: string | null
  log_tail: string[]
}

export interface CheckAction {
  kind: 'link' | 'setting' | 'retry' | 'docs'
  target: string
}

export interface HealthCheck {
  id: string
  group: string
  level: CheckLevel
  title_key: string
  title: string
  params: Record<string, unknown>
  evidence: string
  actions: CheckAction[]
  dismissible: boolean
  dismissed: { by: number | null; reason: string; at: string } | null
  diagnosis: Diagnosis | null
  onboarding: boolean
  doc_anchor: string
  since: string | null
  last_run: string | null
  alerted: boolean
}

export interface Health {
  status: 'ok' | 'degraded' | 'down'
  version: string
  problems: string[]
  checks: HealthCheck[]
}

export type InviteState = 'active' | 'expired' | 'used_up' | 'revoked'

export interface InviteUser {
  id: number
  username: string
  created_at: string
  approval_pending: boolean
}

export interface Invite {
  id: number
  prefix: string
  note: string
  created_by: string | null
  created_at: string
  expires_at: string
  max_uses: number
  uses: number
  requires_approval: boolean
  last_used_at: string | null
  revoked_at: string | null
  state: InviteState
  users: InviteUser[]
}

export interface InviteCreated extends Invite {
  token: string
  path: string
}

export interface PendingUser extends InviteUser {
  invite_id: number | null
  invite_prefix: string | null
  invite_note: string | null
}

export interface InviteCheck {
  valid: boolean
  code: string | null
  requires_approval: boolean
  expires_at: string | null
}

export const setupApi = {
  health: () => request<Health>('GET', '/api/health'),
  runAll: () => request<Health>('POST', '/api/health/run'),
  runCheck: (id: string) => request<HealthCheck>('POST', `/api/health/checks/${encodeURIComponent(id)}/run`),
  dismiss: (id: string, reason: string) =>
    request<HealthCheck>('POST', `/api/health/checks/${encodeURIComponent(id)}/dismiss`, { reason }),
  undismiss: (id: string) => request<HealthCheck>('DELETE', `/api/health/checks/${encodeURIComponent(id)}/dismiss`),

  invites: () => request<Invite[]>('GET', '/api/admin/invites'),
  createInvite: (body: { note?: string; days?: number; max_uses?: number; requires_approval?: boolean }) =>
    request<InviteCreated>('POST', '/api/admin/invites', body),
  revokeInvite: (id: number) => request<void>('DELETE', `/api/admin/invites/${id}`),
  pending: () => request<PendingUser[]>('GET', '/api/admin/invites/pending'),
  approve: (userId: number) => request<InviteUser>('POST', `/api/admin/invites/pending/${userId}/approve`),
  reject: (userId: number) => request<void>('POST', `/api/admin/invites/pending/${userId}/reject`),
  checkInvite: (token: string) => request<InviteCheck>('POST', '/api/invites/check', { token }),
}

const DOCS_BASE = 'https://peleryuan.github.io/slotserve/'

/** A docs reference of the backend ("operations#backups", "storage.md#types") as a link to the published docs. */
export function docsUrl(ref: string, locale: string): string {
  const [page, anchor] = ref.split('#')
  const name = page.replace(/\.md$/, '').replace(/^\/+/, '')
  const prefix = locale.startsWith('zh') ? 'zh/' : ''
  const path = name && name !== 'index' ? `${name}/` : ''
  return `${DOCS_BASE}${prefix}${path}${anchor ? `#${anchor}` : ''}`
}
