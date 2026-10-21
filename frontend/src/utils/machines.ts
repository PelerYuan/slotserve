import type { Machine } from '@/api/machines'

/** MiB -> "24 GB" / "7.5 GB" */
export function gb(mb: number | null | undefined): string {
  if (mb == null) return '—'
  const v = mb / 1024
  return `${v >= 10 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)} GB`
}

export function cores(n: number | null | undefined): string {
  if (n == null) return '—'
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

export function rate(bps: number | null | undefined): string {
  if (bps == null) return '—'
  if (bps >= 1024 ** 2) return `${(bps / 1024 ** 2).toFixed(1)} MB/s`
  if (bps >= 1024) return `${(bps / 1024).toFixed(0)} KB/s`
  return `${bps.toFixed(0)} B/s`
}

export function pct(part: number | null | undefined, whole: number | null | undefined): number {
  if (part == null || !whole) return 0
  return Math.max(0, Math.min(100, (part / whole) * 100))
}

/** ok | warn | danger by how full something is */
export function level(percent: number | null | undefined): string {
  if (percent == null) return ''
  return percent >= 90 ? 'danger' : percent >= 70 ? 'warn' : 'ok'
}

/** Largest GPU (by memory) of a machine, for summaries. */
export function maxGpuMemory(m: Machine): number {
  return m.gpus.reduce((best, g) => Math.max(best, g.memory_total_mb), 0)
}

/** "2 × GPU name" groups for a compact GPU line. */
export function gpuSummary(m: Machine): string {
  const groups = new Map<string, number>()
  for (const g of m.gpus) groups.set(g.name, (groups.get(g.name) ?? 0) + 1)
  return [...groups.entries()].map(([name, n]) => (n > 1 ? `${n} × ${name}` : name)).join(' + ')
}

export const STATUS_CLASS: Record<Machine['status'], string> = {
  online: 'ok',
  offline: 'danger',
  never: '',
  mismatch: 'warn',
}
