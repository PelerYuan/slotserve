<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NDatePicker, NForm, NFormItem, NInput, NModal, NSelect } from 'naive-ui'
import { Info, TriangleAlert } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Model, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, formatDuration } from '@/utils/format'

const show = defineModel<boolean>('show', { default: false })
const props = defineProps<{
  start: Date | null
  end: Date | null
  models: Model[]
  reservations: Reservation[]
}>()
const emit = defineEmits<{ booked: [Reservation] }>()

const { t } = useI18n()
const { describe } = useErrorMessage()

const modelId = ref<number | null>(null)
const startTs = ref<number | null>(null)
const endTs = ref<number | null>(null)
const note = ref('')
const busy = ref(false)
const error = ref('')

const ready = computed(() => props.models.filter((m) => m.status === 'ready' && m.file_exists))
const options = computed(() =>
  ready.value.map((m) => ({ label: `${m.name}  ·  ${formatBytes(m.size_bytes)}`, value: m.id })),
)
const selected = computed(() => ready.value.find((m) => m.id === modelId.value) ?? null)

watch(show, (open) => {
  if (!open) return
  error.value = ''
  note.value = ''
  startTs.value = props.start?.getTime() ?? null
  endTs.value = props.end?.getTime() ?? null
  if (!ready.value.some((m) => m.id === modelId.value)) {
    modelId.value = (ready.value.find((m) => m.is_default) ?? ready.value[0])?.id ?? null
  }
})

const durationSeconds = computed(() =>
  startTs.value && endTs.value ? Math.max(0, (endTs.value - Math.max(startTs.value, Date.now())) / 1000) : 0,
)

const previous = computed(() => {
  if (!startTs.value) return null
  return (
    props.reservations
      .filter((r) => r.status === 'booked' && Math.abs(new Date(r.end_at).getTime() - startTs.value!) < 60_000)
      .sort((a, b) => b.end_at.localeCompare(a.end_at))[0] ?? null
  )
})

const loadHint = computed(() => {
  if (!selected.value) return null
  if (previous.value && previous.value.model_id === selected.value.id) return { kind: 'same' as const }
  if (previous.value) return { kind: 'adjacent' as const }
  return { kind: 'preload' as const }
})

const slotMs = computed(() => (session.config?.slot_minutes ?? 30) * 60_000)
const canSubmit = computed(() => !!modelId.value && !!startTs.value && !!endTs.value && endTs.value > startTs.value)

function disablePast(ts: number) {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return ts < d.getTime()
}

function startNow() {
  const now = Date.now()
  const span = endTs.value && startTs.value ? endTs.value - startTs.value : 3_600_000
  startTs.value = now
  endTs.value = Math.ceil((now + span) / slotMs.value) * slotMs.value
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    const r = await api.createReservation(modelId.value!, new Date(startTs.value!), new Date(endTs.value!), note.value)
    emit('booked', r)
    show.value = false
  } catch (e) {
    error.value = describe(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NModal v-model:show="show" preset="card" :title="t('schedule.bookingTitle')" style="width: 520px; max-width: calc(100vw - 32px)" :bordered="false">
    <div v-if="!ready.length" class="muted">{{ t('schedule.noModels') }}</div>
    <NForm v-else :show-require-mark="false" @submit.prevent="submit">
      <NFormItem :label="t('schedule.model')">
        <NSelect v-model:value="modelId" :options="options" filterable />
      </NFormItem>
      <div class="two">
        <NFormItem :label="t('schedule.start')">
          <NDatePicker
            v-model:value="startTs"
            type="datetime"
            format="yyyy-MM-dd HH:mm"
            :time-picker-props="{ format: 'HH:mm', minutes: session.config?.slot_minutes ?? 30 }"
            :is-date-disabled="disablePast"
            style="width: 100%"
          />
        </NFormItem>
        <NFormItem :label="t('schedule.end')">
          <NDatePicker
            v-model:value="endTs"
            type="datetime"
            format="yyyy-MM-dd HH:mm"
            :time-picker-props="{ format: 'HH:mm', minutes: session.config?.slot_minutes ?? 30 }"
            :is-date-disabled="disablePast"
            style="width: 100%"
          />
        </NFormItem>
      </div>
      <div class="quick">
        <NButton text type="primary" size="small" @click="startNow">{{ t('schedule.startNow') }}</NButton>
        <span class="faint small">
          {{ t('schedule.limits', { hours: session.config?.max_reservation_hours ?? 8, days: session.config?.max_advance_days ?? 30 }) }}
        </span>
      </div>
      <NFormItem :label="t('schedule.note')">
        <NInput v-model:value="note" :placeholder="t('schedule.notePlaceholder')" maxlength="500" />
      </NFormItem>

      <div v-if="selected" class="facts">
        <div>
          <div class="faint small">{{ t('schedule.duration') }}</div>
          <div class="fact num">{{ formatDuration(durationSeconds) }}</div>
        </div>
        <div>
          <div class="faint small">{{ t('schedule.estLoad') }}</div>
          <div class="fact num">{{ formatDuration(selected.est_load_seconds) }}</div>
        </div>
        <div>
          <div class="faint small">{{ t('models.size') }}</div>
          <div class="fact num">{{ formatBytes(selected.size_bytes) }}</div>
        </div>
      </div>
      <div v-if="loadHint" class="hint" :class="loadHint.kind">
        <TriangleAlert v-if="loadHint.kind === 'adjacent'" :size="15" />
        <Info v-else :size="15" />
        <span v-if="loadHint.kind === 'adjacent'">{{ t('schedule.adjacentWarning', { eta: formatDuration(selected?.est_load_seconds) }) }}</span>
        <span v-else-if="loadHint.kind === 'same'">{{ t('schedule.sameModelNote') }}</span>
        <span v-else>{{ t('schedule.preloadInfo') }}</span>
      </div>

      <NAlert v-if="error" type="error" :show-icon="false" class="err">{{ error }}</NAlert>
    </NForm>

    <template #footer>
      <div class="footer">
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!canSubmit" :loading="busy" @click="submit">{{ t('schedule.newBooking') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.quick {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: -12px 0 14px;
  gap: 12px;
}
.small {
  font-size: 12.5px;
}
.facts {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 14px 16px;
  border-radius: 10px;
  background: var(--surface-muted);
  border: 1px solid var(--border);
}
.fact {
  font-weight: 600;
  margin-top: 2px;
}
.hint {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-top: 12px;
  font-size: 12.5px;
  color: var(--text-2);
}
.hint svg {
  flex: none;
  margin-top: 2px;
}
.hint.adjacent {
  color: #b45309;
}
.err {
  margin-top: 14px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 520px) {
  .two {
    grid-template-columns: 1fr;
  }
}
</style>
