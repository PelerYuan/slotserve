<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NFormItem, NModal, NSelect, useMessage } from 'naive-ui'
import { useLanes } from '@/composables/lanes'
import { CalendarClock, Boxes, Coins, KeyRound, Activity, Users } from 'lucide-vue-next'
import LogDrawer from '@/components/LogDrawer.vue'
import StatusDot from '@/components/StatusDot.vue'
import { api } from '@/api/client'
import type { AdminStats, Backup, Model } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useStatus } from '@/composables/useStatus'
import { formatBytes, formatCompact, formatDateTime, formatDuration, formatNumber, formatTime } from '@/utils/format'

const emit = defineEmits<{ navigate: [tab: 'users' | 'reservations' | 'keys' | 'runtime'] }>()
const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const { status, refresh } = useStatus()

const stats = ref<AdminStats | null>(null)
const logs = ref(false)
const restarting = ref(false)

const backups = ref<Backup[]>([])
const backingUp = ref(false)

async function load() {
  try {
    ;[stats.value, backups.value] = await Promise.all([api.admin.stats(), api.admin.backups()])
  } catch (e) {
    showError(e)
  }
}

async function backupNow() {
  backingUp.value = true
  try {
    await api.admin.createBackup()
    backups.value = await api.admin.backups()
    message.success(t('admin.backups.done'))
  } catch (e) {
    showError(e)
  } finally {
    backingUp.value = false
  }
}
onMounted(load)

const { lanes, multi } = useLanes()
const laneId = ref<number | null>(null)
const laneOptions = computed(() => lanes.value.map((l) => ({ label: l.name, value: l.id })))
const lane = computed(
  () => status.value?.lanes.find((l) => l.id === laneId.value) ?? status.value?.lanes[0] ?? null,
)
const runtime = computed(() => lane.value?.runtime ?? status.value?.runtime)

const tiles = computed(() => {
  const s = stats.value
  if (!s) return []
  return [
    {
      icon: Users,
      label: t('admin.stats.users'),
      value: formatNumber(s.users),
      sub: t('admin.stats.usersSub', { admins: s.admins, disabled: s.disabled_users }),
      to: 'users' as const,
    },
    { icon: KeyRound, label: t('admin.stats.keys'), value: formatNumber(s.active_keys), to: 'keys' as const },
    {
      icon: CalendarClock,
      label: t('admin.stats.upcoming'),
      value: formatNumber(s.upcoming_reservations),
      sub: s.pending_reservations ? t('admin.stats.pendingSub', { n: s.pending_reservations }) : undefined,
      to: 'reservations' as const,
    },
    {
      icon: Activity,
      label: t('admin.stats.requests'),
      value: formatNumber(s.requests_24h),
      sub: s.failed_requests_24h ? t('admin.stats.requestsSub', { n: s.failed_requests_24h }) : undefined,
    },
    { icon: Coins, label: t('admin.stats.tokens'), value: formatCompact(s.tokens_24h) },
    { icon: Boxes, label: t('admin.stats.models'), value: formatNumber(s.models) },
  ]
})

async function restart() {
  restarting.value = true
  try {
    await api.restart(lane.value?.id)
    message.success(t('overview.restartDone'))
    refresh()
  } catch (e) {
    showError(e)
  } finally {
    restarting.value = false
  }
}

// ---- manual control ----------------------------------------------------------
const manual = computed(() => lane.value?.manual ?? null)
const loadForm = ref({ open: false, modelId: null as number | null, presetId: null as number | null, hours: 0 })
const models = ref<Model[]>([])
const busyManual = ref(false)
const readyModels = computed(() => models.value.filter((m) => m.status === 'ready' && m.file_exists))
const modelOptions = computed(() => readyModels.value.map((m) => ({ label: m.name, value: m.id })))
const presetOptions = computed(() => [
  { label: t('schedule.presetDefault'), value: null as unknown as number },
  ...(readyModels.value.find((m) => m.id === loadForm.value.modelId)?.presets ?? []).map((p) => ({
    label: p.name,
    value: p.id,
  })),
])
const durationOptions = computed(() => [
  { label: t('admin.manual.untilNext'), value: 0 },
  ...[1, 2, 4, 8, 24].map((h) => ({ label: t('admin.manual.hours', { n: h }), value: h })),
])

async function openLoad() {
  models.value = await api.models().catch(() => [])
  loadForm.value = { open: true, modelId: readyModels.value[0]?.id ?? null, presetId: null, hours: 0 }
}

async function manualAction(action: 'load' | 'unload' | 'auto') {
  busyManual.value = true
  try {
    await api.admin.manual({
      action,
      lane_id: lane.value?.id ?? null,
      model_id: action === 'load' ? loadForm.value.modelId ?? undefined : undefined,
      preset_id: action === 'load' ? loadForm.value.presetId : undefined,
      hours: action === 'auto' ? undefined : loadForm.value.hours || null,
    })
    loadForm.value.open = false
    message.success(t('admin.manual.done'))
    window.setTimeout(refresh, 500)
  } catch (e) {
    showError(e)
  } finally {
    busyManual.value = false
  }
}

const seconds = (a: string, b: string) => (new Date(b).getTime() - new Date(a).getTime()) / 1000
</script>

<template>
  <div class="grid">
    <component
      :is="tile.to ? 'button' : 'div'"
      v-for="tile in tiles"
      :key="tile.label"
      class="card tile"
      :class="{ link: tile.to }"
      @click="tile.to && emit('navigate', tile.to)"
    >
      <div class="tile-head">
        <span class="label">{{ tile.label }}</span>
        <component :is="tile.icon" :size="16" :stroke-width="1.8" class="tile-icon" />
      </div>
      <div class="tile-value num">{{ tile.value }}</div>
      <div class="faint tile-sub">{{ tile.sub ?? ' ' }}</div>
    </component>
  </div>

  <section class="card runtime">
    <div class="card-header">
      <div class="row">
        <h3 class="card-title">{{ t('admin.runtime') }}</h3>
        <NSelect
          v-if="multi"
          :value="lane?.id ?? null"
          :options="laneOptions"
          size="small"
          class="lane-select"
          @update:value="(v: number) => (laneId = v)"
        />
        <StatusDot v-if="runtime" :state="runtime.state" />
        <span v-if="runtime?.model_name" class="muted mono">{{ runtime.model_name }}</span>
      </div>
      <div class="row wrap">
        <NButton size="small" secondary type="primary" @click="openLoad">{{ t('admin.manual.load') }}</NButton>
        <NButton size="small" secondary :loading="busyManual" @click="loadForm.hours = 0; manualAction('unload')">
          {{ t('admin.manual.unload') }}
        </NButton>
        <NButton v-if="manual" size="small" secondary :loading="busyManual" @click="manualAction('auto')">
          {{ t('admin.manual.auto') }}
        </NButton>
        <NButton size="small" secondary @click="emit('navigate', 'runtime')">{{ t('admin.runtimeView.open') }}</NButton>
        <NButton size="small" secondary :loading="restarting" :disabled="!runtime?.model_name" @click="restart">
          {{ t('admin.restart') }}
        </NButton>
      </div>
    </div>
    <div v-if="manual" class="manual-note">
      {{ manual.model_name ? t('overview.manualLoad', { user: manual.by, model: manual.model_name }) : t('overview.manualUnload', { user: manual.by }) }}
      {{ manual.until ? t('overview.manualUntil', { time: formatTime(manual.until) }) : t('overview.manualUntilNext') }}
    </div>
    <div v-if="runtime?.error" class="runtime-error mono">{{ runtime.error }}</div>
  </section>

  <NModal
    v-model:show="loadForm.open"
    preset="card"
    :title="t('admin.manual.load')"
    :bordered="false"
    style="width: 440px; max-width: calc(100vw - 32px)"
  >
    <p class="muted hint">{{ t('admin.manual.hint') }}</p>
    <NFormItem :label="t('schedule.model')">
      <NSelect v-model:value="loadForm.modelId" :options="modelOptions" filterable />
    </NFormItem>
    <NFormItem :label="t('schedule.preset')">
      <NSelect v-model:value="loadForm.presetId" :options="presetOptions" />
    </NFormItem>
    <NFormItem :label="t('admin.manual.duration')">
      <NSelect v-model:value="loadForm.hours" :options="durationOptions" />
    </NFormItem>
    <template #footer>
      <div class="footer">
        <NButton @click="loadForm.open = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!loadForm.modelId" :loading="busyManual" @click="manualAction('load')">
          {{ t('admin.manual.loadNow') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <section class="card">
    <div class="card-header">
      <h3 class="card-title">{{ t('admin.loadEvents') }}</h3>
    </div>
    <div v-if="stats && !stats.load_events.length" class="empty">{{ t('admin.loadEventsEmpty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.model') }}</th>
            <th>{{ t('admin.started') }}</th>
            <th>{{ t('admin.took') }}</th>
            <th>{{ t('admin.result') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in stats?.load_events ?? []" :key="e.id">
            <td class="mono">{{ e.model_name ?? '—' }}</td>
            <td class="muted num">{{ formatDateTime(e.started_at) }}</td>
            <td class="muted num">{{ formatDuration(seconds(e.started_at, e.finished_at)) }}</td>
            <td>
              <span v-if="e.success" class="badge ok">{{ t('admin.success') }}</span>
              <span v-else class="badge danger" :title="e.error ?? ''">{{ t('admin.failed') }}</span>
              <span v-if="!e.success && e.error" class="faint err">{{ e.error }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card backups">
    <div class="card-header">
      <div>
        <h3 class="card-title">{{ t('admin.backups.title') }}</h3>
        <div class="card-sub">{{ t('admin.backups.subtitle') }}</div>
      </div>
      <NButton size="small" secondary :loading="backingUp" @click="backupNow">{{ t('admin.backups.now') }}</NButton>
    </div>
    <div v-if="!backups.length" class="empty">{{ t('admin.backups.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <tbody>
          <tr v-for="b in backups.slice(0, 10)" :key="b.name">
            <td class="muted num nowrap">{{ formatDateTime(b.created_at) }}</td>
            <td><span class="badge" :class="{ brand: b.kind === 'manual' }">{{ t(`admin.backups.kinds.${b.kind}`) }}</span></td>
            <td class="mono small">{{ b.name }}</td>
            <td class="right num muted nowrap">{{ formatBytes(b.size_bytes) }}</td>
            <td class="right">
              <a :href="api.admin.backupUrl(b.name)" download>{{ t('admin.backups.download') }}</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <LogDrawer v-model:show="logs" :lane-id="lane?.id" />
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.tile {
  text-align: left;
  font: inherit;
  color: inherit;
  padding: 16px 18px 14px;
  transition: border-color 0.2s, box-shadow 0.2s, transform 0.2s;
}
.tile.link {
  cursor: pointer;
}
.tile.link:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-lg);
  transform: translateY(-1px);
}
.tile-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tile-head .label {
  text-transform: none;
  letter-spacing: 0;
  font-size: 12.5px;
}
.tile-icon {
  color: var(--text-3);
}
.tile-value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  margin: 6px 0 2px;
}
.tile-sub {
  font-size: 12px;
}
.runtime {
  margin-bottom: 16px;
}
.runtime .card-header {
  border-bottom: none;
  flex-wrap: wrap;
}
.wrap {
  flex-wrap: wrap;
}
.lane-select {
  width: 150px;
}
.backups {
  margin-top: 16px;
}
.nowrap {
  white-space: nowrap;
}
.small {
  font-size: 12px;
}
.manual-note {
  margin: 0 20px 16px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12.5px;
}
.hint {
  margin: 0 0 16px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.runtime-error {
  margin: 0 20px 16px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
  white-space: pre-wrap;
}
.err {
  margin-left: 8px;
  font-size: 12px;
}
</style>
