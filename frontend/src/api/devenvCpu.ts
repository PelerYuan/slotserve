// Dev environments without a GPU (backend: api/devenv_cpu.py)
import { request } from './client'
import type { DevSession } from './types'

export interface CpuLive {
  /** cores in use */
  cpu: number
  /** bytes */
  memory: number
  memory_limit: number | null
  at: string
}

export interface CpuSession {
  id: number
  username: string | null
  mine: boolean
  name: string
  project_id: number | null
  state: string
  started_at: string | null
  live: CpuLive | null
  node_id?: number | null
  node_name?: string | null
}

/** a machine CPU-only environments can run on (id null: the server's own) */
export interface CpuNode {
  id: number | null
  name: string
  online: boolean
  cpus: number | null
  memory: number | null
}

export interface CpuInfo {
  enabled: boolean
  per_user: number
  /** mine, starting or running */
  running: number
  /** per environment; 0 = the whole shared pool */
  cores: number
  /** per environment; 0 = no limit */
  memory_gb: number
  /** all CPU-only environments together; 0 = no limit */
  memory_pool_gb: number
  memory_claimed_gb: number
  cores_per_gpu: number
  host_cpus: number | null
  host_memory: number | null
  pool_cpus: number | null
  pool: string | null
  lanes: { lane_id: number; name: string; cpus: number; cpuset: string }[]
  /** my kept environments that continue without a GPU when a booking ends */
  after_booking: number[]
  /** mine; everyone's for admins */
  sessions: CpuSession[]
  nodes?: CpuNode[]
}

export const cpuApi = {
  info: () => request<CpuInfo>('GET', '/api/devenv/cpu'),
  start: (body: { template_id?: number; project_id?: number; node_id?: number | null }) =>
    request<DevSession>('POST', '/api/devenv/cpu/start', body),
  stop: (id: number) => request<{ ok: boolean }>('POST', `/api/devenv/cpu/sessions/${id}/stop`),
  afterBooking: (projectId: number, afterBooking: boolean) =>
    request<void>('PUT', `/api/devenv/cpu/projects/${projectId}`, { after_booking: afterBooking }),
}
