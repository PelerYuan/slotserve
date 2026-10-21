// Storage managed in Admin -> Storage (backend: api/storage.py).
import { request } from './client'

export type StorageKind = 'host' | 'nfs' | 'smb'
export type StoragePurpose = 'shared' | 'personal' | 'projects' | 'models'
export type StorageAccess = 'everyone' | 'users' | 'admins'

export interface StorageCheck {
  ok: boolean
  error: string | null
  total: number | null
  used: number | null
  free: number | null
  writable: boolean | null
}

export interface Storage {
  id: number
  name: string
  kind: StorageKind
  purpose: StoragePurpose
  source: string
  host_path: string | null
  server: string | null
  remote_path: string | null
  options: string | null
  username: string | null
  has_password: boolean
  mount_path: string | null
  container_path: string | null
  read_only: boolean
  access: StorageAccess
  user_ids: number[]
  usernames: string[]
  local_path: string | null
  quota_gb: number | null
  enabled: boolean
  /** host: other machines where the same directory exists */
  node_ids: number[]
  status: StorageCheck | null
  checked_at: string | null
  usage_total: number | null
  usage_people: number
  over_quota: number
  created_at: string
}

export interface StorageInput {
  name: string
  kind: StorageKind
  purpose: StoragePurpose
  host_path?: string | null
  server?: string | null
  remote_path?: string | null
  options?: string | null
  username?: string | null
  password?: string | null // undefined/null keeps the stored one, '' removes it
  mount_path?: string | null
  read_only: boolean
  access: StorageAccess
  user_ids: number[]
  local_path?: string | null
  quota_gb?: number | null
  enabled: boolean
  node_ids?: number[]
}

export interface StorageUsage {
  user_id: number
  username: string
  bytes: number
  over: boolean
}

export interface MyStorage {
  id: number
  name: string
  purpose: StoragePurpose
  path: string | null
  read_only: boolean
  ok: boolean | null
  used: number | null
  quota_gb: number | null
  over: boolean
}

export const storageApi = {
  list: () => request<Storage[]>('GET', '/api/admin/storage'),
  create: (body: StorageInput) => request<Storage>('POST', '/api/admin/storage', body),
  update: (id: number, body: StorageInput) => request<Storage>('PUT', `/api/admin/storage/${id}`, body),
  remove: (id: number) => request<void>('DELETE', `/api/admin/storage/${id}`),
  check: (id: number) => request<Storage>('POST', `/api/admin/storage/${id}/check`),
  usage: (id: number) => request<StorageUsage[]>('GET', `/api/admin/storage/${id}/usage`),
  mine: () => request<MyStorage[]>('GET', '/api/storage'),
}
