<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NRadioButton, NRadioGroup, NSelect, useDialog } from 'naive-ui'
import { Plus } from 'lucide-vue-next'
import BookingModal from '@/components/BookingModal.vue'
import { api } from '@/api/client'
import type { AdminUser, Model, Reservation } from '@/api/types'
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

const models = ref<Model[]>([])
const modal = ref(false)
const editing = ref<Reservation | null>(null)
const draftStart = ref<Date | null>(null)
const draftEnd = ref<Date | null>(null)

function openNew() {
  const slot = 30 * 60_000
  const s = new Date(Math.ceil(Date.now() / slot) * slot)
  draftStart.value = s
  draftEnd.value = new Date(s.getTime() + 3_600_000)
  editing.value = null
  modal.value = true
}

function openEdit(r: Reservation) {
  editing.value = r
  modal.value = true
}

async function onDone() {
  refreshStatus()
  await load()
}

const what = (r: Reservation) =>
  r.exclusive ? t('schedule.kindExclusive') : r.preset_name ? `${r.model_name} · ${r.preset_name}` : r.model_name

onMounted(async () => {
  ;[users.value, models.value] = await Promise.all([
    api.admin.users().catch(() => []),
    api.models().catch(() => []),
  ])
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
    <div class="row">
      <NSelect
        v-model:value="userId"
        :options="userOptions"
        :placeholder="t('admin.reservations.allUsers')"
        clearable
        size="small"
        class="user-select"
      />
      <NButton size="small" type="primary" @click="openNew">
        <template #icon><Plus :size="14" /></template>
        {{ t('admin.reservations.bookFor') }}
      </NButton>
    </div>
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
            <td :class="{ mono: !r.exclusive }" class="small">
              {{ what(r) }}<span v-if="r.series_id" class="faint"> ↻</span>
            </td>
            <td class="muted num nowrap">{{ formatRange(r.start_at, r.end_at) }}</td>
            <td><span class="badge" :class="tone[r.phase]">{{ t(`phase.${r.phase}`) }}</span></td>
            <td class="muted note" :title="r.note">{{ r.note || '—' }}</td>
            <td class="right nowrap">
              <NButton v-if="r.phase === 'upcoming' || r.phase === 'active'" size="small" quaternary @click="openEdit(r)">
                {{ t('common.edit') }}
              </NButton>
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

  <BookingModal
    v-model:show="modal"
    :start="draftStart"
    :end="draftEnd"
    :models="models"
    :reservations="rows"
    :editing="editing"
    @done="onDone"
  />
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
