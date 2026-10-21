// Ports of dev environments: added at runtime, opened through the SlotServe login or directly on a host port.
import { request } from './client'

export type DevPortMode = 'login' | 'direct'
/** login: behind the SlotServe login; active/starting/paused/off/error: a direct port's forwarder */
export type DevPortStatus = 'login' | 'active' | 'starting' | 'paused' | 'off' | 'error'

export interface DevPort {
  id: number | null
  name: string
  port: number
  kind: string
  mode: DevPortMode
  declared: boolean
  host_port: number | null
  url: string
  direct_url: string | null
  status: DevPortStatus
  error: string | null
  /** all: reachable; local: only on 127.0.0.1 inside the container; none: nothing listens; null: unknown */
  listening: 'all' | 'local' | 'none' | null
}

export interface DevPorts {
  enabled: boolean
  can_direct: boolean
  can_add: boolean
  limit: number
  used: number
  range: string
  ports: DevPort[]
  detected: number[]
}

export interface DevPublicPort {
  id: number
  host_port: number
  container_port: number
  name: string
  owner: string | null
  project_id: number | null
  project_name: string | null
  session_id: number | null
  environment: string
  status: DevPortStatus
  error: string | null
  url: string
  created_at: string
}

export const portsApi = {
  list: (sessionId: number) => request<DevPorts>('GET', `/api/devenv/sessions/${sessionId}/ports`),
  put: (sessionId: number, port: number, body: { name?: string; mode: DevPortMode }) =>
    request<DevPorts>('PUT', `/api/devenv/sessions/${sessionId}/ports/${port}`, body),
  remove: (sessionId: number, port: number) =>
    request<DevPorts>('DELETE', `/api/devenv/sessions/${sessionId}/ports/${port}`),
  publicPorts: (everyone = false) =>
    request<DevPublicPort[]>('GET', '/api/devenv/public-ports', undefined, { everyone: everyone || undefined }),
  release: (id: number) => request<void>('DELETE', `/api/devenv/public-ports/${id}`),
}

/** An address to copy: login URLs are relative to SlotServe. */
export function absolute(url: string): string {
  return url.startsWith('/') ? window.location.origin + url : url
}
