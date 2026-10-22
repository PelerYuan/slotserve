<script setup lang="ts">
// One environment as a compact "My work" row (for the Overview, F01/F04): name, state, where, time left, and the
// one thing you most likely want: open its web interface, or copy its SSH command; details on the Dev
// environments page.
//
//   <WorkItem :session="s" />                     s: a session of GET /api/devenv/sessions (features/devenv/api.ts)
//   <WorkItem :session="s" title="thesis-train" />  title: the card title (default: kept name, else template name)
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { Copy, ExternalLink, SquareTerminal } from 'lucide-vue-next'
import { useNow } from '@/composables/useNow'
import type { DevSessionX } from './api'
import { useWebOpen } from './useWebOpen'

const props = defineProps<{ session: DevSessionX; title?: string }>()
const { t } = useI18n()
const web = useWebOpen()
const now = useNow(30_000)

const s = computed(() => props.session)
const name = computed(() => props.title ?? s.value.project_name ?? s.value.template_name ?? s.value.image)
const running = computed(() => s.value.state === 'running')
const where = computed(() => (s.value.kind === 'cpu' ? t('devenvUi.card.noGpu') : (s.value.lane_name ?? '')))
const left = computed(() => {
  if (s.value.kind !== 'booking' || !s.value.end_at || !s.value.start_at) return null
  if (new Date(s.value.start_at).getTime() > now.value) return null
  const m = Math.max(0, Math.floor((new Date(s.value.end_at).getTime() - now.value) / 60_000))
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`
})
const tone = computed(() =>
  s.value.state === 'running' ? 'ok' : s.value.state === 'failed' ? 'danger' : s.value.state === 'stopped' ? 'idle' : 'warn',
)
const firstWeb = computed(() => (running.value ? (s.value.web[0] ?? null) : null))

const copied = ref(false)
async function copySsh() {
  const text = s.value.ssh_command ?? ''
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
  copied.value = true
  window.setTimeout(() => (copied.value = false), 1500)
}
</script>

<template>
  <div class="work-item" :data-session="s.id" data-test="work-item">
    <span class="dot" :class="tone" />
    <div class="main">
      <div class="line">
        <RouterLink :to="{ name: 'devenv' }" class="strong name">{{ name }}</RouterLink>
        <span class="faint small">{{ t(`devenv.state.${s.state}`) }}<template v-if="where"> · {{ where }}</template></span>
      </div>
      <div v-if="left" class="small muted num">{{ t('devenvUi.time.left', { time: left }) }}</div>
    </div>
    <div class="act">
      <NButton v-if="firstWeb" size="small" type="primary" secondary @click="web.open(s.id, firstWeb.port)">
        <template #icon><ExternalLink :size="13" /></template>
        {{ firstWeb.kind === 'http' ? t('devenv.web.openNamed', { name: firstWeb.name }) : t(`devenv.web.open.${firstWeb.kind}`) }}
      </NButton>
      <NButton v-else-if="running && s.ssh_command" size="small" secondary :title="s.ssh_command" @click="copySsh">
        <template #icon><Copy v-if="!copied" :size="13" /><SquareTerminal v-else :size="13" /></template>
        {{ copied ? t('common.copied') : t('devenvUi.work.copySsh') }}
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.work-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  min-width: 0;
}
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--text-3);
}
.dot.ok {
  background: var(--ok);
}
.dot.warn {
  background: var(--warn);
}
.dot.danger {
  background: var(--danger);
}
.main {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 2px;
}
.line {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  min-width: 0;
}
.name {
  color: var(--text);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.name:hover {
  color: var(--brand);
}
.act {
  flex: none;
}
</style>
