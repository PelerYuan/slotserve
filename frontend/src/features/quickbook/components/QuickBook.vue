<script setup lang="ts">
// QuickBook (F02): what -> when -> where. "Where" is automatic (placement.fit on the server decides; nothing is
// judged here), "Change" lists every place with its reasons, and "More options" opens the full booking dialog
// with what was chosen so far. One component for every entry point: Overview, Schedule "New booking", Dev
// environments "Book an environment".
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NDatePicker, NModal, NRadioButton, NRadioGroup, NSelect } from 'naive-ui'
import { Info, TriangleAlert } from 'lucide-vue-next'
import BookingModal from '@/components/BookingModal.vue'
import { api } from '@/api/client'
import type { BookingInput, DevProject, DevTemplate, Model, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useLanes } from '@/composables/lanes'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatRange, formatTime } from '@/utils/format'
import { laneLabel } from '@/utils/naming'
import { placementApi, type BookingKind, type Candidate, type FitRequest, type FitResult } from '../api'
import { useReasonText } from '../reasons'
import WherePicker from './WherePicker.vue'

const show = defineModel<boolean>('show', { default: false })
const props = defineProps<{
  /** what to preselect: a dev environment (Dev environments page), a model (a model card) or the whole GPU */
  intent?: BookingKind | null
  /** with intent "model": this model */
  modelId?: number | null
  /** with intent "devenv": start from this template, or continue this kept environment */
  templateId?: number | null
  projectId?: number | null
  /** the models to offer (loaded when not given) */
  models?: Model[] | null
}>()
const emit = defineEmits<{ done: [created: Reservation[]] }>()

const { t } = useI18n()
const { describe, message } = useErrorMessage()
const reasonText = useReasonText()
const { lanes, multi } = useLanes()

const HOUR = 3_600_000
const STALE_MS = 60_000
const UNTIL_HOUR = 18

// ---- what ----------------------------------------------------------------------------------------------------
const kind = ref<BookingKind>('devenv')
const devenvEnabled = computed(() => !!session.config?.devenv_enabled)
const loadedModels = ref<Model[]>([])
const allModels = computed(() => props.models ?? loadedModels.value)
const ready = computed(() => allModels.value.filter((m) => m.status === 'ready' && m.file_exists))
const modelId = ref<number | null>(null)
const modelOptions = computed(() => ready.value.map((m) => ({ label: m.name, value: m.id })))
const templates = ref<DevTemplate[]>([])
const projects = ref<DevProject[]>([])
/** "t:<template id>" (new from a template) or "p:<project id>" (continue a kept environment) */
const env = ref<string | null>(null)
const envOptions = computed(() => [
  ...projects.value.map((p) => ({ label: t('quickbook.continueKept', { name: p.name }), value: `p:${p.id}` })),
  ...templates.value
    .filter((tpl) => tpl.enabled)
    .map((tpl) => ({ label: t('quickbook.newFrom', { name: tpl.name }), value: `t:${tpl.id}` })),
])
const envTemplateId = computed(() => (env.value?.startsWith('t:') ? Number(env.value.slice(2)) : null))
const envProjectId = computed(() => (env.value?.startsWith('p:') ? Number(env.value.slice(2)) : null))
const whatReady = computed(() =>
  kind.value === 'exclusive' ? true : kind.value === 'model' ? !!modelId.value : !!env.value,
)

// ---- when ----------------------------------------------------------------------------------------------------
type Chip = '1h' | '2h' | 'until' | 'custom'
const chip = ref<Chip>('2h')
const customStart = ref<number | null>(null)
const customEnd = ref<number | null>(null)
const openedAt = ref(Date.now())
const maxHours = computed(() => session.config?.max_reservation_hours ?? 8)
/** "now until 18:00" while that is more than an hour away and not longer than a booking may be */
const untilEnd = computed(() => {
  const end = new Date(openedAt.value)
  end.setHours(UNTIL_HOUR, 0, 0, 0)
  const span = end.getTime() - openedAt.value
  return span > HOUR && span <= maxHours.value * HOUR ? end : null
})

function windowNow(): [Date, Date] | null {
  const now = Date.now()
  if (chip.value === '1h') return [new Date(now), new Date(now + HOUR)]
  if (chip.value === '2h') return [new Date(now), new Date(now + 2 * HOUR)]
  if (chip.value === 'until') return untilEnd.value ? [new Date(now), untilEnd.value] : null
  if (customStart.value && customEnd.value && customEnd.value > customStart.value)
    return [new Date(customStart.value), new Date(customEnd.value)]
  return null
}

function disablePast(ts: number) {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return ts < d.getTime()
}

// ---- where: placement.fit ------------------------------------------------------------------------------------
const fit = ref<FitResult | null>(null)
const fitAt = ref(0)
const fitting = ref(false)
const fitError = ref('')
/** the lane picked under "Change"; null = automatic */
const laneChoice = ref<number | null>(null)
const showWhere = ref(false)
let seq = 0
let timer: number | undefined

function fitRequest(win: [Date, Date]): FitRequest {
  return {
    kind: kind.value,
    model_id: kind.value === 'model' ? modelId.value : null,
    devenv_template_id: kind.value === 'devenv' ? envTemplateId.value : null,
    devenv_project_id: kind.value === 'devenv' ? envProjectId.value : null,
    start_at: win[0].toISOString(),
    end_at: win[1].toISOString(),
  }
}

async function runFit() {
  const win = windowNow()
  const mine = ++seq
  if (!win || !whatReady.value) {
    fit.value = null
    return
  }
  fitting.value = true
  fitError.value = ''
  try {
    const result = await placementApi.fit(fitRequest(win))
    if (mine !== seq) return
    fit.value = result
    fitAt.value = Date.now()
    if (laneChoice.value !== null && !result.candidates.some((c) => c.lane_id === laneChoice.value && c.fits))
      laneChoice.value = null
  } catch (e) {
    if (mine === seq) {
      fit.value = null
      fitError.value = describe(e)
    }
  } finally {
    if (mine === seq) fitting.value = false
  }
}

function refit() {
  window.clearTimeout(timer)
  timer = window.setTimeout(runFit, 200)
}
onBeforeUnmount(() => window.clearTimeout(timer))

const chosen = computed<Candidate | null>(() => {
  const f = fit.value
  if (!f) return null
  if (laneChoice.value !== null) return f.candidates.find((c) => c.lane_id === laneChoice.value && c.fits) ?? f.best
  return f.best
})
const candidateName = (c: Candidate) => laneLabel({ name: c.lane_name ?? '', gpus: c.gpus })
const whereLine = computed(() => {
  const c = chosen.value
  if (!c) return ''
  const free = c.reasons_ok.find((r) => r.code === 'free_until' || r.code === 'free_ahead')
  const name = laneChoice.value === null ? t('quickbook.autoPick', { name: candidateName(c) }) : candidateName(c)
  return free ? `${name} · ${reasonText(free)}` : name
})
/** why it cannot be booked: the request-wide reasons, else the reason of each lane */
const refusals = computed(() => {
  const f = fit.value
  if (!f || chosen.value) return []
  const list = f.reasons_no.length ? f.reasons_no : f.candidates.flatMap((c) => c.reasons_no.slice(0, 1))
  return [...new Set(list.map(reasonText))]
})
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const hints = computed(() => (fit.value?.reasons_ok ?? []).map((r) => capital(reasonText(r))))
const conflictOnly = computed(
  () =>
    !!fit.value &&
    !chosen.value &&
    !fit.value.reasons_no.length &&
    fit.value.candidates.some((c) => c.reasons_no.some((r) => r.code === 'reservation_conflict')),
)
const startsNow = computed(() => !!fit.value && new Date(fit.value.start_at).getTime() <= Date.now() + 60_000)
const canSubmit = computed(() => !!chosen.value && !fitting.value && !busy.value)

watch([kind, modelId, env, chip, customStart, customEnd], () => {
  if (show.value) refit()
})

// ---- open ----------------------------------------------------------------------------------------------------
async function prepare() {
  openedAt.value = Date.now()
  error.value = ''
  fit.value = null
  laneChoice.value = null
  showWhere.value = false
  chip.value = '2h'
  customStart.value = null
  customEnd.value = null
  const wanted = props.intent ?? (devenvEnabled.value ? 'devenv' : 'model')
  kind.value = wanted === 'devenv' && !devenvEnabled.value ? 'model' : wanted
  const loads: Promise<unknown>[] = []
  if (!props.models) loads.push(api.models().then((list) => (loadedModels.value = list)).catch(() => undefined))
  if (devenvEnabled.value) {
    loads.push(api.devenv.templates().then((list) => (templates.value = list)).catch(() => undefined))
    loads.push(
      api.devenv
        .projects()
        .then((list) => (projects.value = list.filter((p) => p.mine !== false)))
        .catch(() => undefined),
    )
  }
  await Promise.all(loads)
  if (props.modelId && ready.value.some((m) => m.id === props.modelId)) modelId.value = props.modelId
  else if (!ready.value.some((m) => m.id === modelId.value))
    modelId.value = (ready.value.find((m) => m.is_default) ?? ready.value[0])?.id ?? null
  const values = envOptions.value.map((o) => o.value)
  if (props.projectId && values.includes(`p:${props.projectId}`)) env.value = `p:${props.projectId}`
  else if (props.templateId && values.includes(`t:${props.templateId}`)) env.value = `t:${props.templateId}`
  else if (!env.value || !values.includes(env.value)) env.value = values[0] ?? null
  await runFit()
}

watch(show, (open) => {
  if (open) void prepare()
})

// ---- book ----------------------------------------------------------------------------------------------------
const busy = ref(false)
const error = ref('')

function bookingBody(c: Candidate | null, f: FitResult): BookingInput {
  return {
    lane_id: c?.lane_id ?? null,
    model_id: kind.value === 'model' ? modelId.value : null,
    devenv_template_id: kind.value === 'devenv' ? envTemplateId.value : null,
    devenv_project_id: kind.value === 'devenv' ? envProjectId.value : null,
    start_at: f.start_at,
    end_at: f.end_at,
    note: '',
  }
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    // a "now" window from a minute ago would start in the past: ask again (same rules, fresh times)
    if (Date.now() - fitAt.value > STALE_MS) await runFit()
    const f = fit.value
    const c = chosen.value
    if (!f || !c) return
    const created = await api.createReservation(bookingBody(c, f))
    if (created[0]?.status === 'pending') message.info(t('schedule.submittedForApproval'))
    else message.success(kind.value === 'devenv' && startsNow.value ? t('quickbook.started') : t('quickbook.booked'))
    emit('done', created)
    show.value = false
  } catch (e) {
    error.value = describe(e)
    void runFit()
  } finally {
    busy.value = false
  }
}

function useNextFree() {
  const next = fit.value?.next_free
  if (!next) return
  customStart.value = new Date(next.start_at).getTime()
  customEnd.value = new Date(next.end_at).getTime()
  chip.value = 'custom'
}

async function joinWaitlist() {
  const f = fit.value
  if (!f) return
  busy.value = true
  try {
    const lane = laneChoice.value ?? f.candidates[0]?.lane_id ?? null
    const body = bookingBody(null, f)
    await api.joinWaitlist({ ...body, lane_id: lane, auto_book: true })
    message.success(t('schedule.waitlistJoined'))
    emit('done', [])
    show.value = false
  } catch (e) {
    error.value = describe(e)
  } finally {
    busy.value = false
  }
}

// ---- more options: the full dialog ----------------------------------------------------------------------------
const full = ref(false)
const draftStart = ref<Date | null>(null)
const draftEnd = ref<Date | null>(null)
const draft = ref<Partial<BookingInput> | null>(null)

function moreOptions() {
  const win = windowNow()
  draftStart.value = fit.value ? new Date(fit.value.start_at) : (win?.[0] ?? null)
  draftEnd.value = fit.value ? new Date(fit.value.end_at) : (win?.[1] ?? null)
  draft.value = {
    lane_id: chosen.value?.lane_id ?? null,
    model_id: kind.value === 'model' ? modelId.value : null,
    devenv_template_id: kind.value === 'devenv' ? envTemplateId.value : null,
    devenv_project_id: kind.value === 'devenv' ? envProjectId.value : null,
  }
  show.value = false
  full.value = true
}
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    class="quickbook"
    :title="t(`schedule.bookingTitle.${kind}`)"
    :auto-focus="false"
    style="width: 560px; max-width: calc(100vw - 32px)"
    :bordered="false"
  >
    <p class="model-only faint">
      <Info :size="13" />
      <span>{{ t('quickbook.modelOnly') }}</span>
      <RouterLink :to="{ name: 'keys' }" @click="show = false">{{ t('quickbook.modelOnlyLink') }} →</RouterLink>
    </p>

    <div class="rows">
      <div class="row">
        <div class="label">{{ t('quickbook.what') }}</div>
        <div class="field">
          <NRadioGroup v-model:value="kind" class="kind" size="small">
            <NRadioButton v-if="devenvEnabled" value="devenv">{{ t('devenv.short') }}</NRadioButton>
            <NRadioButton value="model">{{ t('schedule.kindModel') }}</NRadioButton>
            <NRadioButton value="exclusive">{{ t('schedule.kindExclusive') }}</NRadioButton>
          </NRadioGroup>
          <NSelect
            v-if="kind === 'devenv'"
            v-model:value="env"
            :options="envOptions"
            :placeholder="envOptions.length ? t('ph.template') : t('quickbook.noEnvironments')"
            :disabled="!envOptions.length"
            filterable
            size="small"
            data-field="environment"
          />
          <template v-else-if="kind === 'model'">
            <NSelect
              v-if="ready.length"
              v-model:value="modelId"
              :options="modelOptions"
              :placeholder="t('ph.model')"
              filterable
              size="small"
              data-field="model"
            />
            <span v-else class="faint small">{{ t('schedule.noModels') }}</span>
          </template>
        </div>
      </div>

      <div class="row">
        <div class="label">{{ t('quickbook.when') }}</div>
        <div class="field">
          <div class="chips">
            <NButton size="small" :type="chip === '1h' ? 'primary' : 'default'" secondary data-chip="1h" @click="chip = '1h'">
              {{ t('quickbook.nowFor', { n: 1 }) }}
            </NButton>
            <NButton size="small" :type="chip === '2h' ? 'primary' : 'default'" secondary data-chip="2h" @click="chip = '2h'">
              {{ t('quickbook.nowFor', { n: 2 }) }}
            </NButton>
            <NButton
              v-if="untilEnd"
              size="small"
              :type="chip === 'until' ? 'primary' : 'default'"
              secondary
              data-chip="until"
              @click="chip = 'until'"
            >
              {{ t('quickbook.nowUntil', { time: formatTime(untilEnd) }) }}
            </NButton>
            <NButton
              size="small"
              :type="chip === 'custom' ? 'primary' : 'default'"
              secondary
              data-chip="custom"
              @click="chip = 'custom'"
            >
              {{ t('quickbook.pickTime') }}
            </NButton>
          </div>
          <div v-if="chip === 'custom'" class="two">
            <NDatePicker
              v-model:value="customStart"
              type="datetime"
              size="small"
              format="yyyy-MM-dd HH:mm"
              :placeholder="t('ph.dateTime')"
              :time-picker-props="{ format: 'HH:mm', minutes: session.config?.slot_minutes ?? 30 }"
              :is-date-disabled="disablePast"
            />
            <NDatePicker
              v-model:value="customEnd"
              type="datetime"
              size="small"
              format="yyyy-MM-dd HH:mm"
              :placeholder="t('ph.dateTime')"
              :time-picker-props="{ format: 'HH:mm', minutes: session.config?.slot_minutes ?? 30 }"
              :is-date-disabled="disablePast"
            />
          </div>
        </div>
      </div>

      <div v-if="multi && (chosen || showWhere)" class="row">
        <div class="label">{{ t('quickbook.where') }}</div>
        <div class="field">
          <div class="where-line">
            <span data-field="where">{{ whereLine }}</span>
            <NButton text type="primary" size="small" @click="showWhere = !showWhere">{{ t('quickbook.change') }}</NButton>
          </div>
          <WherePicker v-if="showWhere && fit" v-model="laneChoice" :candidates="fit.candidates" />
        </div>
      </div>
    </div>

    <slot name="summary" :kind="kind" :template-id="envTemplateId" :project-id="envProjectId" :lane-id="chosen?.lane_id ?? null" />

    <div v-if="fitting && !fit" class="faint small status">{{ t('quickbook.checking') }}</div>
    <div v-for="h in hints" :key="h" class="hint">
      <Info :size="14" />
      <span>{{ h }}</span>
    </div>

    <NAlert v-if="refusals.length" type="warning" :show-icon="false" class="alert" data-field="refusal">
      <div>{{ t('quickbook.nothingFits') }}</div>
      <ul class="reasons">
        <li v-for="r in refusals" :key="r">{{ r }}</li>
      </ul>
      <div class="alternatives">
        <NButton v-if="fit?.next_free" size="small" secondary type="primary" @click="useNextFree">
          {{ t('quickbook.nextFree', { range: formatRange(fit.next_free.start_at, fit.next_free.end_at) }) }} ·
          {{ t('quickbook.useThisTime') }}
        </NButton>
        <NButton v-if="conflictOnly && kind !== 'devenv'" size="small" secondary :loading="busy" @click="joinWaitlist">
          {{ t('schedule.joinWaitlist') }}
        </NButton>
      </div>
    </NAlert>
    <NAlert v-if="fitError || error" type="error" :show-icon="false" class="alert">
      <TriangleAlert :size="14" /> {{ error || fitError }}
    </NAlert>

    <template #footer>
      <div class="footer">
        <NButton text type="primary" size="small" class="more" data-action="more-options" @click="moreOptions">
          {{ t('quickbook.moreOptions') }} →
        </NButton>
        <span class="spacer" />
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!canSubmit" :loading="busy" data-action="quickbook-submit" @click="submit">
          {{ startsNow ? t('quickbook.start') : t('quickbook.book') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <BookingModal
    v-model:show="full"
    :start="draftStart"
    :end="draftEnd"
    :models="allModels"
    :reservations="[]"
    :lane-id="draft?.lane_id ?? lanes[0]?.id ?? null"
    :initial-kind="kind"
    :draft="draft"
    @done="emit('done', [])"
  />
</template>

<style scoped>
.model-only {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: -4px 0 14px;
  font-size: 12.5px;
}
.rows {
  display: grid;
  gap: 14px;
}
.row {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 12px;
  align-items: start;
}
.label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  padding-top: 4px;
}
.field {
  display: grid;
  gap: 8px;
  min-width: 0;
}
.kind {
  display: flex;
  flex-wrap: wrap;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.where-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 13.5px;
  padding-top: 3px;
}
.small {
  font-size: 12.5px;
}
.status {
  margin-top: 12px;
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
.alert {
  margin-top: 14px;
}
.reasons {
  margin: 6px 0 0;
  padding-left: 18px;
}
.alternatives {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.alternatives:empty {
  display: none;
}
.footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.spacer {
  flex: 1;
}
@media (max-width: 520px) {
  .row {
    grid-template-columns: 1fr;
    gap: 6px;
  }
  .two {
    grid-template-columns: 1fr;
  }
}
</style>
