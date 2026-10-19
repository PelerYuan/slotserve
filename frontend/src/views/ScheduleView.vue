<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NButtonGroup, NModal, useDialog } from 'naive-ui'
import { CalendarPlus, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import WeekCalendar from '@/components/WeekCalendar.vue'
import BookingModal from '@/components/BookingModal.vue'
import { api } from '@/api/client'
import type { Model, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useNow } from '@/composables/useNow'
import { useStatus } from '@/composables/useStatus'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDate, formatRange, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()
const { refresh: refreshStatus } = useStatus()
const now = useNow(30_000)

const narrow = ref(window.innerWidth < 760)
const days = computed(() => (narrow.value ? 3 : 7))
function onResize() {
  narrow.value = window.innerWidth < 760
}

function startOfDay(d: Date) {
  const x = new Date(d)
  x.setHours(0, 0, 0, 0)
  return x
}
function startOfWeek(d: Date) {
  const x = startOfDay(d)
  const dow = (x.getDay() + 6) % 7 // Monday = 0
  x.setDate(x.getDate() - dow)
  return x
}

const anchor = ref(new Date())
const rangeStart = computed(() => (narrow.value ? startOfDay(anchor.value) : startOfWeek(anchor.value)))
const rangeEnd = computed(() => {
  const x = new Date(rangeStart.value)
  x.setDate(x.getDate() + days.value)
  return x
})

const reservations = ref<Reservation[]>([])
const mine = ref<Reservation[]>([])
const models = ref<Model[]>([])
const slotMinutes = computed(() => session.config?.slot_minutes ?? 30)

async function load() {
  try {
    const horizon = new Date(Date.now() + (session.config?.max_advance_days ?? 30) * 86_400_000)
    const [all, my] = await Promise.all([
      api.reservations(rangeStart.value, rangeEnd.value),
      api.reservations(new Date(Date.now() - 86_400_000), horizon, true),
    ])
    reservations.value = all
    mine.value = my.filter((r) => r.phase === 'upcoming' || r.phase === 'active')
  } catch (e) {
    showError(e)
  }
}

let timer: number | undefined
onMounted(async () => {
  window.addEventListener('resize', onResize)
  models.value = await api.models().catch(() => [])
  await load()
  timer = window.setInterval(load, 30_000)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  window.clearInterval(timer)
})
watch(rangeStart, load)

function shift(n: number) {
  const x = new Date(anchor.value)
  x.setDate(x.getDate() + n * days.value)
  anchor.value = x
}

const rangeLabel = computed(() => {
  const last = new Date(rangeEnd.value.getTime() - 1)
  return `${formatDate(rangeStart.value)} – ${formatDate(last)}`
})

// ---- booking -----------------------------------------------------------------

const booking = ref(false)
const draftStart = ref<Date | null>(null)
const draftEnd = ref<Date | null>(null)

function openBooking(start?: Date, end?: Date) {
  const slot = slotMinutes.value * 60_000
  const s = start ?? new Date(Math.ceil(Date.now() / slot) * slot)
  draftStart.value = s
  draftEnd.value = end ?? new Date(s.getTime() + 3_600_000)
  booking.value = true
}

async function onBooked() {
  await load()
  refreshStatus()
}

// ---- details -----------------------------------------------------------------

const detail = ref<Reservation | null>(null)
const canManage = (r: Reservation) => r.user_id === session.user?.id || !!session.user?.is_admin

function endReservation(r: Reservation) {
  const active = r.phase === 'active' || new Date(r.start_at).getTime() <= Date.now()
  dialog.warning({
    title: active ? t('schedule.release') : t('schedule.cancel'),
    content: active ? t('schedule.confirmRelease') : t('schedule.confirmCancel'),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.endReservation(r.id)
        detail.value = null
        await load()
        refreshStatus()
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <div class="page wide">
    <div class="page-header">
      <div>
        <h1>{{ t('schedule.title') }}</h1>
        <p>{{ t('schedule.subtitle') }}</p>
      </div>
      <NButton type="primary" @click="openBooking()">
        <template #icon><CalendarPlus :size="16" /></template>
        {{ t('schedule.newBooking') }}
      </NButton>
    </div>

    <div class="layout">
      <section class="card cal-card">
        <div class="toolbar">
          <NButtonGroup size="small">
            <NButton @click="shift(-1)"><ChevronLeft :size="16" /></NButton>
            <NButton @click="anchor = new Date()">{{ t('schedule.today') }}</NButton>
            <NButton @click="shift(1)"><ChevronRight :size="16" /></NButton>
          </NButtonGroup>
          <div class="range num">{{ rangeLabel }}</div>
          <span class="spacer" />
          <div class="legend">
            <span class="sw mine" />{{ t('common.you') }}
            <span class="sw other" />{{ t('schedule.booked') }}
          </div>
        </div>
        <WeekCalendar
          :start="rangeStart"
          :days="days"
          :slot-minutes="slotMinutes"
          :reservations="reservations"
          :current-user-id="session.user?.id ?? null"
          :now="now"
          @select="openBooking"
          @open="detail = $event"
        />
      </section>

      <aside class="card mine-card">
        <div class="card-header">
          <h3 class="card-title">{{ t('schedule.myBookings') }}</h3>
        </div>
        <div v-if="!mine.length" class="empty">{{ t('schedule.noBookings') }}</div>
        <ul v-else class="mine">
          <li v-for="r in mine" :key="r.id" :class="{ active: r.phase === 'active' }">
            <div class="row">
              <span class="phase" :class="r.phase">{{ t(`phase.${r.phase}`) }}</span>
              <span class="spacer" />
              <span class="faint small">{{ relativeFromNow(r.phase === 'active' ? r.end_at : r.start_at) }}</span>
            </div>
            <div class="when num">{{ formatRange(r.start_at, r.end_at) }}</div>
            <div class="mono faint small">{{ r.model_name }}</div>
            <div v-if="r.note" class="small muted note">{{ r.note }}</div>
            <NButton size="tiny" secondary :type="r.phase === 'active' ? 'warning' : 'default'" class="end-btn" @click="endReservation(r)">
              {{ r.phase === 'active' ? t('schedule.release') : t('schedule.cancel') }}
            </NButton>
          </li>
        </ul>
      </aside>
    </div>

    <BookingModal v-model:show="booking" :start="draftStart" :end="draftEnd" :models="models" :reservations="reservations" @booked="onBooked" />

    <NModal
      :show="!!detail"
      preset="card"
      :title="detail?.username"
      style="width: 400px; max-width: calc(100vw - 32px)"
      :bordered="false"
      @update:show="(v: boolean) => !v && (detail = null)"
    >
      <template v-if="detail">
        <dl class="dl">
          <dt>{{ t('schedule.model') }}</dt>
          <dd class="mono">{{ detail.model_name }}</dd>
          <dt>{{ t('schedule.start') }} – {{ t('schedule.end') }}</dt>
          <dd class="num">{{ formatRange(detail.start_at, detail.end_at) }}</dd>
          <dt>{{ t('models.status') }}</dt>
          <dd>{{ t(`phase.${detail.phase}`) }}</dd>
          <template v-if="detail.note">
            <dt>{{ t('schedule.note') }}</dt>
            <dd>{{ detail.note }}</dd>
          </template>
        </dl>
      </template>
      <template v-if="detail && canManage(detail) && (detail.phase === 'upcoming' || detail.phase === 'active')" #footer>
        <div class="footer">
          <NButton :type="detail.phase === 'active' ? 'warning' : 'error'" secondary @click="endReservation(detail)">
            {{ detail.phase === 'active' ? t('schedule.release') : t('schedule.cancel') }}
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.page.wide {
  max-width: 1400px;
}
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 16px;
  align-items: start;
}
.cal-card {
  overflow: hidden;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.range {
  font-weight: 600;
}
.legend {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}
.sw {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  display: inline-block;
}
.sw.mine {
  background: var(--brand);
}
.sw.other {
  background: #a3a6b8;
  margin-left: 10px;
}
.mine {
  list-style: none;
  margin: 0;
  padding: 0;
}
.mine li {
  padding: 14px 20px;
  display: grid;
  gap: 3px;
}
.mine li + li {
  border-top: 1px solid var(--border);
}
.mine li.active {
  background: var(--brand-softer);
}
.phase {
  font-size: 11.5px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 5px;
  background: #f0f0f3;
  color: var(--text-2);
}
.phase.active {
  background: var(--brand-soft);
  color: var(--brand);
}
.when {
  font-weight: 600;
  margin-top: 6px;
}
.small {
  font-size: 12.5px;
}
.note {
  margin-top: 2px;
}
.end-btn {
  justify-self: start;
  margin-top: 8px;
}
.dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px 18px;
  margin: 0;
}
.dl dt {
  color: var(--text-3);
}
.dl dd {
  margin: 0;
  word-break: break-all;
}
.footer {
  display: flex;
  justify-content: flex-end;
}
@media (max-width: 1100px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
