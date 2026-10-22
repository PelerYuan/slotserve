import { request } from '@/api/client'

export type InboxCategory = 'personal' | 'admin'

/** One message of the in-app inbox (W1-04, backend api/inbox.py); `message` is already in the person's language. */
export interface InboxItem {
  id: number
  event: string
  category: InboxCategory
  level: 'info' | 'success' | 'warn' | 'error'
  title_key: string
  params: Record<string, unknown>
  message: string
  /** a page of this web UI, e.g. /admin?tab=reservations */
  link: string | null
  interruption_id: number | null
  created_at: string
  read_at: string | null
}

export interface UnreadCounts {
  total: number
  personal: number
  admin: number
}

export interface InboxPage {
  items: InboxItem[]
  unread: UnreadCounts
  has_more: boolean
}

export const inboxApi = {
  list: (q: { unread?: boolean; category?: InboxCategory | null; beforeId?: number | null; limit?: number } = {}) =>
    request<InboxPage>('GET', '/api/me/notifications', undefined, {
      unread: q.unread || undefined,
      category: q.category ?? undefined,
      before_id: q.beforeId ?? undefined,
      limit: q.limit,
    }),
  unread: () => request<UnreadCounts>('GET', '/api/me/notifications/unread'),
  read: (body: { ids: number[] } | { all: true; category?: InboxCategory }) =>
    request<UnreadCounts>('POST', '/api/me/notifications/read', body),
}
