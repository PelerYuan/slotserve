import type { CatalogEntry, CatalogVariant, FitStatus } from '@/api/types'

const RANK: Record<FitStatus, number> = { fits: 3, tight: 2, no: 1, unknown: 0 }

export const CONTEXT_OPTIONS = [2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144]

export function fitOn(v: CatalogVariant, laneId: number): FitStatus {
  return v.fits.find((f) => f.lane_id === laneId)?.status ?? 'unknown'
}

/** The best variant for a lane: the largest one that fits, else the one closest to fitting. */
export function bestFor(entry: CatalogEntry, laneId: number): { status: FitStatus; variant: CatalogVariant | null } {
  let best: CatalogVariant | null = null
  let status: FitStatus = 'unknown'
  for (const v of entry.variants) {
    const s = fitOn(v, laneId)
    const better =
      !best ||
      RANK[s] > RANK[status] ||
      (RANK[s] === RANK[status] && (s === 'fits' ? v.estimate.total > best.estimate.total : v.estimate.total < best.estimate.total))
    if (better) {
      best = v
      status = s
    }
  }
  return { status, variant: best }
}

export function fitClass(status: FitStatus): string {
  return { fits: 'ok', tight: 'warn', no: 'danger', unknown: '' }[status]
}

export function contextLabel(tokens: number): string {
  return tokens >= 1024 ? `${Math.round(tokens / 1024)}K` : String(tokens)
}

export function paramsLabel(entry: CatalogEntry): string {
  const p = (n: number) => (n >= 10 ? n.toFixed(0) : n < 1 ? n.toFixed(2).replace(/0$/, '') : n.toFixed(1).replace(/\.0$/, ''))
  return entry.active_params_b ? `${p(entry.params_b)}B · A${p(entry.active_params_b)}B` : `${p(entry.params_b)}B`
}

export function engineLabel(engine: string): string {
  return { 'llama.cpp': 'llama.cpp', vllm: 'vLLM', sglang: 'SGLang' }[engine] ?? engine
}

export function hfUrl(repo: string): string {
  return `https://huggingface.co/${repo}`
}

export function installed(entry: CatalogEntry): boolean {
  return entry.variants.some((v) => v.models.length > 0)
}
