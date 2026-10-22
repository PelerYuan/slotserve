<script setup lang="ts">
// "My work" in the Overview's next-step slot (F01): what runs now or starts within 24 hours. Environments use the
// environment card's row (features/devenv/WorkItem.vue: name, state, where, time left, Open / Copy ssh); bookings
// without an environment get a row of the same shape here. A running booking adds [+1 h].
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { Boxes, Lock } from 'lucide-vue-next'
import WorkItem from '@/features/devenv/WorkItem.vue'
import { api } from '@/api/client'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useNow } from '@/composables/useNow'
import { formatDuration, relativeFromNow } from '@/utils/format'
import { reservationWhat } from '@/utils/reservation'
import type { WorkRow } from '../work'

defineProps<{ rows: WorkRow[] }>()
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const { showError, message } = useErrorMessage()
const now = useNow(15_000)
const busy = ref<string | null>(null)

function title(row: WorkRow): string {
  if (row.env) return row.env.project_name ?? row.env.template_name ?? t('onboarding.work.env')
  return row.booking ? reservationWhat(row.booking, t) : ''
}
function where(row: WorkRow): string {
  return row.env?.lane_name ?? row.booking?.lane_name ?? ''
}
function when(row: WorkRow): string {
  if (row.booking?.phase === 'pending') return t('onboarding.work.pending')
  if (row.env && row.env.state !== 'running' && row.active) return t('onboarding.work.starting')
  if (!row.active && row.start) return t('onboarding.work.startsIn', { when: relativeFromNow(row.start) })
  if (row.end) return t('onboarding.work.left', { time: formatDuration((new Date(row.end).getTime() - now.value) / 1000) })
  return ''
}
function icon(row: WorkRow) {
  return row.booking?.exclusive ? Lock : Boxes
}

async function extend(row: WorkRow) {
  if (!row.booking) return
  busy.value = row.key
  try {
    await api.extendReservation(row.booking.id, 60)
    message.success(t('onboarding.work.extended'))
    emit('changed')
  } catch (e) {
    showError(e)
  } finally {
    busy.value = null
  }
}

</script>

<template>
  <ul class="work" data-component="my-work">
    <li v-for="row in rows" :key="row.key" class="work-row" :data-row="row.key">
      <WorkItem v-if="row.env" :session="row.env" class="w-env" />
      <template v-else>
        <span class="w-icon" :class="{ active: row.active }"><component :is="icon(row)" :size="16" /></span>
        <span class="w-main">
          <span class="w-title">{{ title(row) }}</span>
          <span class="w-meta faint">
            <span v-if="where(row)">{{ where(row) }}</span>
            <span v-if="where(row) && when(row)">·</span>
            <span :class="{ pending: row.booking?.phase === 'pending' }">{{ when(row) }}</span>
          </span>
        </span>
      </template>
      <span v-if="row.booking && row.booking.phase === 'active'" class="w-actions">
        <NButton size="small" secondary :loading="busy === row.key" data-action="work-extend" @click="extend(row)">
          {{ t('onboarding.work.extend') }}
        </NButton>
      </span>
    </li>
  </ul>
</template>

<style scoped>
.work {
  list-style: none;
  margin: 0;
  padding: 0;
}
.work-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
}
.work-row + .work-row {
  border-top: 1px solid var(--border);
}
.w-env {
  flex: 1;
  min-width: 0;
}
.w-icon {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  display: grid;
  place-items: center;
  background: var(--neutral-soft);
  color: var(--text-2);
}
.w-icon.active {
  background: var(--brand-soft);
  color: var(--brand);
}
.w-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.w-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.w-meta {
  display: flex;
  gap: 6px;
  font-size: 12.5px;
  flex-wrap: wrap;
}
.pending {
  color: var(--warn-text);
}
.w-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
@media (max-width: 640px) {
  .work-row {
    flex-wrap: wrap;
  }
  .w-actions {
    width: 100%;
    justify-content: flex-start;
    padding-left: 44px;
  }
}
</style>
