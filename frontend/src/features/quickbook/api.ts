import { request } from '@/api/client'

export type BookingKind = 'model' | 'exclusive' | 'devenv'

/** POST /api/placement/fit: the fields mean what they mean in POST /api/reservations; `kind` says which apply. */
export interface FitRequest {
  kind: BookingKind
  model_id?: number | null
  preset_id?: number | null
  devenv_template_id?: number | null
  devenv_project_id?: number | null
  devenv_project_name?: string | null
  start_at: string
  end_at: string
  /** only judge this lane */
  lane_id?: number | null
  /** at least this many GPUs */
  gpu?: number | null
  /** judged only with several machines (W4) */
  gpu_mem_mb?: number
  cpu_cores?: number
  mem_mb?: number
  /** also look for the earliest window of the same length when nothing fits (default true) */
  suggest?: boolean
}

/** Why a place fits or not: an i18n code and its parameters (refusals use the booking's own error codes). */
export interface Reason {
  code: string
  params: Record<string, unknown>
}

/** One place a booking can go; on one machine a lane (`node_id` null). */
export interface Candidate {
  node_id: number | null
  lane_id: number | null
  lane_name: string | null
  gpus: number[] | null
  score: number
  fits: boolean
  reasons_ok: Reason[]
  reasons_no: Reason[]
  /** the next booking on this lane after the window (null: none ahead) */
  free_until: string | null
  needs_approval: boolean
  est_migration_bytes: number | null
  owner_id: number | null
  trust: string
  interruptions_7d: number
}

export interface FitResult {
  /** the window as it will be booked: book exactly this */
  start_at: string
  end_at: string
  /** the automatic choice; null when nothing fits */
  best: Candidate | null
  /** every lane, fitting ones first, best first */
  candidates: Candidate[]
  /** request-wide: quota_left, needs_approval */
  reasons_ok: Reason[]
  /** request-wide refusals (also part of every candidate's reasons_no) */
  reasons_no: Reason[]
  needs_approval: boolean
  /** when nothing fits: the earliest window of the same length that does */
  next_free: { start_at: string; end_at: string; candidate: Candidate } | null
}

export const placementApi = {
  fit: (body: FitRequest) => request<FitResult>('POST', '/api/placement/fit', body),
}
