import type { CheckLevel, HealthCheck } from './api'

/** Setup checklist items (F06) that everyone needs; the other onboarding checks are optional. */
export const REQUIRED_SETUP = new Set(['engine', 'gpu', 'models', 'invite'])

export const LEVEL_ORDER: Record<CheckLevel, number> = { error: 0, warn: 1, info: 2, ok: 3 }

export const isDone = (c: HealthCheck) => c.level === 'ok' || !!c.dismissed

/** Problems first; dismissed ones count as handled. */
export function byUrgency(a: HealthCheck, b: HealthCheck): number {
  const rank = (c: HealthCheck) => (c.dismissed ? 4 : LEVEL_ORDER[c.level])
  return rank(a) - rank(b)
}
