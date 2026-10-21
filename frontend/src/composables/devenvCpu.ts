// Dev environments without a GPU: limits and live use, shared by the components on the Dev environments page.
import { ref } from 'vue'
import { cpuApi, type CpuInfo, type CpuLive } from '@/api/devenvCpu'

export const cpuInfo = ref<CpuInfo | null>(null)

export async function loadCpu(): Promise<void> {
  cpuInfo.value = await cpuApi.info()
}

export function cpuLive(sessionId: number): CpuLive | null {
  return cpuInfo.value?.sessions.find((s) => s.id === sessionId)?.live ?? null
}

/** Whether I may start another environment without a GPU now (the server checks again). */
export function cpuCanStart(): boolean {
  const info = cpuInfo.value
  return !!info && info.enabled && info.running < info.per_user
}

export function formatCores(n: number): string {
  return n >= 10 ? n.toFixed(0) : n.toFixed(1)
}
