import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import type { Status } from '@/api/types'

const status = ref<Status | null>(null)
let subscribers = 0
let timer: number | undefined

async function poll() {
  try {
    status.value = await api.status()
  } catch {
    /* keep last value */
  }
}

function schedule() {
  const loading = status.value?.runtime.state === 'loading' || status.value?.runtime.state === 'stopping'
  timer = window.setTimeout(async () => {
    await poll()
    if (subscribers > 0) schedule()
  }, loading ? 2000 : 5000)
}

/** Shared, ref-counted poller for /api/status. */
export function useStatus() {
  onMounted(async () => {
    subscribers++
    if (subscribers === 1) {
      await poll()
      schedule()
    }
  })
  onBeforeUnmount(() => {
    subscribers--
    if (subscribers === 0) window.clearTimeout(timer)
  })
  return { status, refresh: poll }
}
