<script setup lang="ts">
// "You get": one line of what an environment would get, from POST /api/devenv/preview (nothing is started).
// Used in QuickBook's #summary slot and under the start card. Admins can open the container settings.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Sparkles } from 'lucide-vue-next'
import { ApiError } from '@/api/client'
import { session } from '@/composables/session'
import { formatBytes } from '@/utils/format'
import { gpuName } from '@/utils/naming'
import { devenvApi, type Preview } from '../api'

const props = defineProps<{
  templateId?: number | null
  projectId?: number | null
  gpu: boolean
  laneId?: number | null
}>()

const { t, te } = useI18n()
const isAdmin = computed(() => !!session.user?.is_admin)
const data = ref<Preview | null>(null)
const failed = ref<string | null>(null)

let seq = 0
let timer: number | undefined
watch(
  () => [props.templateId, props.projectId, props.gpu, props.laneId] as const,
  () => {
    window.clearTimeout(timer)
    timer = window.setTimeout(load, 250)
  },
  { immediate: true },
)

async function load() {
  const mine = ++seq
  if (!props.templateId && !props.projectId) {
    data.value = null
    return
  }
  try {
    const out = await devenvApi.preview({
      template_id: props.projectId ? null : props.templateId,
      project_id: props.projectId ?? null,
      gpu: props.gpu,
      lane_id: props.gpu ? (props.laneId ?? null) : null,
    })
    if (mine !== seq) return
    data.value = out
    failed.value = null
  } catch (e) {
    if (mine !== seq) return
    data.value = null
    failed.value = e instanceof ApiError && te(`errors.${e.code}`) ? t(`errors.${e.code}`, e.params) : null
  }
}

const webKinds = new Set(['vscode', 'jupyter', 'desktop'])
const parts = computed(() => {
  const p = data.value
  if (!p || !p.ok) return []
  const out: string[] = []
  if (p.gpus.length) out.push(gpuName(p.gpus.join(',')))
  else out.push(t('devenvUi.preview.noGpu'))
  if (p.cpus) out.push(t('devenvUi.preview.cores', { n: Math.round(p.cpus * 10) / 10 }))
  else if (isAdmin.value && p.cpuset) out.push(t('devenvUi.preview.cpuset', { cpus: p.cpuset }))
  if (p.memory) out.push(t('devenvUi.preview.memory', { size: formatBytes(p.memory) }))
  if (p.mounts.some((m) => m.target === '/workspace')) out.push('/workspace')
  for (const w of p.web) out.push(webKinds.has(w.kind) ? t(`devenv.web.kind.${w.kind}`) : w.name)
  if (p.ssh.keys || p.ssh.password) out.push(t('devenvUi.preview.ssh'))
  return out
})
const refusal = computed(() => {
  const p = data.value
  if (!p || p.ok) return null
  const code = p.error.code
  return te(`errors.${code}`) ? t(`errors.${code}`, p.error.params) : t('errors.unknown_error')
})
const body = computed(() => (data.value && data.value.ok ? JSON.stringify(data.value.body, null, 2) : ''))
</script>

<template>
  <div v-if="parts.length || refusal || failed" class="preview small" data-test="devenv-preview">
    <template v-if="parts.length">
      <Sparkles :size="13" class="icon" />
      <span class="you">{{ t('devenvUi.preview.youGet') }}</span>
      <span class="text">{{ parts.join(' · ') }}</span>
      <span v-if="data && data.ok && data.compat" class="badge warn">{{ t('devenvUi.preview.compat') }}</span>
    </template>
    <span v-else class="refused">{{ refusal ?? failed }}</span>
    <details v-if="isAdmin && body" class="spec">
      <summary class="faint">{{ t('devenvUi.preview.spec') }}</summary>
      <pre class="mono">{{ body }}</pre>
    </details>
  </div>
</template>

<style scoped>
.preview {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 6px;
  color: var(--text-2);
  min-width: 0;
}
.icon {
  color: var(--brand);
  align-self: center;
}
.you {
  font-weight: 600;
}
.text {
  min-width: 0;
  overflow-wrap: anywhere;
}
.refused {
  color: var(--warn-text);
}
.spec {
  flex-basis: 100%;
}
.spec summary {
  cursor: pointer;
}
.spec pre {
  margin: 6px 0 0;
  max-height: 260px;
  overflow: auto;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--code-bg);
  color: var(--code-text);
  font-size: 11.5px;
}
</style>
