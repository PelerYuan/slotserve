import { onBeforeUnmount, onMounted, reactive } from 'vue'
import { inboxApi, type InboxCategory, type UnreadCounts } from './api'

// The bell's badge: one shared poller of /api/me/notifications/unread while any layout shows it.
const POLL_MS = 10_000

export const inbox = reactive<{ unread: UnreadCounts; open: boolean; category: InboxCategory | null }>({
  unread: { total: 0, personal: 0, admin: 0 },
  open: false,
  category: null,
})

let users = 0
let timer: number | undefined

export async function refreshUnread() {
  try {
    inbox.unread = await inboxApi.unread()
  } catch {
    /* keep the last count */
  }
}

function tick() {
  timer = window.setTimeout(async () => {
    if (document.visibilityState === 'visible') await refreshUnread()
    if (users > 0) tick()
  }, POLL_MS)
}

function onVisible() {
  if (document.visibilityState === 'visible') void refreshUnread()
}

export function openInbox(category: InboxCategory | null = null) {
  inbox.category = category
  inbox.open = true
}

export function useInboxPoller() {
  onMounted(() => {
    users++
    if (users === 1) {
      void refreshUnread()
      tick()
      document.addEventListener('visibilitychange', onVisible)
    }
  })
  onBeforeUnmount(() => {
    users--
    if (users === 0) {
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  })
  return inbox
}
