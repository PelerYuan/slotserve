import type { Runtime } from '@/api/types'

export interface LoadProgress {
  pct: number // overall, 0-99 while loading
  weights: number | null // weight loading as the server reports it
  stage: string | null
  elapsed: number
  remaining: number
  source: 'log' | 'estimate'
}

// Share of the overall bar that each stage reaches once it starts.
const STAGE_FLOOR: Record<string, number> = { context: 88, warmup: 94, ready: 99 }

/** Loading progress from the server's own log when it reports any, otherwise from the time estimate. */
export function loadProgress(rt: Runtime, now: number): LoadProgress | null {
  if (rt.state !== 'loading' || !rt.load_started_at) return null
  const elapsed = Math.max(0, (now - Date.parse(rt.load_started_at)) / 1000)
  const est = rt.est_load_seconds || 60
  const remaining = Math.max(0, est - elapsed)
  if (rt.log_percent !== null && rt.log_percent !== undefined) {
    const floor = rt.stage ? STAGE_FLOOR[rt.stage] : undefined
    const pct = floor ?? rt.log_percent * 0.85
    const weights = floor === undefined ? rt.log_percent : null
    return { pct: Math.min(99, pct), weights, stage: rt.stage ?? null, elapsed, remaining, source: 'log' }
  }
  return { pct: Math.min(99, (elapsed / est) * 100), weights: null, stage: rt.stage ?? null, elapsed, remaining, source: 'estimate' }
}
