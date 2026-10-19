import { i18n } from '@/i18n'

const locale = () => i18n.global.locale.value

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = bytes
  let i = 0
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  const zh = locale() === 'zh-CN'
  if (s < 60) return zh ? `${s} 秒` : `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return zh ? `${m} 分${rs ? ` ${rs} 秒` : '钟'}` : `${m}m${rs ? ` ${rs}s` : ''}`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return zh ? `${h} 小时${rm ? ` ${rm} 分` : ''}` : `${h}h${rm ? ` ${rm}m` : ''}`
}

export function formatNumber(n: number): string {
  return new Intl.NumberFormat(locale()).format(n)
}

export function formatCompact(n: number): string {
  return new Intl.NumberFormat(locale(), { notation: 'compact', maximumFractionDigits: 1 }).format(n)
}

export function formatTime(iso: string | Date): string {
  const d = typeof iso === 'string' ? new Date(iso) : iso
  return d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
}

export function formatDate(iso: string | Date): string {
  const d = typeof iso === 'string' ? new Date(iso) : iso
  return d.toLocaleDateString(locale(), { month: 'short', day: 'numeric', weekday: 'short' })
}

export function formatDateTime(iso: string | Date | null | undefined): string {
  if (!iso) return '—'
  const d = typeof iso === 'string' ? new Date(iso) : iso
  return d.toLocaleString(locale(), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
}

export function formatRange(start: string | Date, end: string | Date): string {
  const s = typeof start === 'string' ? new Date(start) : start
  const e = typeof end === 'string' ? new Date(end) : end
  const sameDay = s.toDateString() === e.toDateString()
  return sameDay
    ? `${formatDate(s)} ${formatTime(s)} – ${formatTime(e)}`
    : `${formatDateTime(s)} – ${formatDateTime(e)}`
}

export function relativeFromNow(iso: string | Date): string {
  const d = typeof iso === 'string' ? new Date(iso) : iso
  const diff = (d.getTime() - Date.now()) / 1000
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' })
  const abs = Math.abs(diff)
  if (abs < 60) return rtf.format(Math.round(diff), 'second')
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / 86400), 'day')
}
