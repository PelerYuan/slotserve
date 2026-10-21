import { ref } from 'vue'
import { sharingApi } from '@/api/sharing'

// How many machines the signed-in person owns ("My machines" appears in the navigation only then).
export const ownedMachines = ref(0)

export async function loadOwnedMachines() {
  ownedMachines.value = (await sharingApi.mine().catch(() => [])).length
}
