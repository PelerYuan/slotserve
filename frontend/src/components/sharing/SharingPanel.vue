<script setup lang="ts">
// Owner controls of one machine: sharing hours, pause ("I'm using my PC"), what runs on it, Wake-on-LAN and
// contribution. Used on "My machines" and in Admin -> Nodes.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NInput, NRadioButton, NRadioGroup, NSelect, useDialog, useMessage } from 'naive-ui'
import { Pause, Play, Plus, Power, Trash2 } from 'lucide-vue-next'
import {
  NIGHTS_AND_WEEKENDS,
  sharingApi,
  type NodeContribution,
  type NodeRunning,
  type NodeSharing,
  type RunningBooking,
  type SharingConflict,
  type SharingWindow,
} from '@/api/sharing'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, formatRange, relativeFromNow } from '@/utils/format'

const props = defineProps<{ nodeId: number }>()
const emit = defineEmits<{ changed: [node: NodeSharing] }>()

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()

const node = ref<NodeSharing | null>(null)
const running = ref<NodeRunning | null>(null)
const contribution = ref<NodeContribution | null>(null)
const mode = ref<'always' | 'schedule' | 'never'>('always')
const windows = ref<SharingWindow[]>([])
const macs = ref('')
const network = ref('')
const conflicts = ref<SharingConflict[]>([])
const busy = ref(false)

function fromNode(n: NodeSharing) {
  node.value = n
  const s = n.sharing_schedule
  mode.value = s === null ? 'always' : s.length ? 'schedule' : 'never'
  windows.value = (s ?? []).map((w) => ({ days: [...w.days], start: w.start, end: w.end }))
  macs.value = n.mac_addresses
  network.value = n.network ?? ''
}

async function loadRunning() {
  try {
    running.value = await sharingApi.running(props.nodeId)
  } catch (e) {
    showError(e)
  }
}

async function load() {
  try {
    fromNode(await sharingApi.get(props.nodeId))
    contribution.value = await sharingApi.contribution(props.nodeId).catch(() => null)
  } catch (e) {
    showError(e)
  }
  await loadRunning()
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(loadRunning, 10_000)
})
onBeforeUnmount(() => window.clearInterval(timer))
watch(() => props.nodeId, load)

function changed(n: NodeSharing) {
  fromNode(n)
  emit('changed', n)
}

// ---- status -----------------------------------------------------------------------------------------------

const status = computed(() => {
  const n = node.value
  if (!n) return { cls: '', text: '', detail: '' }
  if (n.sharing_paused) {
    return {
      cls: 'warn',
      text: n.sharing_paused_until
        ? t('sharing.pausedUntil', { when: formatDateTime(n.sharing_paused_until) })
        : t('sharing.pausedOpen'),
      detail:
        n.sharing_stops_at && new Date(n.sharing_stops_at).getTime() > Date.now()
          ? t('sharing.stopsAt', { when: relativeFromNow(n.sharing_stops_at) })
          : '',
    }
  }
  const next = n.sharing_next_change ? formatDateTime(n.sharing_next_change) : ''
  return n.shared_now
    ? { cls: 'ok', text: t('sharing.sharedNow'), detail: next ? t('sharing.sharedUntil', { when: next }) : '' }
    : { cls: '', text: t('sharing.notShared'), detail: next ? t('sharing.sharedFrom', { when: next }) : '' }
})

// ---- pause --------------------------------------------------------------------------------------------------

const pauseOptions = computed(() => [
  ...[1, 2, 4, 8].map((n) => ({ key: n, label: t('sharing.pauseFor', { n }) })),
  { key: 0, label: t('sharing.pauseOpen') },
])

async function pause(hours: number) {
  busy.value = true
  try {
    const r = await sharingApi.pause(props.nodeId, hours || null)
    changed(r.node)
    message.success(t('sharing.paused1', { n: r.affected }))
    await loadRunning()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function resume() {
  busy.value = true
  try {
    changed(await sharingApi.resume(props.nodeId))
    message.success(t('sharing.resumed'))
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

// ---- sharing hours --------------------------------------------------------------------------------------------

const TIMES = Array.from({ length: 49 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, '0')}:${i % 2 ? '30' : '00'}`)
const startOptions = TIMES.slice(0, -1).map((v) => ({ label: v, value: v }))
const endOptions = TIMES.slice(1).map((v) => ({ label: v, value: v }))
const overnight = (w: SharingWindow) => w.end !== '24:00' && w.end <= w.start

function toggleDay(w: SharingWindow, d: number) {
  w.days = w.days.includes(d) ? w.days.filter((x) => x !== d) : [...w.days, d].sort()
}
function addWindow() {
  windows.value.push({ days: [0, 1, 2, 3, 4], start: '18:00', end: '08:00' })
}
function usePreset() {
  windows.value = NIGHTS_AND_WEEKENDS.map((w) => ({ days: [...w.days], start: w.start, end: w.end }))
  mode.value = 'schedule'
}
const hoursValid = computed(
  () => mode.value !== 'schedule' || (windows.value.length > 0 && windows.value.every((w) => w.days.length && w.start !== w.end)),
)

async function saveHours() {
  busy.value = true
  try {
    const schedule = mode.value === 'always' ? null : mode.value === 'never' ? [] : windows.value
    const r = await sharingApi.save(props.nodeId, { schedule })
    conflicts.value = r.conflicts
    changed(r.node)
    message.success(t('sharing.saved'))
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

// ---- what runs --------------------------------------------------------------------------------------------------

const laneName = (id: number | null) => running.value?.lanes.find((l) => l.id === id)?.name ?? ''

function stopBooking(b: RunningBooking) {
  dialog.warning({
    title: t('sharing.stop'),
    content: t('sharing.stopConfirm', { user: b.username, when: formatRange(b.start_at, b.end_at) }),
    positiveText: t('sharing.stop'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        running.value = await sharingApi.stop(props.nodeId, { reservation_id: b.id })
        message.success(t('sharing.stopped'))
      } catch (e) {
        showError(e)
      }
    },
  })
}

async function stopLane(laneId: number) {
  try {
    running.value = await sharingApi.stop(props.nodeId, { lane_id: laneId })
    message.success(t('sharing.stopped'))
  } catch (e) {
    showError(e)
  }
}

const idleBusy = (reason: string | null, state: string | null) =>
  !!reason && ['default', 'idle-devenv', 'fallback'].includes(reason) && state !== 'stopped'

// ---- Wake-on-LAN --------------------------------------------------------------------------------------------------

async function saveWol() {
  busy.value = true
  try {
    const r = await sharingApi.save(props.nodeId, { mac_addresses: macs.value.trim(), network: network.value.trim() })
    changed(r.node)
    message.success(t('cluster.saved'))
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function wake() {
  busy.value = true
  try {
    const r = await sharingApi.wake(props.nodeId)
    message.success(t('sharing.woken', { via: r.senders.join(', ') }))
    await load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

const hours = (n: number | undefined) => t('sharing.hoursShort', { n: (n ?? 0) < 10 ? (n ?? 0).toFixed(1) : Math.round(n ?? 0) })
</script>

<template>
  <div v-if="node" class="sharing" :data-sharing-node="node.name">
    <div class="status-row">
      <span class="badge" :class="node.online ? 'ok' : 'danger'">{{ node.online ? t('sharing.online') : t('sharing.offline') }}</span>
      <span class="badge" :class="status.cls" data-field="sharing-status">{{ status.text }}</span>
      <span v-if="status.detail" class="faint small">{{ status.detail }}</span>
      <span v-if="node.platform" class="faint small">· {{ node.platform }}</span>
      <span class="spacer" />
      <NButton v-if="node.sharing_paused" size="small" type="primary" :loading="busy" data-action="resume-sharing" @click="resume">
        <template #icon><Play :size="14" /></template>
        {{ t('sharing.resume') }}
      </NButton>
      <NDropdown v-else trigger="click" :options="pauseOptions" @select="pause">
        <NButton size="small" type="warning" secondary :loading="busy" data-action="pause-sharing">
          <template #icon><Pause :size="14" /></template>
          {{ t('sharing.pause') }}
        </NButton>
      </NDropdown>
    </div>
    <p v-if="!node.sharing_paused" class="faint small hint">{{ t('sharing.pauseHint', { n: node.grace_minutes }) }}</p>

    <h4 class="sub">{{ t('sharing.hours') }}</h4>
    <NRadioGroup v-model:value="mode" size="small">
      <NRadioButton value="always">{{ t('sharing.always') }}</NRadioButton>
      <NRadioButton value="schedule" data-action="schedule-mode">{{ t('sharing.schedule') }}</NRadioButton>
      <NRadioButton value="never">{{ t('sharing.never') }}</NRadioButton>
    </NRadioGroup>
    <div v-if="mode === 'schedule'" class="windows">
      <div v-for="(w, i) in windows" :key="i" class="win">
        <div class="days">
          <button
            v-for="d in 7"
            :key="d"
            type="button"
            class="day"
            :class="{ on: w.days.includes(d - 1) }"
            @click="toggleDay(w, d - 1)"
          >
            {{ t(`sharing.days.${d - 1}`) }}
          </button>
        </div>
        <span class="faint small">{{ t('sharing.from') }}</span>
        <NSelect v-model:value="w.start" size="small" :options="startOptions" class="time" />
        <span class="faint small">{{ t('sharing.to') }}</span>
        <NSelect v-model:value="w.end" size="small" :options="endOptions" class="time" />
        <span v-if="overnight(w)" class="faint small">{{ t('sharing.nextDay') }}</span>
        <NButton size="tiny" quaternary @click="windows.splice(i, 1)"><Trash2 :size="13" /></NButton>
      </div>
      <div class="win-actions">
        <NButton size="small" secondary @click="addWindow">
          <template #icon><Plus :size="14" /></template>
          {{ t('sharing.addWindow') }}
        </NButton>
        <NButton size="small" quaternary data-action="preset-nights" @click="usePreset">{{ t('sharing.preset') }}</NButton>
      </div>
      <p class="faint small hint">{{ t('sharing.timezone', { tz: node.timezone }) }}</p>
    </div>
    <div class="save-row">
      <NButton size="small" type="primary" :loading="busy" :disabled="!hoursValid" data-action="save-sharing" @click="saveHours">
        {{ t('sharing.save') }}
      </NButton>
    </div>
    <div v-if="conflicts.length" class="conflicts small">
      <p>{{ t('sharing.conflicts', { n: conflicts.length }) }}</p>
      <ul>
        <li v-for="c in conflicts" :key="c.id">{{ c.username }} · {{ formatRange(c.start_at, c.end_at) }}</li>
      </ul>
    </div>

    <h4 class="sub">{{ t('sharing.running') }}</h4>
    <template v-if="running">
      <table v-if="running.lanes.length" class="table small">
        <tbody>
          <tr v-for="l in running.lanes" :key="l.id">
            <td>{{ l.name }}<span v-if="l.gpus.length" class="faint"> · GPU {{ l.gpus.join(',') }}</span></td>
            <td>
              <span class="mono">{{ l.model_name ?? '—' }}</span>
              <span v-if="l.reason" class="faint"> · {{ t(`admin.runtimeView.reason.${l.reason}`) }}</span>
            </td>
            <td class="right">
              <NButton v-if="idleBusy(l.reason, l.state)" size="tiny" secondary @click="stopLane(l.id)">
                {{ t('sharing.stopLane') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!running.bookings.length && !running.sessions.length" class="faint small">{{ t('sharing.nothing') }}</div>
      <table v-else class="table small">
        <tbody>
          <tr v-for="b in running.bookings" :key="b.id" :data-booking="b.id">
            <td>
              <span class="badge" :class="b.active ? 'ok' : ''">{{ b.active ? t('sharing.active') : t('sharing.upcoming') }}</span>
            </td>
            <td>{{ b.username }}</td>
            <td class="nowrap">{{ formatRange(b.start_at, b.end_at) }}</td>
            <td>
              {{ t(`sharing.kinds.${b.kind}`) }}<span v-if="b.model_name" class="mono"> · {{ b.model_name }}</span>
              <span v-if="running.lanes.length > 1" class="faint"> · {{ laneName(b.lane_id) }}</span>
            </td>
            <td class="right">
              <NButton size="tiny" secondary type="error" @click="stopBooking(b)">{{ t('sharing.stop') }}</NButton>
            </td>
          </tr>
          <tr v-for="s in running.sessions.filter((x) => x.kind === 'shared')" :key="`s${s.id}`">
            <td><span class="badge ok">{{ s.state }}</span></td>
            <td colspan="2">{{ t('sharing.sharedEnv') }}</td>
            <td class="faint">{{ laneName(s.lane_id) }}</td>
            <td class="right">
              <NButton v-if="s.lane_id" size="tiny" secondary @click="stopLane(s.lane_id)">{{ t('sharing.stopLane') }}</NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-if="!node.is_local">
      <h4 class="sub">{{ t('sharing.wol') }}</h4>
      <p class="faint small hint">{{ t('sharing.wolHint') }}</p>
      <div class="wol">
        <label>
          <span class="field-label">{{ t('sharing.macs') }}</span>
          <NInput v-model:value="macs" size="small" maxlength="255" placeholder="aa:bb:cc:dd:ee:ff" data-field="macs" />
          <span class="faint small">{{ t('sharing.macsHint') }}</span>
        </label>
        <label>
          <span class="field-label">{{ t('sharing.network') }}</span>
          <NInput v-model:value="network" size="small" maxlength="64" placeholder="192.168.1.0/24" />
          <span class="faint small">{{ t('sharing.networkHint') }}</span>
        </label>
      </div>
      <div class="save-row">
        <span v-if="node.wol_macs.length" class="faint small mono">{{ t('sharing.reported', { macs: node.wol_macs.join(', ') }) }}</span>
        <span v-if="node.woken_at" class="faint small">· {{ t('sharing.lastWoken', { when: relativeFromNow(node.woken_at) }) }}</span>
        <span class="spacer" />
        <NButton size="small" secondary :loading="busy" @click="saveWol">{{ t('common.save') }}</NButton>
        <NButton size="small" :disabled="node.online || !node.wol_macs.length" :loading="busy" data-action="wake" @click="wake">
          <template #icon><Power :size="14" /></template>
          {{ t('sharing.wake') }}
        </NButton>
      </div>
    </template>

    <template v-if="contribution">
      <h4 class="sub">{{ t('sharing.contribution') }}</h4>
      <div class="stats">
        <div><span class="faint small">{{ t('sharing.onlineHours') }}</span><strong class="num">{{ hours(contribution.online_hours) }}</strong></div>
        <div><span class="faint small">{{ t('sharing.sharedHours') }}</span><strong class="num">{{ hours(contribution.shared_hours) }}</strong></div>
        <div><span class="faint small">{{ t('sharing.gpuOffered') }}</span><strong class="num">{{ hours(contribution.gpu_hours_shared) }}</strong></div>
        <div><span class="faint small">{{ t('sharing.usedByOthers') }}</span><strong class="num">{{ hours(contribution.gpu_hours_used_by_others) }}</strong></div>
        <div><span class="faint small">{{ t('sharing.usedByOwner') }}</span><strong class="num">{{ hours(contribution.gpu_hours_used_by_owner) }}</strong></div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.small {
  font-size: 12.5px;
}
.nowrap {
  white-space: nowrap;
}
.hint {
  margin: 6px 0 0;
}
.sub {
  margin: 18px 0 8px;
  font-size: 13.5px;
}
.windows {
  margin-top: 10px;
  display: grid;
  gap: 8px;
}
.win {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.days {
  display: inline-flex;
  gap: 2px;
}
.day {
  border: 1px solid var(--border, #d4d4d8);
  background: transparent;
  color: var(--text-2);
  border-radius: 6px;
  padding: 2px 6px;
  font-size: 12px;
  cursor: pointer;
}
.day.on {
  background: var(--brand, #4f46e5);
  border-color: var(--brand, #4f46e5);
  color: #fff;
}
.time {
  width: 92px;
}
.win-actions,
.save-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.conflicts {
  margin-top: 8px;
  color: var(--warning, #b45309);
}
.conflicts ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.wol {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.wol label {
  display: grid;
  gap: 4px;
}
.field-label {
  font-size: 13px;
  color: var(--text-2);
}
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}
.stats div {
  display: grid;
  gap: 2px;
}
</style>
