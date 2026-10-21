import type { Reservation } from '@/api/types'

type Translate = (key: string, params?: Record<string, unknown>) => string

/** What a booking is for: a model (and preset), the whole GPU, or a dev environment. */
export function reservationWhat(r: Reservation, t: Translate): string {
  if (r.devenv_project_id) return `${t('devenv.short')} · ${r.devenv_project_name ?? '?'}`
  if (r.devenv_template_id) return `${t('devenv.short')} · ${r.devenv_template_name ?? '?'}`
  if (r.exclusive) return t('schedule.kindExclusive')
  return r.preset_name ? `${r.model_name} · ${r.preset_name}` : (r.model_name ?? '')
}
