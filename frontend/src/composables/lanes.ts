import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { Lane } from '@/api/types'

// Shared list of lanes (GPU groups). Most installations have exactly one; lane UI only appears with more.
const lanes = ref<Lane[]>([])
let loading: Promise<void> | null = null

export async function loadLanes(force = false) {
  if (!force && loading) return loading
  loading = api
    .lanes()
    .then((list) => {
      lanes.value = list
    })
    .catch(() => undefined)
  return loading
}

export function useLanes() {
  if (!loading) void loadLanes()
  const multi = computed(() => lanes.value.length > 1)
  const nameOf = (id: number | null | undefined) => lanes.value.find((l) => l.id === id)?.name ?? ''
  return { lanes, multi, nameOf, reload: () => loadLanes(true) }
}
