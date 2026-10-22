<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton } from 'naive-ui'
import { RefreshCw } from 'lucide-vue-next'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { relativeFromNow, formatDateTime } from '@/utils/format'
import { setupApi, type Health, type HealthCheck } from '../api'
import { byUrgency } from '../checks'
import CheckActions from './CheckActions.vue'
import DiagnosisCard from './DiagnosisCard.vue'

// System health (F08): every check as a traffic light row; problems first, with their evidence, buttons and the
// diagnosis card when the error pattern library recognised the cause.
const props = defineProps<{ health: Health | null }>()
const emit = defineEmits<{ updated: [check: HealthCheck]; reloaded: [health: Health] }>()
const { t, te } = useI18n()
const { showError, message } = useErrorMessage()

const showDismissed = ref(false)
const open = ref<Set<string>>(new Set())
const running = ref(false)

const all = computed(() => [...(props.health?.checks ?? [])].sort(byUrgency))
const dismissed = computed(() => all.value.filter((c) => c.dismissed))
const rows = computed(() => (showDismissed.value ? all.value : all.value.filter((c) => !c.dismissed)))
const problems = computed(() => all.value.filter((c) => !c.dismissed && (c.level === 'error' || c.level === 'warn')))

const group = (g: string) => (te(`adminSetup.health.groups.${g}`) ? t(`adminSetup.health.groups.${g}`) : g)
const expandable = (c: HealthCheck) => !!(c.evidence || c.diagnosis || c.dismissed)

function toggle(id: string) {
  const next = new Set(open.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  open.value = next
}

async function runAll() {
  running.value = true
  try {
    emit('reloaded', await setupApi.runAll())
    message.success(t('adminSetup.health.ran'))
  } catch (e) {
    showError(e)
  } finally {
    running.value = false
  }
}

async function retry(c: HealthCheck) {
  try {
    emit('updated', await setupApi.runCheck(c.id))
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <section class="card health" data-testid="health-table">
    <div class="card-header">
      <div>
        <h3 class="card-title">
          {{ t('adminSetup.health.title') }}
          <span v-if="problems.length" class="badge danger">{{ t('adminSetup.health.problems', { n: problems.length }) }}</span>
          <span v-else-if="health" class="badge ok">{{ t('adminSetup.health.levels.ok') }}</span>
        </h3>
        <div class="card-sub">{{ t('adminSetup.health.sub') }}</div>
      </div>
      <NButton size="small" secondary :loading="running" @click="runAll">
        <template #icon><RefreshCw :size="14" /></template>
        {{ t('adminSetup.health.runAll') }}
      </NButton>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th class="dot-col" />
            <th>{{ t('adminSetup.health.check') }}</th>
            <th class="hide-sm">{{ t('adminSetup.health.lastRun') }}</th>
            <th class="hide-sm" />
          </tr>
        </thead>
        <tbody>
          <template v-for="c in rows" :key="c.id">
            <tr :class="['check-row', c.level, { dim: c.dismissed, clickable: expandable(c) }]" :data-check="c.id" @click="expandable(c) && toggle(c.id)">
              <td class="dot-col">
                <span class="light" :class="c.dismissed ? 'dismissed' : c.level" :title="t(`adminSetup.health.levels.${c.level}`)" />
              </td>
              <td>
                <div class="title">{{ c.title }}</div>
                <div class="faint small meta">
                  <span>{{ group(c.group) }}</span>
                  <span>· {{ t(`adminSetup.health.levels.${c.level}`) }}</span>
                  <span v-if="c.since && c.level !== 'ok'">· {{ t('adminSetup.health.since', { time: formatDateTime(c.since) }) }}</span>
                  <span v-if="c.alerted">· {{ t('adminSetup.health.alerted') }}</span>
                </div>
                <div class="show-sm inline-actions" @click.stop>
                  <CheckActions :check="c" size="tiny" @updated="(x) => emit('updated', x)" />
                </div>
              </td>
              <td class="muted small nowrap hide-sm">{{ c.last_run ? relativeFromNow(c.last_run) : t('adminSetup.health.never') }}</td>
              <td class="right hide-sm" @click.stop>
                <CheckActions :check="c" size="tiny" @updated="(x) => emit('updated', x)" />
              </td>
            </tr>
            <tr v-if="open.has(c.id)" class="detail-row">
              <td />
              <td colspan="3" class="detail-cell">
                <p v-if="c.dismissed" class="faint small">{{ t('adminSetup.health.dismissedNoName', { reason: c.dismissed.reason }) }}</p>
                <DiagnosisCard v-if="c.diagnosis" :diagnosis="c.diagnosis" :title="c.title" retry @retry="retry(c)" />
                <template v-else-if="c.evidence">
                  <div class="faint small">{{ t('adminSetup.health.evidence') }}</div>
                  <pre class="evidence">{{ c.evidence }}</pre>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <div v-if="dismissed.length" class="foot">
      <NButton size="tiny" quaternary @click="showDismissed = !showDismissed">
        {{ t('adminSetup.health.showDismissed', { n: dismissed.length }) }}
      </NButton>
    </div>
  </section>
</template>

<style scoped>
.health {
  margin-top: 16px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dot-col {
  width: 28px;
  padding-right: 0 !important;
}
.light {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-3);
}
.light.ok {
  background: var(--ok);
}
.light.info {
  background: var(--brand);
}
.light.warn {
  background: var(--warn);
}
.light.error {
  background: var(--danger);
  box-shadow: 0 0 0 3px var(--danger-soft);
}
.light.dismissed {
  background: transparent;
  border: 2px solid var(--text-3);
}
.check-row.clickable {
  cursor: pointer;
}
.check-row.dim .title {
  color: var(--text-3);
}
.title {
  font-weight: 500;
  overflow-wrap: anywhere;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.small {
  font-size: 12px;
}
.nowrap {
  white-space: nowrap;
}
.right :deep(.check-actions) {
  justify-content: flex-end;
}
.detail-row td {
  background: var(--surface-muted);
}
.evidence {
  margin: 4px 0 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--code-bg);
  color: var(--code-text);
  font-family: var(--mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}
.foot {
  padding: 8px 16px;
  border-top: 1px solid var(--border);
}
.show-sm {
  display: none;
}
.inline-actions {
  margin-top: 6px;
}
@media (max-width: 760px) {
  .hide-sm {
    display: none;
  }
  .show-sm {
    display: block;
  }
}
</style>
