// Personal computers: owners share their machine on a schedule, pause sharing, wake it; contribution statistics.
import { request } from './client'

export interface SharingWindow {
  days: number[] // 0 = Monday
  start: string // HH:MM
  end: string // HH:MM, 24:00 allowed; an end before the start runs into the next day
}

export interface NodeSharing {
  id: number
  name: string
  is_local: boolean
  online: boolean
  platform: string | null
  owner_id: number | null
  owner_name: string | null
  /** null: always shared; []: never */
  sharing_schedule: SharingWindow[] | null
  shared_now: boolean
  sharing_paused: boolean
  sharing_paused_until: string | null
  sharing_stops_at: string | null
  sharing_next_change: string | null
  network: string | null
  mac_addresses: string
  wol_macs: string[]
  woken_at: string | null
  timezone: string
  grace_minutes: number
}

export interface SharingConflict {
  id: number
  username: string
  start_at: string
  end_at: string
}

export interface RunningLane {
  id: number
  name: string
  gpus: number[]
  state: string | null
  model_name: string | null
  reason: string | null
}

export interface RunningBooking {
  id: number
  user_id: number
  username: string
  start_at: string
  end_at: string
  lane_id: number | null
  active: boolean
  kind: 'model' | 'exclusive' | 'devenv'
  model_name: string | null
}

export interface RunningSession {
  id: number
  kind: string
  username: string | null
  lane_id: number | null
  state: string
  reservation_id: number | null
}

export interface NodeRunning {
  lanes: RunningLane[]
  bookings: RunningBooking[]
  sessions: RunningSession[]
}

export interface LaneBlock {
  start: string
  end: string
  reason: 'unshared' | 'paused'
}

export interface LaneAvailability {
  lane_id: number
  node_id: number | null
  owner_id: number | null
  blocks: LaneBlock[]
}

export interface ContributionRow {
  user_id: number
  username: string
  nodes: string[]
  online_hours: number
  shared_hours: number
  gpu_hours_shared: number
  gpu_hours_used_by_others: number
  gpu_hours_consumed: number
}

export interface NodeContribution {
  since: string
  online_hours: number
  shared_hours: number
  gpu_hours_shared: number
  gpu_hours_used_by_others: number
  gpu_hours_used_by_owner: number
}

export const sharingApi = {
  mine: () => request<NodeSharing[]>('GET', '/api/nodes/mine'),
  get: (id: number) => request<NodeSharing>('GET', `/api/nodes/${id}/sharing`),
  save: (id: number, body: { schedule?: SharingWindow[] | null; network?: string; mac_addresses?: string }) =>
    request<{ node: NodeSharing; conflicts: SharingConflict[] }>('PUT', `/api/nodes/${id}/sharing`, body),
  pause: (id: number, hours: number | null) =>
    request<{ node: NodeSharing; affected: number }>('POST', `/api/nodes/${id}/pause`, { hours }),
  resume: (id: number) => request<NodeSharing>('POST', `/api/nodes/${id}/resume`),
  running: (id: number) => request<NodeRunning>('GET', `/api/nodes/${id}/running`),
  stop: (id: number, body: { reservation_id?: number; lane_id?: number }) =>
    request<NodeRunning>('POST', `/api/nodes/${id}/stop`, body),
  wake: (id: number) => request<{ senders: string[] }>('POST', `/api/nodes/${id}/wake`),
  contribution: (id: number, days = 30) =>
    request<NodeContribution>('GET', `/api/nodes/${id}/contribution`, undefined, { days }),
  unavailable: (start: Date, end: Date) =>
    request<LaneAvailability[]>('GET', '/api/lanes/unavailable', undefined, {
      start: start.toISOString(),
      end: end.toISOString(),
    }),
  usage: (days: number) =>
    request<{ since: string; rows: ContributionRow[] }>('GET', '/api/usage/contribution', undefined, { days }),
  usageExportUrl: (days: number) => `/api/usage/contribution/export?days=${days}`,
}

/** Weekdays 23:00-09:00 and the whole weekend. */
export const NIGHTS_AND_WEEKENDS: SharingWindow[] = [
  { days: [0, 1, 2, 3, 4], start: '23:00', end: '09:00' },
  { days: [5, 6], start: '00:00', end: '24:00' },
]
