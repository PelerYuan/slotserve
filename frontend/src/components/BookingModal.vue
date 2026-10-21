<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NAlert,
  NButton,
  NCheckbox,
  NDatePicker,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSwitch,
  useMessage,
} from 'naive-ui'
import { Cpu, Info, Terminal, TriangleAlert } from 'lucide-vue-next'
import { ApiError, api } from '@/api/client'
import type { AdminUser, BookingInput, DevTemplate, Model, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useLanes } from '@/composables/lanes'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, formatDuration } from '@/utils/format'

const show = defineModel<boolean>('show', { default: false })
const props = defineProps<{
  start: Date | null
  end: Date | null
  models: Model[]
  reservations: Reservation[]
  editing?: Reservation | null
  laneId?: number | null
  initialKind?: 'model' | 'exclusive' | 'devenv' | null
}>()
const emit = defineEmits<{ done: [] }>()

const { t } = useI18n()
const message = useMessage()
const { describe } = useErrorMessage()

const { lanes, multi } = useLanes()
const lane = ref<number | null>(null)
const laneOptions = computed(() =>
  lanes.value.map((l) => ({ label: l.gpus.length ? `${l.name}  ·  GPU ${l.gpus.join(',')}` : l.name, value: l.id })),
)
const kind = ref<'model' | 'exclusive' | 'devenv'>('model')
const devenvEnabled = computed(() => !!session.config?.devenv_enabled)
const templates = ref<DevTemplate[]>([])
const templateId = ref<number | null>(null)
const templateOptions = computed(() =>
  templates.value
    .filter((tpl) => tpl.enabled)
    .map((tpl) => ({ label: `${tpl.name}  ·  ${tpl.image}`, value: tpl.id })),
)
const template = computed(() => templates.value.find((tpl) => tpl.id === templateId.value) ?? null)
const modelId = ref<number | null>(null)
const presetId = ref<number | null>(null)
const startTs = ref<number | null>(null)
const endTs = ref<number | null>(null)
const note = ref('')
const repeat = ref(false)
const repeatWeeks = ref(3)
const forUser = ref<number | null>(null)
const users = ref<AdminUser[]>([])
const busy = ref(false)
const error = ref('')
const conflict = ref(false)
const autoBook = ref(true)

const editing = computed(() => props.editing ?? null)
const isActive = computed(() => editing.value?.phase === 'active')
const isAdmin = computed(() => !!session.user?.is_admin)

const ready = computed(() => props.models.filter((m) => m.status === 'ready' && m.file_exists))
const options = computed(() =>
  ready.value.map((m) => ({ label: `${m.name}  ·  ${formatBytes(m.size_bytes)}`, value: m.id })),
)
const selected = computed(() => ready.value.find((m) => m.id === modelId.value) ?? null)
const presetOptions = computed(() => [
  { label: t('schedule.presetDefault'), value: null as unknown as number },
  ...(selected.value?.presets ?? []).map((p) => ({ label: p.name, value: p.id })),
])
const userOptions = computed(() =>
  users.value.filter((u) => !u.disabled_at).map((u) => ({ label: u.username, value: u.id })),
)

watch(show, async (open) => {
  if (!open) return
  error.value = ''
  conflict.value = false
  repeat.value = false
  forUser.value = null
  const r = editing.value
  lane.value = r?.lane_id ?? props.laneId ?? lanes.value[0]?.id ?? null
  if (devenvEnabled.value) {
    templates.value = await api.devenv.templates().catch(() => templates.value)
  }
  if (r) {
    kind.value = r.devenv_template_id ? 'devenv' : r.exclusive ? 'exclusive' : 'model'
    templateId.value = r.devenv_template_id ?? templateId.value
    modelId.value = r.model_id ?? modelId.value
    presetId.value = r.preset_id
    startTs.value = new Date(r.start_at).getTime()
    endTs.value = new Date(r.end_at).getTime()
    note.value = r.note
  } else {
    kind.value = props.initialKind && (props.initialKind !== 'devenv' || devenvEnabled.value) ? props.initialKind : 'model'
    if (!templates.value.some((tpl) => tpl.id === templateId.value && tpl.enabled)) {
      templateId.value = templates.value.find((tpl) => tpl.enabled)?.id ?? null
    }
    note.value = ''
    presetId.value = null
    startTs.value = props.start?.getTime() ?? null
    endTs.value = props.end?.getTime() ?? null
    if (!ready.value.some((m) => m.id === modelId.value)) {
      modelId.value = (ready.value.find((m) => m.is_default) ?? ready.value[0])?.id ?? null
    }
  }
  if (isAdmin.value && !r && !users.value.length) users.value = await api.admin.users().catch(() => [])
})

watch(modelId, () => {
  if (!selected.value?.presets.some((p) => p.id === presetId.value)) presetId.value = null
})

const durationSeconds = computed(() =>
  startTs.value && endTs.value ? Math.max(0, (endTs.value - Math.max(startTs.value, Date.now())) / 1000) : 0,
)

const previous = computed(() => {
  if (!startTs.value) return null
  return (
    props.reservations
      .filter(
        (r) =>
          r.status === 'booked' &&
          r.id !== editing.value?.id &&
          Math.abs(new Date(r.end_at).getTime() - startTs.value!) < 60_000,
      )
      .sort((a, b) => b.end_at.localeCompare(a.end_at))[0] ?? null
  )
})

const needsApproval = computed(() => {
  const cfg = session.config
  if (!cfg || cfg.approval_mode === 'off' || isAdmin.value || isActive.value) return false
  if (cfg.approval_mode === 'all') return true
  return !!startTs.value && !!endTs.value && endTs.value - startTs.value > cfg.approval_hours * 3_600_000
})

const loadHint = computed(() => {
  if (kind.value === 'devenv') return { kind: 'devenv' as const }
  if (kind.value === 'exclusive') return { kind: 'exclusive' as const }
  if (!selected.value) return null
  if (previous.value && previous.value.model_id === selected.value.id) return { kind: 'same' as const }
  if (previous.value) return { kind: 'adjacent' as const }
  return { kind: 'preload' as const }
})

const slotMs = computed(() => (session.config?.slot_minutes ?? 30) * 60_000)
const maxWeeks = computed(() => Math.max(1, Math.floor((session.config?.max_advance_days ?? 30) / 7)))
const canSubmit = computed(
  () =>
    (kind.value === 'exclusive' || (kind.value === 'devenv' ? !!templateId.value : !!modelId.value)) &&
    !!startTs.value &&
    !!endTs.value &&
    endTs.value > startTs.value,
)

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

function payload(): BookingInput {
  const exclusive = kind.value !== 'model'
  return {
    lane_id: lane.value,
    model_id: exclusive ? null : modelId.value,
    preset_id: exclusive ? null : presetId.value,
    devenv_template_id: kind.value === 'devenv' ? templateId.value : null,
    start_at: new Date(startTs.value!).toISOString(),
    end_at: new Date(endTs.value!).toISOString(),
    note: note.value,
  }
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  conflict.value = false
  try {
    if (editing.value) {
      const body: Partial<BookingInput> = isActive.value
        ? { end_at: payload().end_at, note: note.value }
        : payload()
      await api.updateReservation(editing.value.id, body)
      message.success(t('schedule.saved'))
    } else {
      const created = await api.createReservation({
        ...payload(),
        repeat_weeks: repeat.value ? repeatWeeks.value : 0,
        user_id: forUser.value,
      })
      if (created[0]?.status === 'pending') message.info(t('schedule.submittedForApproval'))
      else if (created.length > 1) message.success(t('schedule.seriesCreated', { n: created.length }))
    }
    emit('done')
    show.value = false
  } catch (e) {
    error.value = describe(e)
    conflict.value =
      e instanceof ApiError &&
      e.code === 'reservation_conflict' &&
      !editing.value &&
      !repeat.value &&
      kind.value !== 'devenv'
  } finally {
    busy.value = false
  }
}

async function joinWaitlist() {
  busy.value = true
  try {
    await api.joinWaitlist({ ...payload(), auto_book: autoBook.value })
    message.success(t('schedule.waitlistJoined'))
    emit('done')
    show.value = false
  } catch (e) {
    error.value = describe(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    :title="editing ? t('schedule.editTitle') : t('schedule.bookingTitle')"
    :auto-focus="false"
    style="width: 540px; max-width: calc(100vw - 32px)"
    :bordered="false"
  >
    <NForm :show-require-mark="false" @submit.prevent="submit">
      <NFormItem v-if="multi" :label="t('schedule.lane')">
        <NSelect v-model:value="lane" :options="laneOptions" :disabled="isActive" />
      </NFormItem>
      <NFormItem :label="t('schedule.kind')">
        <NRadioGroup v-model:value="kind" :disabled="isActive" class="kind">
          <NRadioButton value="model">{{ t('schedule.kindModel') }}</NRadioButton>
          <NRadioButton value="exclusive">{{ t('schedule.kindExclusive') }}</NRadioButton>
          <NRadioButton v-if="devenvEnabled" value="devenv">{{ t('devenv.short') }}</NRadioButton>
        </NRadioGroup>
      </NFormItem>

      <template v-if="kind === 'devenv'">
        <NFormItem :label="t('devenv.template')">
          <NSelect v-model:value="templateId" :options="templateOptions" filterable :disabled="isActive" />
        </NFormItem>
        <p v-if="template?.description" class="faint small tpl-desc">
          {{ template.builtin_key ? t(`devenv.builtin.${template.builtin_key}`) : template.description }}
        </p>
      </template>

      <template v-if="kind === 'model'">
        <div v-if="!ready.length" class="muted empty-models">{{ t('schedule.noModels') }}</div>
        <div v-else class="two">
          <NFormItem :label="t('schedule.model')">
            <NSelect v-model:value="modelId" :options="options" filterable :disabled="isActive" />
          </NFormItem>
          <NFormItem :label="t('schedule.preset')">
            <NSelect
              v-model:value="presetId"
              :options="presetOptions"
              :placeholder="t('schedule.presetDefault')"
              :disabled="isActive || !selected?.presets.length"
            />
          </NFormItem>
        </div>
      </template>

      <div class="two">
        <NFormItem :label="t('schedule.start')">
          <NDatePicker
            v-model:value="startTs"
            type="datetime"
            format="yyyy-MM-dd HH:mm"
            :time-picker-props="{ format: 'HH:mm', minutes: session.config?.slot_minutes ?? 30 }"
            :is-date-disabled="disablePast"
            :disabled="isActive"
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
        <NButton v-if="!isActive" text type="primary" size="small" @click="startNow">{{ t('schedule.startNow') }}</NButton>
        <span class="faint small">
          {{ t('schedule.limits', { hours: session.config?.max_reservation_hours ?? 8, days: session.config?.max_advance_days ?? 30 }) }}
        </span>
      </div>

      <div v-if="!editing" class="extras">
        <label class="switch-row">
          <NSwitch v-model:value="repeat" size="small" />
          <span>{{ t('schedule.repeatWeekly') }}</span>
          <template v-if="repeat">
            <NInputNumber v-model:value="repeatWeeks" size="small" :min="1" :max="maxWeeks" class="weeks" />
            <span class="faint small">{{ t('schedule.moreWeeks') }}</span>
          </template>
        </label>
        <NFormItem v-if="isAdmin" :label="t('schedule.bookFor')" class="for-user">
          <NSelect v-model:value="forUser" :options="userOptions" :placeholder="t('schedule.bookForSelf')" clearable />
        </NFormItem>
      </div>

      <NFormItem :label="t('schedule.note')">
        <NInput v-model:value="note" :placeholder="t('schedule.notePlaceholder')" maxlength="500" />
      </NFormItem>

      <div v-if="kind === 'model' && selected" class="facts">
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
      <div v-if="kind === 'model' && selected?.exceeds_vram" class="hint adjacent">
        <TriangleAlert :size="15" />
        <span>{{ t('models.exceedsVramHint') }}</span>
      </div>
      <div v-if="needsApproval" class="hint adjacent">
        <Info :size="15" />
        <span>{{ t('schedule.needsApproval') }}</span>
      </div>
      <div v-if="loadHint" class="hint" :class="loadHint.kind">
        <TriangleAlert v-if="loadHint.kind === 'adjacent'" :size="15" />
        <Cpu v-else-if="loadHint.kind === 'exclusive'" :size="15" />
        <Terminal v-else-if="loadHint.kind === 'devenv'" :size="15" />
        <Info v-else :size="15" />
        <span v-if="loadHint.kind === 'adjacent'">{{ t('schedule.adjacentWarning', { eta: formatDuration(selected?.est_load_seconds) }) }}</span>
        <span v-else-if="loadHint.kind === 'same'">{{ t('schedule.sameModelNote') }}</span>
        <span v-else-if="loadHint.kind === 'exclusive'">{{ t('schedule.exclusiveInfo') }}</span>
        <span v-else-if="loadHint.kind === 'devenv'">{{ t('devenv.bookingInfo') }}</span>
        <span v-else>{{ t('schedule.preloadInfo') }}</span>
      </div>

      <NAlert v-if="error" type="error" :show-icon="false" class="err">
        {{ error }}
        <div v-if="conflict" class="waitlist">
          <span>{{ t('schedule.waitlistOffer') }}</span>
          <NCheckbox v-model:checked="autoBook" size="small">{{ t('schedule.autoBook') }}</NCheckbox>
          <NButton size="small" secondary type="primary" :loading="busy" @click="joinWaitlist">
            {{ t('schedule.joinWaitlist') }}
          </NButton>
        </div>
      </NAlert>
    </NForm>

    <template #footer>
      <div class="footer">
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!canSubmit" :loading="busy" @click="submit">
          {{ editing ? t('common.save') : t('schedule.newBooking') }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.tpl-desc {
  margin: -8px 0 14px;
}
.kind {
  width: 100%;
  display: flex;
}
.kind :deep(.n-radio-button) {
  flex: 1;
  text-align: center;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.empty-models {
  margin-bottom: 16px;
}
.quick {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: -12px 0 14px;
  gap: 12px;
}
.extras {
  display: grid;
  gap: 12px;
  margin-bottom: 4px;
}
.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-2);
  min-height: 28px;
  margin-bottom: 12px;
}
.weeks {
  width: 96px;
}
.for-user {
  margin-top: -4px;
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
  color: var(--warn);
}
.err {
  margin-top: 14px;
}
.waitlist {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed currentColor;
  color: var(--text-2);
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
