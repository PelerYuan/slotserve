// Dev environments: the requests of the environment page and card that api/client.ts does not have
// (backend: api/devenv.py, api/devenv_ports.py, gateway/devenv_web.py, api/reservations.py).
import { request } from '@/api/client'
import type { DevPortMode, DevPorts } from '@/api/devenvPorts'
import type { DevProject, DevSession, Reservation } from '@/api/types'

/** A session as the server sends it now (W1-01 added the per-environment host key and shared membership). */
export interface DevSessionX extends DevSession {
  /** this environment's own SSH host key (null until it first starts) */
  host_key_fingerprint?: string | null
  /** shared idle environments: whether I joined it (ssh_command is null until I do) */
  joined?: boolean | null
}

export interface DevProjectX extends DevProject {
  host_key_fingerprint?: string | null
}

export interface PreviewMount {
  target: string
  type: string
  source: string
  read_only: boolean
}

/** POST /api/devenv/preview: what an environment would get, without starting anything. */
export type Preview =
  | {
      ok: true
      kind: string
      image: string
      compat: boolean
      gpus: string[]
      cpuset: string | null
      cpus: number | null
      memory: number | null
      pids_limit: number | null
      shm: number | null
      mounts: PreviewMount[]
      web: { name: string; port: number; kind: string }[]
      ssh: { keys: number; password: boolean; accounts: number; needs_credentials: boolean }
      security: { no_new_privileges: boolean; cap_drop: string[]; log: unknown }
      files: string[]
      body: unknown
    }
  | { ok: false; error: { code: string; params: Record<string, unknown> } }

export interface PreviewIn {
  template_id?: number | null
  project_id?: number | null
  gpu?: boolean
  lane_id?: number | null
}

export interface WebOpenOut {
  /** one-time link (60 s) on the web listener, or the plain path without one */
  url: string
  as_admin: boolean
  expires_in: number | null
}

export const devenvApi = {
  sessions: (everyone = false) =>
    request<DevSessionX[]>('GET', '/api/devenv/sessions', undefined, { everyone: everyone || undefined }),
  preview: (body: PreviewIn) => request<Preview>('POST', '/api/devenv/preview', body),
  join: (laneId: number) => request<void>('POST', `/api/devenv/shared/${laneId}/join`),
  leave: (laneId: number) => request<void>('DELETE', `/api/devenv/shared/${laneId}/join`),
  webOpen: (sessionId: number, port: number, confirmAsAdmin = false) =>
    request<WebOpenOut>('POST', `/api/devenv/sessions/${sessionId}/web-open`, {
      port,
      confirm_as_admin: confirmAsAdmin,
    }),
  /** 400 devenv_port_unauthenticated for vscode/desktop/jupyter going direct without confirm_unauthenticated */
  putPort: (sessionId: number, port: number, body: { name?: string; mode: DevPortMode; confirm_unauthenticated?: boolean }) =>
    request<DevPorts>('PUT', `/api/devenv/sessions/${sessionId}/ports/${port}`, body),
  extend: (reservationId: number, minutes: number) =>
    request<Reservation>('POST', `/api/reservations/${reservationId}/extend`, { minutes }),
  /** ends an active booking now (cancels an upcoming one) */
  end: (reservationId: number) => request<Reservation[]>('DELETE', `/api/reservations/${reservationId}`),
}
