import { api } from '@/api/client'
import type { DevSession, Reservation } from '@/api/types'
import { session } from '@/composables/session'

/** One row of "My work": a dev environment and/or a booking that runs now or starts within 24 hours. */
export interface WorkRow {
  key: string
  env: DevSession | null
  booking: Reservation | null
  start: string | null
  end: string | null
  active: boolean
}

const DAY_MS = 24 * 3_600_000
const LIVE_ENV = new Set(['waiting', 'pulling', 'ready', 'starting', 'running', 'stopping'])

export async function loadWork(): Promise<WorkRow[]> {
  const now = new Date()
  const [bookings, envs] = await Promise.all([
    api.reservations(new Date(now.getTime() - DAY_MS), new Date(now.getTime() + DAY_MS), true).catch(() => []),
    session.config?.devenv_enabled ? api.devenv.sessions().catch(() => []) : Promise.resolve([] as DevSession[]),
  ])
  const mine = bookings.filter(
    (r) =>
      r.user_id === session.user?.id &&
      (r.phase === 'active' || r.phase === 'upcoming' || r.phase === 'pending') &&
      new Date(r.end_at) > now &&
      new Date(r.start_at).getTime() - now.getTime() < DAY_MS,
  )
  const rows: WorkRow[] = []
  const used = new Set<number>()
  for (const env of envs.filter((e) => e.mine && LIVE_ENV.has(e.state))) {
    const booking = env.reservation_id ? (mine.find((r) => r.id === env.reservation_id) ?? null) : null
    if (booking) used.add(booking.id)
    rows.push({
      key: `env-${env.id}`,
      env,
      booking,
      start: booking?.start_at ?? env.start_at,
      end: booking?.end_at ?? env.end_at,
      active: true,
    })
  }
  for (const r of mine) {
    if (used.has(r.id)) continue
    rows.push({ key: `booking-${r.id}`, env: null, booking: r, start: r.start_at, end: r.end_at, active: r.phase === 'active' })
  }
  return rows.sort((a, b) => Number(b.active) - Number(a.active) || (a.start ?? '').localeCompare(b.start ?? ''))
}
