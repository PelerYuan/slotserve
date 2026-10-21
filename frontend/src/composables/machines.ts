// Every machine with hardware, load and what is free on it: shared by the Machines page and the pickers.
import { computed, ref } from 'vue'
import { machinesApi, type Machine } from '@/api/machines'

const machines = ref<Machine[]>([])
const loaded = ref(false)
let loading: Promise<void> | null = null

export async function loadMachines(force = false): Promise<void> {
  if (!force && loading) return loading
  loading = machinesApi
    .list()
    .then((list) => {
      machines.value = list
    })
    .catch(() => undefined)
    .finally(() => {
      loaded.value = true
    })
  return loading
}

export function useMachines() {
  if (!loading) void loadMachines()
  /** more than one machine: machine choices appear in dialogs */
  const multi = computed(() => machines.value.length > 1)
  const byId = (id: number | null | undefined) => machines.value.find((m) => m.id === id) ?? null
  const ofLane = (laneId: number | null | undefined) =>
    machines.value.find((m) => m.lanes.some((l) => l.id === laneId)) ?? null
  return { machines, loaded, multi, byId, ofLane, reload: () => loadMachines(true) }
}
