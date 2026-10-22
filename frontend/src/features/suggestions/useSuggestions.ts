import { computed, ref } from 'vue'
import { loadOnboarding, onboarding, updateOnboarding } from '@/features/onboarding/api'

// "You can also…" (F01): one suggestion service for the whole site. Each suggestion appears once (remembered per
// account on the server, so not again on another device), can be closed, and at most one is on screen at a time.

export type SuggestionId =
  | 'playground_first_reply' // after the first Playground answer: book a bigger model
  | 'key_created' // after the first key: try the browser dev environment
  | 'start_done' // the three start goals are reached: subscribe to the calendar

const current = ref<SuggestionId | null>(null)
const closed = new Set<string>() // closed in this tab before the server answered

/** Shows `id` if it was never shown and no other suggestion is on screen; returns whether it is shown now. */
export async function offerSuggestion(id: SuggestionId): Promise<boolean> {
  if (current.value === id) return true
  if (current.value || closed.has(id)) return false
  await loadOnboarding()
  if (!onboarding.data || onboarding.data.seen.includes(id) || current.value) return false
  current.value = id
  void updateOnboarding({ seen: [id] }) // shown once: it does not come back after a reload
  return true
}

export function closeSuggestion(id: SuggestionId) {
  closed.add(id)
  if (current.value === id) current.value = null
}

export function useSuggestion(id: SuggestionId) {
  return {
    visible: computed(() => current.value === id),
    offer: () => offerSuggestion(id),
    close: () => closeSuggestion(id),
  }
}

/** Sign-out: the next account starts clean. */
export function resetSuggestions() {
  current.value = null
  closed.clear()
}
