<script setup lang="ts">
// One environment as a workbench (F04): its name and state, time left with +1 h and a two-step End, the web
// interfaces (opened through web-open), live GPU use and GPU-hours of this session, the SSH command with its own
// Host alias and host key, and folded: ports, files and storage, more (save as image, restart, logs).
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NInput, NModal, useMessage } from 'naive-ui'
import { ChevronDown, Clock, Cpu, ExternalLink, Gauge, Plus, Square } from 'lucide-vue-next'
import { api, ApiError } from '@/api/client'
import { cpuApi } from '@/api/devenvCpu'
import type { DevProject, Reservation } from '@/api/types'
import DevEnvPorts from '@/components/devenv/DevEnvPorts.vue'
import CpuSessionInfo from '@/components/devenv/CpuSessionInfo.vue'
import { loadCpu } from '@/composables/devenvCpu'
import { useLanes } from '@/composables/lanes'
import { session as me } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useNow } from '@/composables/useNow'
import { useStatus } from '@/composables/useStatus'
import { useReasonText } from '@/features/quickbook/reasons'
import { placementApi, type Reason } from '@/features/quickbook/api'
import { formatRange, formatTime } from '@/utils/format'
import { gpuListName } from '@/utils/naming'
import { devenvApi, type DevSessionX } from '../api'
import StorageInfo from '../StorageInfo.vue'
import { useWebOpen } from '../useWebOpen'
import ConnectInfo from './ConnectInfo.vue'
import SshCredentials from './SshCredentials.vue'

const props = defineProps<{
  session: DevSessionX
  title: string
  alias: string
  sshHost: string
  booking?: Reservation | null
  project?: DevProject | null
  /** show whose it is (admins looking at everyone's) */
  showOwner?: boolean
  canLogIn: boolean
  passwordLogin: boolean
}>()
const emit = defineEmits<{ changed: [] }>()

const { t, te } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const reasonText = useReasonText()
const web = useWebOpen()
const now = useNow(15_000)
const { lanes } = useLanes()
const { status } = useStatus()

const s = computed(() => props.session)
const isAdmin = computed(() => !!me.user?.is_admin)
const canManage = computed(() => s.value.mine || isAdmin.value)
const running = computed(() => s.value.state === 'running')
const kept = computed(() => s.value.project_id !== null)
const isCpu = computed(() => s.value.kind === 'cpu')
const activeBooking = computed(
  () => s.value.kind === 'booking' && !!s.value.end_at && !!s.value.reservation_id && s.value.state !== 'stopped',
)

// ---- time -----------------------------------------------------------------------------------------------------
const msLeft = computed(() => (s.value.end_at ? new Date(s.value.end_at).getTime() - now.value : 0))
const timeLeft = computed(() => {
  const m = Math.max(0, Math.floor(msLeft.value / 60_000))
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`
})
const started = computed(() => !!s.value.start_at && new Date(s.value.start_at).getTime() <= now.value)

// +1 h: judged beforehand with the booking rules (placement fit of the hour after the end), so the button can say
// why not ("free until 09:28 · then alice", "over your weekly quota") instead of failing on click.
const IGNORED = new Set(['quota_upcoming', 'devenv_project_limit', 'needs_approval'])
const extendBlock = ref<string | null>(null)
const extendMax = ref<number | null>(null) // minutes still free when less than an hour is
const extending = ref(false)

async function checkExtend() {
  const b = props.booking
  if (!s.value.mine || !activeBooking.value || !b || b.phase !== 'active') return
  const from = new Date(b.end_at)
  const to = new Date(from.getTime() + 3_600_000)
  try {
    const fit = await placementApi.fit({
      kind: 'devenv',
      devenv_template_id: b.devenv_template_id ?? null,
      devenv_project_id: b.devenv_project_id ?? null,
      start_at: from.toISOString(),
      end_at: to.toISOString(),
      lane_id: b.lane_id ?? null,
      suggest: false,
    })
    const cand = fit.candidates.find((c) => c.lane_id === b.lane_id) ?? fit.best
    const no: Reason[] = (cand ? cand.reasons_no : fit.reasons_no).filter((r) => !IGNORED.has(r.code))
    extendMax.value = null
    if (!no.length) {
      extendBlock.value = null
      return
    }
    const conflict = no.find((r) => r.code === 'reservation_conflict')
    if (conflict && typeof conflict.params.start_at === 'string') {
      const free = Math.floor((new Date(conflict.params.start_at).getTime() - from.getTime()) / 60_000)
      const slot = me.config?.slot_minutes ?? 30
      if (free >= slot) extendMax.value = free - (free % slot)
      extendBlock.value = t('devenvUi.time.freeUntil', {
        time: formatTime(conflict.params.start_at),
        user: String(conflict.params.username ?? '?'),
      })
      return
    }
    extendBlock.value = reasonText(no[0])
  } catch {
    extendBlock.value = null // the server says why on click
  }
}

async function extend(minutes: number) {
  if (!s.value.reservation_id) return
  extending.value = true
  try {
    await devenvApi.extend(s.value.reservation_id, minutes)
    message.success(t('devenvUi.time.extended', { n: minutes >= 60 ? `${minutes / 60} h` : `${minutes} min` }))
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    extending.value = false
  }
}
const extendOptions = computed(() =>
  [30, 60, 120].map((m) => ({ key: m, label: m >= 60 ? `+${m / 60} h` : `+${m} min` })),
)
const extendLabel = computed(() => (extendMax.value ? `+${extendMax.value} min` : '+1 h'))
function extendClick() {
  void extend(extendMax.value ?? 60)
}

let fitTimer: number | undefined
onMounted(() => {
  void checkExtend()
  fitTimer = window.setInterval(checkExtend, 120_000)
})
onBeforeUnmount(() => window.clearInterval(fitTimer))
watch(() => [props.booking?.end_at, props.booking?.phase], checkExtend)

// ---- GPU use ----------------------------------------------------------------------------------------------------
const lane = computed(() => lanes.value.find((l) => l.id === s.value.lane_id) ?? null)
const gpuIndexes = computed(() => {
  if (isCpu.value || !s.value.lane_id) return []
  const l = lane.value
  if (l && l.gpus.length) return l.gpus
  return (status.value?.gpus ?? []).map((g) => g.index)
})
const gpus = computed(() => (status.value?.gpus ?? []).filter((g) => gpuIndexes.value.includes(g.index)))
const where = computed(() => {
  if (isCpu.value) return t('devenvUi.card.noGpu')
  if (!s.value.lane_id) return ''
  return gpuIndexes.value.length ? gpuListName(gpuIndexes.value, t) : (s.value.lane_name ?? '')
})
const gpuHours = computed(() => {
  if (isCpu.value || !running.value || !gpuIndexes.value.length) return null
  const from = s.value.started_at ?? s.value.start_at
  if (!from) return null
  const hours = Math.max(0, now.value - new Date(from).getTime()) / 3_600_000
  return Math.round(hours * gpuIndexes.value.length * 10) / 10
})

// ---- web interfaces ---------------------------------------------------------------------------------------------
function openLabel(p: { name: string; kind: string }) {
  return p.kind === 'http' ? t('devenv.web.openNamed', { name: p.name }) : t(`devenv.web.open.${p.kind}`)
}

// ---- ending -------------------------------------------------------------------------------------------------------
const endDialog = ref(false)
const ending = ref(false)
function askEnd() {
  if (isCpu.value && kept.value) {
    void stopNow() // pausing a kept environment loses nothing
    return
  }
  endDialog.value = true
}
async function stopNow() {
  ending.value = true
  try {
    if (isCpu.value) {
      await cpuApi.stop(s.value.id)
      message.success(kept.value ? t('devenvCpu.paused') : t('devenvCpu.stopped'))
      await loadCpu().catch(() => undefined)
    } else if (s.value.reservation_id) {
      await devenvApi.end(s.value.reservation_id)
      message.success(t('devenvUi.end.ended'))
    }
    endDialog.value = false
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    ending.value = false
  }
}
const endLabel = computed(() => {
  if (isCpu.value) return s.value.state === 'failed' ? t('devenvCpu.dismiss') : kept.value ? t('devenvCpu.pause') : t('devenvCpu.stop')
  return started.value ? t('devenvUi.end.endNow') : t('devenvUi.end.cancel')
})

// ---- save as image (D11: the way to keep what you installed in M1), restart, logs ------------------------------
const saveForm = ref({ open: false, name: '' })
const busy = ref(false)
function openSave() {
  endDialog.value = false
  saveForm.value = { open: true, name: props.project?.name ?? s.value.template_name ?? '' }
}
async function save() {
  busy.value = true
  try {
    await api.devenv.save(s.value.id, saveForm.value.name.trim())
    saveForm.value.open = false
    message.success(t('devenv.saving'))
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
async function restart() {
  try {
    await api.devenv.restart(s.value.id)
    message.success(t('devenv.restarting'))
    emit('changed')
  } catch (e) {
    showError(e)
  }
}
const logs = ref({ open: false, text: '' })
async function showLogs() {
  try {
    logs.value = { open: true, text: (await api.devenv.logs(s.value.id)).text || t('devenv.noLogs') }
  } catch (e) {
    showError(e)
  }
}

// ---- failures: a sentence first, the raw text folded ---------------------------------------------------------------
const failure = computed(() => {
  const err = s.value.error
  if (!err) return null
  if (/^[a-z_]+$/.test(err)) return te(`errors.${err}`) ? t(`errors.${err}`, { lane: s.value.lane_name ?? '' }) : err
  return null
})

const addKey = ref(false)
const stateTone: Record<string, string> = {
  running: 'ok',
  failed: 'danger',
  pulling: 'warn',
  starting: 'warn',
  stopping: 'warn',
  ready: 'brand',
  waiting: '',
  stopped: '',
}
const subtitle = computed(() => {
  const parts: string[] = []
  if (s.value.template_name && s.value.template_name !== props.title) parts.push(s.value.template_name)
  if (kept.value) parts.push(t('devenvUi.card.kept'))
  return parts.join(' · ')
})
const imageNote = computed(() => (isAdmin.value || !s.value.template_name ? s.value.image : ''))
</script>

<template>
  <article class="card env" :data-session="s.id" :data-kind="s.kind" data-test="env-card">
    <!-- header: name, state, where; time left, +1 h, End -->
    <header class="head">
      <div class="title-line">
        <span class="strong name">{{ title }}</span>
        <span class="badge" :class="stateTone[s.state]">{{ t(`devenv.state.${s.state}`) }}</span>
        <span v-if="where" class="faint small where">
          <Cpu v-if="isCpu" :size="12" />{{ where }}
        </span>
        <span v-if="showOwner && s.username" class="faint small">{{ s.username }}</span>
      </div>
      <div class="controls">
        <template v-if="activeBooking && started">
          <span class="left num" :class="{ low: msLeft < 15 * 60_000 }" :title="formatRange(s.start_at!, s.end_at!)" data-test="time-left">
            <Clock :size="13" /> {{ t('devenvUi.time.left', { time: timeLeft }) }}
          </span>
          <div v-if="canManage" class="extend" :title="extendBlock ?? ''">
            <NButton
              size="small"
              secondary
              :loading="extending"
              :disabled="!!extendBlock && !extendMax"
              data-test="extend"
              @click="extendClick"
            >
              <template #icon><Plus :size="13" /></template>
              {{ extendLabel.replace('+', '') }}
            </NButton>
            <NDropdown v-if="!extendBlock" trigger="click" :options="extendOptions" @select="(m: number) => extend(m)">
              <NButton size="small" secondary class="more-time" :aria-label="t('devenvUi.time.more')">
                <ChevronDown :size="13" />
              </NButton>
            </NDropdown>
          </div>
        </template>
        <span v-else-if="activeBooking && s.start_at" class="faint small num">{{ formatRange(s.start_at, s.end_at!) }}</span>
        <NButton
          v-if="canManage && (activeBooking || (isCpu && s.state !== 'stopped' && s.state !== 'stopping'))"
          size="small"
          :type="isCpu && kept ? 'default' : 'error'"
          secondary
          :loading="ending && !endDialog"
          data-test="end"
          @click="askEnd"
        >
          <template #icon><Square :size="12" /></template>
          {{ endLabel }}
        </NButton>
      </div>
    </header>
    <p v-if="subtitle || imageNote" class="faint small sub">
      {{ subtitle }}<span v-if="subtitle && imageNote"> · </span><span v-if="imageNote" class="mono">{{ imageNote }}</span>
    </p>
    <p v-if="extendBlock && canManage && activeBooking && started" class="small faint block-reason" data-test="extend-reason">
      {{ t('devenvUi.time.cannotExtend', { why: extendBlock }) }}
    </p>

    <!-- progress -->
    <div v-if="s.state === 'pulling'" class="progress">
      <div class="bar"><span :style="{ width: `${s.progress ?? 3}%` }" /></div>
      <div class="faint small">{{ t('devenv.pulling', { pct: s.progress !== null ? Math.round(s.progress) : '…' }) }}</div>
    </div>
    <p v-else-if="(s.state === 'ready' || s.state === 'waiting') && !isCpu" class="muted small">{{ t('devenv.waitingStart') }}</p>
    <p v-else-if="s.state === 'starting'" class="muted small">
      {{ s.detail === 'snapshot' ? t('devenv.snapshot') : t('devenv.starting') }}
    </p>

    <!-- failure -->
    <div v-if="s.state === 'failed'" class="failure" data-test="env-failure">
      <p class="small">{{ failure ?? t('devenvUi.card.failed') }}</p>
      <details v-if="!failure && s.error" class="raw">
        <summary class="small faint">{{ t('devenvUi.card.details') }}</summary>
        <pre class="mono small">{{ s.error }}</pre>
      </details>
    </div>
    <p v-else-if="s.error" class="small warn-text">{{ failure ?? s.error }}</p>

    <!-- open -->
    <div v-if="running && s.web.length" class="row wrap open">
      <NButton
        v-for="(p, i) in s.web"
        :key="p.port"
        :type="i === 0 ? 'primary' : 'default'"
        :secondary="i > 0"
        size="small"
        data-test="web-open"
        @click="web.open(s.id, p.port)"
      >
        <template #icon><ExternalLink :size="14" /></template>
        {{ openLabel(p) }}
      </NButton>
      <span class="faint small">{{ t('devenv.web.newTab') }}</span>
    </div>

    <!-- live use -->
    <div v-if="running && (gpus.length || gpuHours !== null)" class="row wrap live small" data-test="gpu-live">
      <Gauge :size="13" class="faint" />
      <span v-for="g in gpus" :key="g.index" class="num">
        {{ t('devenvUi.card.gpuUse', { gpu: `GPU ${g.index}`, pct: g.utilization_pct }) }}
        <span class="faint">· {{ (g.memory_used_mb / 1024).toFixed(1) }}/{{ (g.memory_total_mb / 1024).toFixed(0) }} GB</span>
      </span>
      <span v-if="gpuHours !== null" class="num faint">{{ t('devenvUi.card.gpuHours', { n: gpuHours }) }}</span>
    </div>
    <CpuSessionInfo v-if="isCpu" :session="s" :projects="project ? [project] : []" />

    <!-- SSH -->
    <template v-if="running && s.ssh_command">
      <ConnectInfo v-if="canLogIn || !s.mine || !s.web.length" :session="s" :alias="alias" :ssh-host="sshHost" />
      <div v-else class="also small">
        <span class="faint">{{ t('devenvUi.card.alsoSsh') }}</span>
        <button type="button" class="link" data-test="also-ssh" @click="addKey = !addKey">{{ t('devenvUi.card.addKey') }}</button>
        <SshCredentials v-if="addKey" :password-login="passwordLogin" @done="emit('changed')" />
      </div>
      <p v-if="s.detail === 'saving'" class="small warn-text">{{ t('devenv.saving') }}</p>
    </template>

    <!-- folded -->
    <div class="folds">
      <details v-if="running" class="fold">
        <summary class="small">{{ t('devenvUi.card.ports') }}</summary>
        <DevEnvPorts :session="s" :can-manage="canManage" />
      </details>
      <details class="fold">
        <summary class="small">{{ t('devenvUi.card.files') }}</summary>
        <StorageInfo compact :project="project" :kept="kept" />
      </details>
      <details v-if="canManage" class="fold">
        <summary class="small">{{ t('devenvUi.card.more') }}</summary>
        <div class="row wrap actions">
          <NButton v-if="running" size="small" secondary :disabled="s.detail === 'saving'" data-test="save-image" @click="openSave">
            {{ t('devenv.save') }}
          </NButton>
          <NButton v-if="running || s.state === 'failed'" size="small" secondary @click="restart">
            {{ s.state === 'failed' ? t('devenv.retry') : t('devenv.restart') }}
          </NButton>
          <NButton size="small" quaternary @click="showLogs">{{ t('devenv.logs') }}</NButton>
        </div>
        <p v-if="running && !kept" class="faint small">{{ t('devenvUi.card.saveHint') }}</p>
      </details>
    </div>

    <!-- End, two steps (D11: no "keep" in M1; save as image instead) -->
    <NModal
      v-model:show="endDialog"
      preset="card"
      :title="isCpu ? t('devenvUi.end.stopTitle', { name: title }) : t('devenvUi.end.title', { name: title })"
      style="max-width: 480px"
      data-test="end-dialog"
    >
      <p v-if="kept" class="small">{{ t('devenvUi.end.keptBody') }}</p>
      <template v-else>
        <p class="small">{{ t('devenvUi.end.body') }}</p>
        <p class="faint small">{{ t('devenvUi.end.saveFirst') }}</p>
      </template>
      <template #footer>
        <div class="footer">
          <NButton @click="endDialog = false">{{ t('common.cancel') }}</NButton>
          <NButton v-if="!kept && running" secondary data-test="end-save" @click="openSave">{{ t('devenv.save') }}…</NButton>
          <NButton type="error" :loading="ending" data-test="end-confirm" @click="stopNow">
            {{ isCpu ? t('devenvUi.end.stopConfirm') : t('devenvUi.end.confirm') }}
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal v-model:show="saveForm.open" preset="card" :title="t('devenv.save')" style="max-width: 460px">
      <p class="muted small">{{ t('devenv.saveHint') }}</p>
      <NInput v-model:value="saveForm.name" :placeholder="t('devenv.templateName')" maxlength="64" />
      <template #footer>
        <div class="footer">
          <NButton @click="saveForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!saveForm.name.trim()" @click="save">{{ t('devenv.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <NModal v-model:show="logs.open" preset="card" :title="title" style="max-width: 820px">
      <pre class="log mono">{{ logs.text }}</pre>
    </NModal>
  </article>
</template>

<style scoped>
.env {
  display: grid;
  gap: 10px;
  padding: 14px 16px;
  margin-bottom: 12px;
  min-width: 0;
}
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
}
.title-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  min-width: 0;
}
.name {
  font-size: 15px;
  overflow-wrap: anywhere;
}
.where {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.left {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
  font-size: 13px;
}
.left.low {
  color: var(--warn-text);
}
.extend {
  display: inline-flex;
}
.extend :deep(.n-button:first-child) {
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
}
.more-time {
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  margin-left: 1px;
  padding: 0 6px;
}
.sub,
.block-reason {
  margin: -6px 0 0;
  overflow-wrap: anywhere;
}
p {
  margin: 0;
}
.progress .bar {
  height: 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: hidden;
  margin-bottom: 6px;
}
.progress .bar span {
  display: block;
  height: 100%;
  background: var(--brand);
  transition: width 0.4s ease;
}
.open {
  gap: 10px;
  align-items: center;
}
.live {
  gap: 4px 14px;
  align-items: center;
}
.also {
  display: grid;
  gap: 6px;
}
.also .link {
  justify-self: start;
}
.link {
  background: none;
  border: 0;
  padding: 0;
  color: var(--brand);
  cursor: pointer;
  font-size: inherit;
}
.failure {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger-text);
}
.failure pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
  color: var(--text-2);
}
.raw summary {
  cursor: pointer;
}
.warn-text {
  color: var(--warn-text);
}
.folds {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.fold {
  min-width: 0;
}
.fold[open] {
  flex-basis: 100%;
  display: grid;
  gap: 8px;
}
.fold summary {
  cursor: pointer;
  color: var(--text-2);
  font-weight: 500;
}
.actions {
  gap: 8px;
}
.footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.log {
  margin: 0;
  max-height: 60vh;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: #0f1117;
  color: #c9ced8;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
