<script setup lang="ts">
// Choose a machine in a dialog, seeing what it offers (GPU or CPU only), its memory and current load;
// "Pick for me" asks the server for the best fit for the request, and the choice stays overridable.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { Sparkles } from 'lucide-vue-next'
import { machinesApi, type Candidate, type Machine, type Requirements } from '@/api/machines'
import { useMachines } from '@/composables/machines'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { STATUS_CLASS, cores, gb, maxGpuMemory } from '@/utils/machines'

const selected = defineModel<number | null>({ default: null })
const props = defineProps<{
  /** which numbers matter: GPU lanes and GPU memory, or free cores and memory */
  mode: 'gpu' | 'cpu'
  /** what "Pick for me" asks for */
  need: () => Requirements
  /** why a machine cannot be chosen here (null: it can) */
  disabledReason?: (m: Machine) => string | null
  disabled?: boolean
}>()
const emit = defineEmits<{ picked: [candidate: Candidate] }>()

const { t, te } = useI18n()
const { showError, message } = useErrorMessage()
const { machines } = useMachines()
const busy = ref(false)
const verdicts = ref<Map<number, Candidate> | null>(null)

const reasonText = (code: string) => (te(`machines.reasons.${code}`) ? t(`machines.reasons.${code}`) : code)

function blocked(m: Machine): string | null {
  return props.disabledReason?.(m) ?? null
}

function choose(m: Machine) {
  if (props.disabled || blocked(m)) return
  selected.value = m.id
}

async function pickForMe() {
  busy.value = true
  try {
    const r = await machinesApi.recommend(props.need())
    verdicts.value = new Map(r.machines.map((c) => [c.node_id, c]))
    if (r.best) {
      selected.value = r.best.node_id
      emit('picked', r.best)
      message.success(t('machines.picked', { name: r.best.lane_name ? `${r.best.node_name} · ${r.best.lane_name}` : r.best.node_name }))
    } else {
      message.warning(t('machines.nothingFits'))
    }
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

const rows = computed(() =>
  machines.value.map((m) => {
    const verdict = verdicts.value?.get(m.id) ?? null
    return { m, blocked: blocked(m), verdict }
  }),
)
</script>

<template>
  <div class="picker" :class="{ disabled }">
    <div class="picker-head">
      <span class="faint small">{{ mode === 'gpu' ? t('machines.pickHintGpu') : t('machines.pickHintCpu') }}</span>
      <span class="spacer" />
      <NButton size="tiny" secondary type="primary" :loading="busy" :disabled="disabled" data-action="pick-for-me" @click="pickForMe">
        <template #icon><Sparkles :size="13" /></template>
        {{ t('machines.pickForMe') }}
      </NButton>
    </div>
    <div class="options" role="radiogroup">
      <button
        v-for="r in rows"
        :key="r.m.id"
        type="button"
        role="radio"
        class="option"
        :class="{ on: selected === r.m.id, off: !!r.blocked || !r.m.online }"
        :aria-checked="selected === r.m.id"
        :disabled="disabled || !!r.blocked"
        :data-machine="r.m.name"
        @click="choose(r.m)"
      >
        <span class="top">
          <span class="dot" :class="STATUS_CLASS[r.m.status]" />
          <b class="name">{{ r.m.name }}</b>
          <span class="badge" :class="r.m.kind === 'gpu' ? 'brand' : ''">
            {{ r.m.kind === 'gpu' ? t('machines.gpuKind', { n: r.m.gpus.length }) : t('machines.cpuOnly') }}
          </span>
          <span v-if="r.verdict?.fits" class="score num" :title="t('machines.score')">{{ r.verdict.score.toFixed(0) }}</span>
        </span>
        <span class="nums num">
          <template v-if="mode === 'gpu'">
            <span>{{ t('machines.lanesFree', { n: r.m.capacity.lanes_free, total: r.m.capacity.lanes }) }}</span>
            <span v-if="r.m.gpus.length">{{ t('machines.gpuMem', { n: gb(maxGpuMemory(r.m)) }) }}</span>
          </template>
          <span>{{ t('machines.coresFreeShort', { n: cores(r.m.capacity.cpu_cores_free) }) }}</span>
          <span>{{ t('machines.memFreeShort', { n: gb(r.m.capacity.memory_free_mb) }) }}</span>
          <span>{{ t('machines.cpuPct', { n: r.m.cpu_percent == null ? '—' : r.m.cpu_percent.toFixed(0) }) }}</span>
        </span>
        <span v-if="r.blocked" class="why faint">{{ r.blocked }}</span>
        <span v-else-if="!r.m.online" class="why faint">{{ t(`cluster.statuses.${r.m.status}`) }}</span>
        <span v-else-if="r.verdict && r.verdict.reasons.length" class="why" :class="r.verdict.fits ? 'faint' : 'warn-text'">
          {{ r.verdict.reasons.map(reasonText).join(' · ') }}
        </span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.picker {
  display: grid;
  gap: 8px;
  width: 100%;
}
.picker-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.small {
  font-size: 12px;
}
.options {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 8px;
}
.option {
  display: grid;
  gap: 3px;
  text-align: left;
  font: inherit;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  cursor: pointer;
  min-width: 0;
  transition: border-color 0.12s, background 0.12s;
}
.option:hover:not(:disabled) {
  border-color: var(--border-strong);
}
.option.on {
  border-color: var(--brand);
  background: var(--brand-softer);
  box-shadow: 0 0 0 1px var(--brand) inset;
}
.option.off {
  opacity: 0.6;
}
.option:disabled {
  cursor: not-allowed;
}
.top {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.top .badge {
  height: 18px;
  font-size: 10.5px;
}
.score {
  margin-left: auto;
  font-size: 11px;
  font-weight: 650;
  color: var(--brand);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-3);
  flex: none;
}
.dot.ok {
  background: var(--ok);
}
.dot.danger {
  background: var(--danger);
}
.dot.warn {
  background: var(--warn);
}
.nums {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  font-size: 11.5px;
  color: var(--text-2);
}
.why {
  font-size: 11.5px;
}
.warn-text {
  color: var(--warn-text);
}
</style>
