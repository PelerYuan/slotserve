import { reactive } from 'vue'
import { request } from '@/api/client'
import type { Reservation } from '@/api/types'
import { session } from '@/composables/session'

export type Goal = 'call' | 'env' | 'book'

/** GET /api/me/onboarding (backend api/status.py): kept per account, so it follows the person to other devices. */
export interface Onboarding {
  /** show "start here" (until its goals are done or it is dismissed; never for people who used SlotServe before) */
  start: boolean
  /** goals reached, from real activity: a successful model call, an environment that started, a booking */
  done: Record<Goal, boolean>
  /** one-time suggestions already shown */
  seen: string[]
}

/** One health check, as far as the setup checklist needs it (GET /api/health, W1-04). */
export interface HealthCheck {
  id: string
  level: 'ok' | 'info' | 'warn' | 'error'
  title: string
  onboarding: boolean
  dismissed: unknown | null
}

export interface PendingSignup {
  user_id?: number
  id?: number
  username?: string
}

export const onboardingApi = {
  get: () => request<Onboarding>('GET', '/api/me/onboarding'),
  update: (body: { dismiss_start?: boolean; seen?: string[] }) =>
    request<Onboarding>('POST', '/api/me/onboarding', body),
  // admins: what needs their action
  health: () => request<{ checks: HealthCheck[] }>('GET', '/api/health'),
  pendingBookings: () =>
    request<Reservation[]>('GET', '/api/admin/reservations', undefined, { phase: 'pending' }),
  pendingSignups: () => request<PendingSignup[]>('GET', '/api/admin/invites/pending'),
}

/** The signed-in person's onboarding state, shared by the Overview slot and the suggestions. */
export const onboarding = reactive<{ data: Onboarding | null; loading: Promise<void> | null; userId: number | null }>({
  data: null,
  loading: null,
  userId: null,
})

export function loadOnboarding(force = false): Promise<void> {
  const userId = session.user?.id ?? null
  if (onboarding.loading && !force && onboarding.userId === userId) return onboarding.loading
  if (onboarding.userId !== userId) onboarding.data = null
  onboarding.userId = userId
  onboarding.loading = onboardingApi
    .get()
    .then((data) => {
      onboarding.data = data
    })
    .catch(() => undefined)
  return onboarding.loading
}

export async function updateOnboarding(body: { dismiss_start?: boolean; seen?: string[] }) {
  try {
    onboarding.data = await onboardingApi.update(body)
  } catch {
    /* shown again next time at worst */
  }
}

/** Forget the state of the previous account (sign-out, another person signing in on this tab). */
export function resetOnboarding() {
  onboarding.data = null
  onboarding.loading = null
  onboarding.userId = null
}
