<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NRadioButton, NRadioGroup, NSelect, useDialog } from 'naive-ui'
import { api } from '@/api/client'
import type { AdminUser, Reservation } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useStatus } from '@/composables/useStatus'
import { formatRange } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()
const { refresh: refreshStatus } = useStatus()

const PHASES = ['upcoming', 'active', 'finished', 'cancelled'] as const
const phase = ref<string>('all')
const userId = ref<number | null>(null)
const users = ref<AdminUser[]>([])
const rows = ref<Reservation[]>([])
const loading = ref(false)

const userOptions = computed(() => users.value.map((u) => ({ label: u.username, value: u.id })))

async function load() {
  loading.value = true
  try {
    rows.value = await api.admin.reservations(phase.value === 'all' ? undefined : phase.value, userId.value ?? undefined)
  } catch (e) {
    showError(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  users.value = await api.admin.users().catch(() => [])
  await load()
})
watch([phase, userId], load)

const tone: Record<string, string> = { upcoming: 'brand', active: 'ok', finished: '', cancelled: 'danger' }

function end(r: Reservation) {
  const active = r.phase === 'active'
  dialog.warning({
    title: active ? t('admin.reservations.release') : t('admin.reservations.cancel'),
    content: active
      ? t('admin.reservations.releaseConfirm', { name: r.username })
      : t('admin.reservations.cancelConfirm', { name: r.username, time: formatRange(r.start_at, r.end_at) }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.endReservation(r.id)
        refreshStatus()
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <div class="toolbar">
    <NRadioGroup v-model:value="phase" size="small">
      <NRadioButton value="all">{{ t('admin.reservations.all') }}</NRadioButton>
      <NRadioButton v-for="p in PHASES" :key="p" :value="p">{{ t(`phase.${p}`) }}</NRadioButton>
    </NRadioGroup>
    <NSelect
      v-model:value="userId"
      :options="userOptions"
      :placeholder="t('admin.reservations.allUsers')"
      clearable
      size="small"
      class="user-select"
    />
  </div>

  <section class="card">
    <div v-if="!loading && !rows.length" class="empty">{{ t('admin.reservations.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.reservations.user') }}</th>
            <th>{{ t('admin.reservations.model') }}</th>
            <th>{{ t('admin.reservations.time') }}</th>
            <th>{{ t('admin.reservations.status') }}</th>
            <th>{{ t('admin.reservations.note') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td class="strong">{{ r.username }}</td>
            <td class="mono small">{{ r.model_name }}</td>
            <td class="muted num nowrap">{{ formatRange(r.start_at, r.end_at) }}</td>
            <td><span class="badge" :class="tone[r.phase]">{{ t(`phase.${r.phase}`) }}</span></td>
            <td class="muted note" :title="r.note">{{ r.note || '—' }}</td>
            <td class="right">
              <NButton
                v-if="r.phase === 'upcoming' || r.phase === 'active'"
                size="small"
                quaternary
                :type="r.phase === 'active' ? 'warning' : 'error'"
                @click="end(r)"
              >
                {{ r.phase === 'active' ? t('admin.reservations.release') : t('admin.reservations.cancel') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: space-between;
  margin-bottom: 12px;
}
.user-select {
  width: 180px;
}
.strong {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.nowrap {
  white-space: nowrap;
}
.note {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
