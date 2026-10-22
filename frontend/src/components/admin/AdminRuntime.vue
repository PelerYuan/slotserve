<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NSwitch, useMessage } from 'naive-ui'
import { Copy, Download, Pause, Play, Trash2 } from 'lucide-vue-next'
import StatusDot from '@/components/StatusDot.vue'
import { api } from '@/api/client'
import type { LaneRuntime, LoadEvent } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useNow } from '@/composables/useNow'
import { formatDateTime, formatDuration } from '@/utils/format'
import { loadProgress } from '@/utils/progress'
import DiagnosisCard from '@/features/admin-setup/components/DiagnosisCard.vue'
import type { Diagnosis } from '@/features/admin-setup/api'

// failed lanes and failed loads carry the likely cause (services/diagnostics, F08)
type Diagnosed = { diagnosis?: Diagnosis | null }

const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const now = useNow(500)

const lanes = ref<(LaneRuntime & Diagnosed)[]>([])
const laneId = ref<number | null>(null)
const lane = computed(() => lanes.value.find((l) => l.lane_id === laneId.value) ?? lanes.value[0] ?? null)
const progress = computed(() => (lane.value ? loadProgress(lane.value.runtime, now.value) : null))
const retryIn = computed(() =>
  lane.value?.next_retry_at ? Math.max(0, (Date.parse(lane.value.next_retry_at) - now.value) / 1000) : null,
)
const uptime = computed(() => {
  const rt = lane.value?.runtime
  return rt?.state === 'ready' && rt.ready_at ? (now.value - Date.parse(rt.ready_at)) / 1000 : null
})

let pollTimer: number | undefined
async function poll() {
  try {
    lanes.value = await api.admin.runtime()
    if (laneId.value === null && lanes.value.length) laneId.value = lanes.value[0].lane_id
  } catch (e) {
    showError(e)
  }
}

// ---- live log -------------------------------------------------------------
const MAX_LINES = 3000
const lines = ref<string[]>([])
const partial = ref('')
const live = ref(true)
const follow = ref(true)
const wrap = ref(false)
const filter = ref('')
const connected = ref(false)
const box = ref<HTMLElement | null>(null)
let source: EventSource | null = null
let held: string[] = [] // lines that arrived while paused

function connect() {
  source?.close()
  lines.value = []
  partial.value = ''
  held = []
  if (laneId.value === null) return
  source = new EventSource(api.admin.runtimeStreamUrl(laneId.value))
  source.onopen = () => (connected.value = true)
  source.onerror = () => (connected.value = false) // EventSource reconnects and resumes on its own
  source.onmessage = (ev) => {
    const data = JSON.parse(ev.data) as { lines: string[]; partial: string; reset: boolean }
    if (data.reset) {
      lines.value = []
      held = []
    }
    if (!live.value) {
      held.push(...data.lines)
    } else {
      append(data.lines)
    }
    partial.value = data.partial
  }
}

function append(more: string[]) {
  if (!more.length) return
  const next = lines.value.concat(more)
  lines.value = next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next
  if (follow.value) nextTick(() => box.value?.scrollTo({ top: box.value.scrollHeight }))
}

watch(live, (on) => {
  if (on) {
    append(held)
    held = []
  }
})
watch(laneId, connect)

const shown = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const all = partial.value ? [...lines.value, partial.value] : lines.value
  return q ? all.filter((l) => l.toLowerCase().includes(q)) : all
})

function tone(line: string) {
  if (line.startsWith('===== ')) return 'mark'
  if (/error|failed|fatal|out of memory|traceback|exception/i.test(line)) return 'bad'
  if (/warn/i.test(line)) return 'warn'
  return ''
}

function onScroll() {
  const el = box.value
  if (el) follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

// ---- history ----------------------------------------------------------------
const events = ref<(LoadEvent & Diagnosed)[]>([])
const failedOnly = ref(false)
const open = ref<number | null>(null)
async function loadEvents() {
  try {
    events.value = await api.admin.loadEvents({ laneId: laneId.value, failed: failedOnly.value, limit: 50 })
  } catch (e) {
    showError(e)
  }
}
watch([laneId, failedOnly], loadEvents)
const seconds = (a: string, b: string) => (Date.parse(b) - Date.parse(a)) / 1000

// ---- actions ----------------------------------------------------------------
const restarting = ref(false)
async function restart() {
  if (!lane.value) return
  restarting.value = true
  try {
    await api.restart(lane.value.lane_id)
    message.success(t('admin.lanes.reloading', { name: lane.value.lane_name }))
    setTimeout(poll, 600)
  } catch (e) {
    showError(e)
  } finally {
    restarting.value = false
  }
}

async function copy(text: string) {
  await navigator.clipboard.writeText(text)
  message.success(t('common.copied'))
}

onMounted(async () => {
  await poll()
  connect()
  loadEvents()
  pollTimer = window.setInterval(poll, 2000)
})
onBeforeUnmount(() => {
  window.clearInterval(pollTimer)
  source?.close()
})

// refresh the history whenever a load finishes
watch(
  () => lane.value?.runtime.state,
  (state, before) => {
    if (before === 'loading' && state !== 'loading') loadEvents()
  },
)
</script>

<template>
  <div v-if="lanes.length > 1" class="lane-pills">
    <button
      v-for="l in lanes"
      :key="l.lane_id"
      class="lane-pill"
      :class="{ active: l.lane_id === lane?.lane_id }"
      @click="laneId = l.lane_id"
    >
      <StatusDot :state="l.runtime.state" :label="false" />
      {{ l.lane_name }}
    </button>
  </div>

  <section v-if="lane" class="card card-pad status-card">
    <div class="row head">
      <StatusDot :state="lane.runtime.state" />
      <span class="mono model">{{ lane.runtime.model_name ?? t('admin.runtimeView.nothing') }}</span>
      <span v-if="lane.runtime.preset_name" class="badge">{{ lane.runtime.preset_name }}</span>
      <span class="spacer" />
      <NButton size="small" secondary :loading="restarting" @click="restart">{{ t('admin.lanes.reload') }}</NButton>
    </div>

    <dl class="facts">
      <div>
        <dt>{{ t('admin.runtimeView.engine') }}</dt>
        <dd>{{ lane.engine ?? '—' }}</dd>
      </div>
      <div>
        <dt>GPU</dt>
        <dd class="num">{{ lane.devices?.length ? lane.devices.join(', ') : t('admin.lanes.allGpus') }}</dd>
      </div>
      <div>
        <dt>{{ t('admin.runtimeView.port') }}</dt>
        <dd class="num">{{ lane.port }}</dd>
      </div>
      <div>
        <dt>PID</dt>
        <dd class="num">{{ lane.pid ?? '—' }}</dd>
      </div>
      <div>
        <dt>{{ t('admin.runtimeView.inflight') }}</dt>
        <dd class="num">{{ lane.inflight }}<span v-if="lane.runtime.draining" class="faint"> · {{ t('admin.runtimeView.draining') }}</span></dd>
      </div>
      <div>
        <dt>{{ t('admin.runtimeView.target') }}</dt>
        <dd>
          <span class="mono">{{ lane.target_model ?? t('admin.runtimeView.nothing') }}</span>
          <span v-if="lane.target_reason" class="faint"> · {{ t(`admin.runtimeView.reason.${lane.target_reason}`) }}</span>
        </dd>
      </div>
      <div v-if="uptime !== null">
        <dt>{{ t('admin.runtimeView.uptime') }}</dt>
        <dd class="num">{{ formatDuration(uptime) }}</dd>
      </div>
    </dl>

    <!-- loading -->
    <div v-if="progress" class="progress">
      <div class="row">
        <span class="strong">{{ progress.stage ? t(`admin.runtimeView.stage.${progress.stage}`) : t('state.loading') }}</span>
        <span v-if="progress.weights !== null" class="muted num">· {{ t('admin.runtimeView.weights', { pct: Math.round(progress.weights) }) }}</span>
        <span class="spacer" />
        <span class="muted num">
          {{ formatDuration(progress.elapsed) }} / ~{{ formatDuration(lane.runtime.est_load_seconds) }}
        </span>
      </div>
      <div class="bar"><span :style="{ width: `${progress.pct}%` }" /></div>
      <div class="faint small">
        {{ progress.source === 'log' ? t('admin.runtimeView.fromLog') : t('admin.runtimeView.fromEstimate') }}
      </div>
    </div>

    <!-- failed -->
    <div v-if="lane.runtime.state === 'failed'" class="failure">
      <div class="strong">{{ lane.runtime.error }}</div>
      <div class="row wrap small">
        <span v-if="lane.exit_code !== null">{{ t('admin.runtimeView.exitCode', { code: lane.exit_code }) }}</span>
        <span>{{ t('admin.runtimeView.attempts', { n: lane.fail_count }) }}</span>
        <span v-if="retryIn !== null">{{ t('admin.runtimeView.retryIn', { time: formatDuration(retryIn) }) }}</span>
        <span class="spacer" />
        <NButton size="tiny" type="error" secondary :loading="restarting" @click="restart">
          {{ t('admin.runtimeView.retryNow') }}
        </NButton>
      </div>
      <DiagnosisCard
        v-if="lane.diagnosis?.recognized"
        class="diag-card"
        :diagnosis="lane.diagnosis"
        :title="`${lane.lane_name}: ${lane.runtime.model_name ?? ''}`"
        :log-url="api.admin.runtimeLogUrl(lane.lane_id)"
        retry
        :retrying="restarting"
        @retry="restart"
      />
      <p v-else-if="lane.runtime.error_hint === 'oom'" class="small">{{ t('admin.runtimeView.oomHint') }}</p>
      <pre v-if="lane.failure_log.length && !lane.diagnosis?.recognized" class="excerpt">{{ lane.failure_log.join('\n') }}</pre>
    </div>

    <div v-if="lane.command" class="command">
      <span class="faint small">{{ t('admin.runtimeView.command') }}</span>
      <code class="mono">{{ lane.command }}</code>
      <button class="icon-btn" :aria-label="t('common.copy')" @click="copy(lane.command)"><Copy :size="14" /></button>
    </div>
  </section>

  <section v-if="lane" class="card console-card">
    <div class="card-header">
      <div class="row">
        <h3 class="card-title">{{ t('admin.runtimeView.log') }}</h3>
        <span class="live-dot" :class="{ on: connected && live }" />
        <span class="faint small">{{ connected ? (live ? t('admin.runtimeView.live') : t('admin.runtimeView.paused')) : t('admin.runtimeView.connecting') }}</span>
        <span class="faint small mono">{{ lane.log_file }}</span>
      </div>
      <div class="row wrap">
        <NInput v-model:value="filter" size="small" clearable :placeholder="t('admin.runtimeView.filter')" class="filter" />
        <label class="row small toggle"><NSwitch v-model:value="wrap" size="small" />{{ t('admin.runtimeView.wrap') }}</label>
        <NButton size="small" quaternary @click="live = !live">
          <template #icon><Play v-if="!live" :size="14" /><Pause v-else :size="14" /></template>
          {{ live ? t('admin.runtimeView.pause') : t('admin.runtimeView.resume') }}
        </NButton>
        <NButton size="small" quaternary @click="lines = []">
          <template #icon><Trash2 :size="14" /></template>
          {{ t('admin.runtimeView.clear') }}
        </NButton>
        <NButton size="small" quaternary tag="a" :href="api.admin.runtimeLogUrl(lane.lane_id)">
          <template #icon><Download :size="14" /></template>
          {{ t('admin.runtimeView.download') }}
        </NButton>
      </div>
    </div>
    <div ref="box" class="console mono" :class="{ wrap }" @scroll="onScroll">
      <div v-if="!shown.length" class="faint">{{ t('admin.runtimeView.noOutput') }}</div>
      <div v-for="(line, i) in shown" :key="i" class="line" :class="tone(line)">{{ line || ' ' }}</div>
    </div>
    <button v-if="!follow" class="jump" @click="follow = true; box?.scrollTo({ top: box.scrollHeight })">
      {{ t('admin.runtimeView.jump') }}
    </button>
  </section>

  <section class="card">
    <div class="card-header">
      <h3 class="card-title">{{ t('admin.loadEvents') }}</h3>
      <label class="row small toggle"><NSwitch v-model:value="failedOnly" size="small" />{{ t('admin.runtimeView.failedOnly') }}</label>
    </div>
    <div v-if="!events.length" class="empty">{{ t('admin.loadEventsEmpty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.model') }}</th>
            <th>{{ t('admin.started') }}</th>
            <th>{{ t('admin.took') }}</th>
            <th>{{ t('admin.result') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <template v-for="e in events" :key="e.id">
            <tr class="event-row" @click="open = open === e.id ? null : e.id">
              <td class="mono nowrap">{{ e.model_name ?? '—' }}</td>
              <td class="muted num nowrap">{{ formatDateTime(e.started_at) }}</td>
              <td class="muted num nowrap">{{ formatDuration(seconds(e.started_at, e.finished_at)) }}</td>
              <td>
                <span v-if="e.success" class="badge ok">{{ t('admin.success') }}</span>
                <span v-else class="badge danger">{{ t('admin.failed') }}</span>
                <span v-if="!e.success && e.error" class="faint err">{{ e.error }}</span>
              </td>
              <td class="right faint small">{{ open === e.id ? '▴' : '▾' }}</td>
            </tr>
            <tr v-if="open === e.id" class="detail-row">
              <td colspan="5">
                <div v-if="e.exit_code !== null" class="small">{{ t('admin.runtimeView.exitCode', { code: e.exit_code }) }}</div>
                <div v-if="e.command" class="small"><span class="faint">{{ t('admin.runtimeView.command') }}</span> <code class="mono">{{ e.command }}</code></div>
                <DiagnosisCard v-if="e.diagnosis?.recognized" class="diag-card" :diagnosis="e.diagnosis" :title="e.model_name ?? ''" />
                <pre v-if="e.log_excerpt" class="excerpt">{{ e.log_excerpt }}</pre>
                <div v-else-if="e.success" class="faint small">{{ t('admin.runtimeView.noExcerpt') }}</div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.diag-card {
  margin: 10px 0;
}
.lane-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.lane-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-2);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.lane-pill.active {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
}
.status-card {
  margin-bottom: 16px;
}
.head {
  gap: 10px;
}
.model {
  font-weight: 600;
}
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px 24px;
  margin: 16px 0 0;
}
.facts dt {
  color: var(--text-3);
  font-size: 12px;
}
.facts dd {
  margin: 2px 0 0;
  font-size: 13px;
}
.progress {
  margin-top: 18px;
}
.bar {
  height: 8px;
  margin: 8px 0 6px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--brand);
  transition: width 0.4s ease;
}
.failure {
  margin-top: 18px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
  border-radius: 10px;
  background: var(--danger-soft);
  color: var(--danger);
  display: grid;
  gap: 8px;
}
.excerpt {
  margin: 4px 0 0;
  max-height: 260px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 8px;
  background: #0f1117;
  color: #d6d9e0;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.command {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
  min-width: 0;
}
.command code {
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  white-space: nowrap;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--surface-muted);
  font-size: 12px;
}
.console-card {
  position: relative;
  margin-bottom: 16px;
}
.console {
  height: 420px;
  overflow: auto;
  padding: 12px 16px;
  background: #0f1117;
  color: #c9ced8;
  font-size: 12px;
  line-height: 1.55;
  border-radius: 0 0 12px 12px;
}
.console .line {
  white-space: pre;
}
.console.wrap .line {
  white-space: pre-wrap;
  word-break: break-all;
}
.line.mark {
  color: #a5b4fc;
}
.line.bad {
  color: #fca5a5;
}
.line.warn {
  color: #fcd34d;
}
.jump {
  position: absolute;
  right: 20px;
  bottom: 16px;
  padding: 4px 10px;
  border: 0;
  border-radius: 999px;
  background: var(--brand);
  color: #fff;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-3);
}
.live-dot.on {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.2);
}
.filter {
  width: 180px;
}
.toggle {
  gap: 6px;
  cursor: pointer;
}
.event-row {
  cursor: pointer;
}
.nowrap {
  white-space: nowrap;
}
.detail-row td {
  background: var(--surface-muted);
  display: table-cell;
}
.detail-row td > * + * {
  margin-top: 6px;
}
.err {
  margin-left: 8px;
}
@media (max-width: 640px) {
  .filter {
    width: 100%;
  }
  .console {
    height: 320px;
  }
}
</style>
