<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useMessage } from 'naive-ui'
import { CalendarClock, Boxes, Coins, KeyRound, Activity, Users } from 'lucide-vue-next'
import LogDrawer from '@/components/LogDrawer.vue'
import StatusDot from '@/components/StatusDot.vue'
import { api } from '@/api/client'
import type { AdminStats } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useStatus } from '@/composables/useStatus'
import { formatCompact, formatDateTime, formatDuration, formatNumber } from '@/utils/format'

const emit = defineEmits<{ navigate: [tab: 'users' | 'reservations' | 'keys'] }>()
const { t } = useI18n()
const message = useMessage()
const { showError } = useErrorMessage()
const { status, refresh } = useStatus()

const stats = ref<AdminStats | null>(null)
const logs = ref(false)
const restarting = ref(false)

async function load() {
  try {
    stats.value = await api.admin.stats()
  } catch (e) {
    showError(e)
  }
}
onMounted(load)

const runtime = computed(() => status.value?.runtime)

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
    await api.restart()
    message.success(t('overview.restartDone'))
    refresh()
  } catch (e) {
    showError(e)
  } finally {
    restarting.value = false
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
        <StatusDot v-if="runtime" :state="runtime.state" />
        <span v-if="runtime?.model_name" class="muted mono">{{ runtime.model_name }}</span>
      </div>
      <div class="row">
        <NButton size="small" secondary @click="logs = true">{{ t('admin.viewLogs') }}</NButton>
        <NButton size="small" secondary :loading="restarting" :disabled="!runtime?.model_name" @click="restart">
          {{ t('admin.restart') }}
        </NButton>
      </div>
    </div>
    <div v-if="runtime?.error" class="runtime-error mono">{{ runtime.error }}</div>
  </section>

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

  <LogDrawer v-model:show="logs" />
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
