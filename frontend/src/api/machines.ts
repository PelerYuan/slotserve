// Machines: what each node has, how busy it is, what is free on it; "Pick for me" (backend: cluster/machines_api.py)
import { request } from './client'

export interface MachineDisk {
  label: 'models' | 'docker' | string
  path: string
  total_bytes: number
  free_bytes: number
}

export interface MachineGpu {
  index: number
  name: string
  vendor: string | null
  memory_total_mb: number
  /** null while the machine is offline */
  memory_used_mb: number | null
  utilization_pct: number | null
  temperature_c: number | null
  power_w: number | null
  lane_id: number | null
  lane_name: string | null
}

export interface MachineBooking {
  id: number
  lane_id: number
  start_at: string
  end_at: string
  username: string
  mine: boolean
  status: 'booked' | 'pending'
  kind: 'model' | 'exclusive' | 'devenv'
}

export interface MachineLane {
  id: number
  name: string
  gpus: number[]
  gpu_memory_mb: number | null
  gpu_memory_used_mb: number | null
  state: string
  model_name: string | null
  free_now: boolean
  holder: string | null
  holder_mine: boolean
  held_until: string | null
  next_start_at: string | null
  /** today's and tomorrow's */
  bookings: MachineBooking[]
}

export interface MachineCapacity {
  gpus: number
  gpus_free: number
  lanes: number
  lanes_free: number
  gpu_memory_total_mb: number
  gpu_memory_free_mb: number | null
  max_lane_gpu_memory_mb: number | null
  cpu_cores: number | null
  cpu_cores_lanes: number
  cpu_cores_envs: number
  /** left for work without a GPU */
  cpu_cores_free: number | null
  memory_total_mb: number | null
  memory_used_mb: number | null
  memory_reserved_mb: number
  memory_free_mb: number | null
  cpu_envs_running: number
  /** CPU-only dev environments can be started here */
  cpu_envs: boolean
}

export interface Machine {
  id: number
  name: string
  is_local: boolean
  status: 'online' | 'offline' | 'never' | 'mismatch'
  online: boolean
  last_seen_at: string | null
  kind: 'gpu' | 'cpu'
  hostname: string | null
  os: string | null
  arch: string | null
  cpu_model: string | null
  cpu_physical_cores: number | null
  cpu_logical_cores: number | null
  cpu_percent: number | null
  load: number | null
  memory_total_mb: number | null
  memory_used_mb: number | null
  net_rx_bps: number | null
  net_tx_bps: number | null
  disks: MachineDisk[]
  docker: { available: boolean; version: string | null; runtimes: string[]; gpu_runtime: boolean }
  gpu_driver: { vendor: string | null; driver_version: string | null; cuda_version: string | null } | null
  gpus: MachineGpu[]
  lanes: MachineLane[]
  capacity: MachineCapacity
  models: { id: number; name: string; size_bytes: number }[]
  busy: boolean
  /** set when owners share their machines on a schedule */
  owner?: string | null
  shared_hours?: string | null
  sharing_paused?: boolean
}

export interface MachineHistory {
  node_id: number
  interval_seconds: number
  points: {
    at: string
    cpu_percent: number | null
    memory_used_mb: number | null
    gpu_util_pct: number | null
    gpu_memory_used_mb: number | null
  }[]
  gpu_hours: { start: string; util_pct: number | null }[]
}

export type Purpose = 'booking' | 'cpu_env' | 'deploy' | 'any'

export interface Requirements {
  /** true: a GPU lane; false: CPU only; null: either */
  gpu?: boolean | null
  min_gpu_memory_mb?: number
  cores?: number
  memory_mb?: number
  model_id?: number | null
  purpose?: Purpose
  start_at?: string | null
  end_at?: string | null
  exclude_reservation_id?: number | null
}

export interface Candidate {
  node_id: number
  node_name: string
  lane_id: number | null
  lane_name: string | null
  score: number
  fits: boolean
  reasons: string[]
}

export interface Recommendation {
  best: Candidate | null
  machines: Candidate[]
  options: Candidate[]
}

const tz = () => -new Date().getTimezoneOffset()

export const machinesApi = {
  list: () => request<Machine[]>('GET', '/api/machines', undefined, { tz_offset: tz() }),
  get: (id: number) => request<Machine>('GET', `/api/machines/${id}`, undefined, { tz_offset: tz() }),
  history: (id: number) => request<MachineHistory>('GET', `/api/machines/${id}/history`),
  recommend: (need: Requirements) =>
    request<Recommendation>('POST', '/api/machines/recommend', { ...need, tz_offset: tz() }),
}
