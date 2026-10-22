import { useI18n } from 'vue-i18n'
import { formatDateTime, formatTime } from '@/utils/format'
import type { Reason } from './api'

const TIMES = new Set(['until', 'start_at', 'end_at'])

function sameDay(iso: string): boolean {
  return new Date(iso).toDateString() === new Date().toDateString()
}

/** Text of a placement reason: QuickBook's own wording, else the booking error it stands for. */
export function useReasonText() {
  const { t, te } = useI18n()
  return (r: Reason): string => {
    const params: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(r.params ?? {})) {
      params[k] = TIMES.has(k) && typeof v === 'string' ? (sameDay(v) ? formatTime(v) : formatDateTime(v)) : v
    }
    if (typeof params.hours === 'number') params.hours = Math.round(params.hours * 10) / 10
    if (te(`quickbook.reasons.${r.code}`)) return t(`quickbook.reasons.${r.code}`, params)
    if (te(`errors.${r.code}`)) return t(`errors.${r.code}`, params)
    return t('errors.unknown_error')
  }
}
