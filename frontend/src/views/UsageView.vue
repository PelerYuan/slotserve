<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NRadioButton, NRadioGroup } from 'naive-ui'
import { FileDown } from 'lucide-vue-next'
import UsageChart from '@/components/UsageChart.vue'
import LineChart from '@/components/LineChart.vue'
import { api } from '@/api/client'
import type { Usage, UsageExport, UsageKind } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { isDark } from '@/composables/theme'
import { formatCompact, formatNumber } from '@/utils/format'

// Categorical slots in fixed order (validated reference palette, stepped per theme); color follows the entity.
const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6']
const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9']
const SERIES_C = computed(() => (isDark.value ? SERIES_DARK : SERIES_LIGHT))
const OTHER_C = computed(() => (isDark.value ? '#6b6b7b' : '#a3a6b8'))
const FREE_C = computed(() => (isDark.value ? '#34343e' : '#e4e5ec'))

// Fixed slot per kind; kinds of other resources are appended so existing colors never shift.
const KINDS: UsageKind[] = ['model', 'gpu', 'devenv', 'idle_model', 'idle_devenv', 'cpu_devenv', 'free']
const kindColor = (k: string) => {
  const i = KINDS.indexOf(k as UsageKind)
  return k === 'free' ? FREE_C.value : i >= 0 ? SERIES_C.value[i] : OTHER_C.value
}

const { t } = useI18n()
const { showError } = useErrorMessage()
const days = ref(30)
const scope = ref<'all' | 'mine'>('all')
const groupBy = ref<'person' | 'kind'>('person')
const data = ref<Usage | null>(null)

function exportCsv(kind: UsageExport) {
  const a = document.createElement('a')
  a.href = api.usageExportUrl(days.value, kind, scope.value)
  a.download = ''
  a.click()
}

async function load() {
  try {
    data.value = await api.usage(days.value, scope.value)
  } catch (e) {
    showError(e)
  }
}
onMounted(load)
watch([days, scope], load)

// ---- formatting --------------------------------------------------------------

function hours(v: number) {
  if (v === 0) return '0'
  return v < 1 ? v.toFixed(2) : v < 100 ? v.toFixed(1) : formatNumber(Math.round(v))
}
const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)}%`)
function energy(wh: number) {
  return wh >= 1000 ? `${(wh / 1000).toFixed(wh >= 100_000 ? 0 : 1)} kWh` : `${Math.round(wh)} Wh`
}
const memory = (mb: number | null | undefined) => (mb == null ? '—' : `${(mb / 1024).toFixed(1)} GB`)
const kindName = (k: string) => t(`usage.kinds.${k}`)

// ---- headline ----------------------------------------------------------------

const totals = computed(() => data.value?.totals)
const usedShare = computed(() => {
  const tt = totals.value
  return tt && tt.sampled_gpu_hours > 0 ? Math.round((tt.used_gpu_hours / tt.sampled_gpu_hours) * 100) : null
})

// ---- charts ------------------------------------------------------------------

const personColor = computed(() => {
  const names = [...new Set((data.value?.hours_by_person ?? []).map((s) => s.name))].sort()
  const map = new Map<string, string>()
  names.forEach((n, i) => map.set(n, i < SERIES_C.value.length ? SERIES_C.value[i] : OTHER_C.value))
  return map
})

const hourSeries = computed(() => {
  const d = data.value
  if (!d) return []
  if (groupBy.value === 'kind') {
    return d.hours_by_kind
      .filter((s) => s.values.some((v) => (v ?? 0) > 0))
      .map((s) => ({ name: kindName(s.key), color: kindColor(s.key), values: s.values.map((v) => v ?? 0) }))
  }
  // People beyond the palette fold into "Other" rather than getting generated colors.
  const out = new Map<string, { name: string; color: string; values: number[] }>()
  for (const s of d.hours_by_person) {
    const other = personColor.value.get(s.name) === OTHER_C.value
    const name = other ? t('usage.other') : s.name
    const entry = out.get(name) ?? { name, color: other ? OTHER_C.value : personColor.value.get(s.name)!, values: Array(d.buckets.length).fill(0) }
    s.values.forEach((v, i) => (entry.values[i] += v ?? 0))
    out.set(name, entry)
  }
  return [...out.values()]
})
const hasHours = computed(() => hourSeries.value.some((s) => s.values.some((v) => v > 0)))

const utilSeries = computed(() =>
  (data.value?.utilization_by_lane ?? []).map((s, i) => ({
    name: s.name,
    color: SERIES_C.value[i % SERIES_C.value.length],
    values: s.values,
  })),
)

// ---- tables ------------------------------------------------------------------

const people = computed(() => data.value?.users ?? [])
const occupancyOf = (occ: Partial<Record<UsageKind, number>>) => {
  const total = KINDS.reduce((s, k) => s + (occ[k] ?? 0), 0)
  return total > 0
    ? KINDS.filter((k) => (occ[k] ?? 0) > 0).map((k) => ({ kind: k, share: (occ[k]! / total) * 100, hours: occ[k]! }))
    : []
}
const multiLane = computed(() => (data.value?.lanes.length ?? 0) > 1)
// Several machines (nodes): GPU time and occupancy per machine.
const multiNode = computed(() => (data.value?.nodes?.length ?? 0) > 1)
const kindsPresent = computed(() =>
  KINDS.filter((k) => (data.value?.lanes ?? []).some((l) => (l.occupancy[k] ?? 0) > 0)),
)
// CPU-only environments are shown next to GPU time once any are recorded.
const showCpu = computed(() => (data.value?.totals.cpu_core_hours ?? 0) > 0)
const meteredGpus = computed(() => (data.value?.gpus ?? []).filter((g) => g.gpu_index >= 0))
const hasData = computed(() => {
  const d = data.value
  return !!d && (d.users.length > 0 || d.totals.sampled_gpu_hours > 0)
})
</script>

<template>
  <div class="page usage">
    <div class="page-header">
      <div>
        <h1>{{ t('usage.title') }}</h1>
        <p>{{ t('usage.subtitle') }}</p>
      </div>
      <div class="head-actions">
        <NRadioGroup v-model:value="scope" size="small" class="scope">
          <NRadioButton value="all">{{ t('usage.everyone') }}</NRadioButton>
          <NRadioButton value="mine">{{ t('usage.mine') }}</NRadioButton>
        </NRadioGroup>
        <NRadioGroup v-model:value="days" size="small">
          <NRadioButton v-for="n in [7, 30, 90]" :key="n" :value="n">{{ t('usage.lastDays', { n }) }}</NRadioButton>
        </NRadioGroup>
        <NDropdown
          trigger="click"
          :options="[
            { key: 'people', label: t('usage.exportPeople') },
            { key: 'compute', label: t('usage.exportCompute') },
            ...(multiNode ? [{ key: 'nodes', label: t('usage.exportNodes') }] : []),
            { key: 'summary', label: t('usage.exportSummary') },
            { key: 'requests', label: t('usage.exportRequests') },
          ]"
          @select="(k: UsageExport) => exportCsv(k)"
        >
          <NButton size="small" secondary>
            <template #icon><FileDown :size="14" /></template>
            {{ t('usage.export') }}
          </NButton>
        </NDropdown>
      </div>
    </div>

    <div v-if="totals" class="tiles">
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.bookedGpuHours') }}</div>
        <div class="tile-value">{{ hours(totals.booked_gpu_hours) }}</div>
        <div class="faint small">
          {{ showCpu ? t('usage.cpuCoreHoursTotal', { n: hours(totals.cpu_core_hours) }) : t('usage.bookedHint') }}
        </div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.usedGpuHours') }}</div>
        <div class="tile-value">{{ data!.metered ? hours(totals.used_gpu_hours) : '—' }}</div>
        <div class="faint small">
          {{ usedShare != null ? t('usage.usedHint', { pct: usedShare, busy: data!.busy_percent }) : t('usage.noMetricsShort') }}
        </div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.avgUtilization') }}</div>
        <div class="tile-value">{{ pct(totals.avg_utilization) }}</div>
        <div class="faint small">{{ t(scope === 'mine' ? 'usage.utilHintMine' : 'usage.utilHint') }}</div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.energy') }}</div>
        <div class="tile-value">{{ data!.metered ? energy(totals.energy_wh) : '—' }}</div>
        <div class="faint small">{{ t('usage.peakMemory') }} {{ memory(totals.peak_memory_mb) }}</div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.totalTokens') }}</div>
        <div class="tile-value">{{ formatCompact(totals.tokens) }}</div>
        <div class="faint small">{{ t('usage.requestsCount', { n: formatNumber(totals.requests) }) }}</div>
      </div>
    </div>

    <p v-if="data && !data.metered && totals && totals.sampled_gpu_hours > 0" class="note muted">
      {{ t('usage.noMetrics') }}
    </p>

    <section class="card">
      <div class="card-header wrap">
        <h3 class="card-title">{{ t('usage.gpuHoursOverTime') }}</h3>
        <NRadioGroup v-model:value="groupBy" size="small" class="group-by">
          <NRadioButton value="person">{{ t('usage.byPersonBooked') }}</NRadioButton>
          <NRadioButton value="kind">{{ t('usage.byKindSampled') }}</NRadioButton>
        </NRadioGroup>
      </div>
      <div class="card-pad">
        <div v-if="hasHours && hourSeries.length > 1" class="legend">
          <span v-for="s in hourSeries" :key="s.name" class="lg"><span class="sw" :style="{ background: s.color }" />{{ s.name }}</span>
        </div>
        <UsageChart
          v-if="data && hasHours"
          :labels="data.buckets"
          :series="hourSeries"
          :format="(v: number) => `${hours(v)} h`"
          :axis-format="hours"
          :label="t('usage.gpuHoursOverTime')"
        />
        <div v-else class="empty">{{ t('usage.empty') }}</div>
      </div>
    </section>

    <section v-if="utilSeries.length" class="card">
      <div class="card-header wrap">
        <h3 class="card-title">{{ t('usage.utilOverTime') }}</h3>
        <div v-if="utilSeries.length > 1" class="legend">
          <span v-for="s in utilSeries" :key="s.name" class="lg"><span class="sw" :style="{ background: s.color }" />{{ s.name }}</span>
        </div>
      </div>
      <div class="card-pad">
        <LineChart :labels="data!.buckets" :series="utilSeries" :label="t('usage.utilOverTime')" />
      </div>
    </section>

    <section v-if="hasData" class="card people">
      <div class="card-header"><h3 class="card-title">{{ t('usage.byUser') }}</h3></div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>{{ t('usage.user') }}</th>
              <th class="right">{{ t('usage.bookedGpuHoursShort') }}</th>
              <th class="right">{{ t('usage.usedGpuHoursShort') }}</th>
              <th>{{ t('usage.utilDuringBookings') }}</th>
              <th class="right">{{ t('usage.peakMemory') }}</th>
              <th class="right">{{ t('usage.energy') }}</th>
              <th v-if="showCpu" class="right">{{ t('usage.cpuCoreHours') }}</th>
              <th class="right">{{ t('usage.totalTokens') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in people" :key="u.user_id">
              <td class="strong">
                <span class="sw" :style="{ background: personColor.get(u.username) ?? OTHER_C }" />{{ u.username }}
              </td>
              <td class="right num">{{ hours(u.booked_gpu_hours) }}</td>
              <td class="right num">{{ u.avg_utilization == null ? '—' : hours(u.used_gpu_hours) }}</td>
              <td>
                <div v-if="u.avg_utilization != null" class="meter">
                  <div class="meter-track"><div class="meter-fill" :style="{ width: `${Math.min(100, u.avg_utilization)}%` }" /></div>
                  <span class="num">{{ pct(u.avg_utilization) }}</span>
                </div>
                <span v-else class="faint">—</span>
              </td>
              <td class="right num">{{ memory(u.peak_memory_mb) }}</td>
              <td class="right num">{{ u.avg_utilization == null ? '—' : energy(u.energy_wh) }}</td>
              <td v-if="showCpu" class="right num">{{ hours(u.cpu_core_hours) }}</td>
              <td class="right num">{{ formatCompact(u.prompt_tokens + u.completion_tokens) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="hasData && multiNode" class="card" data-section="usage-nodes">
      <div class="card-header"><h3 class="card-title">{{ t('usage.byNode') }}</h3></div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>{{ t('usage.node') }}</th>
              <th class="right">{{ t('usage.bookedGpuHoursShort') }}</th>
              <th class="right">{{ t('usage.usedGpuHoursShort') }}</th>
              <th class="right">{{ t('usage.avgUtilizationShort') }}</th>
              <th class="right">{{ t('usage.occupied') }}</th>
              <th class="occ-col">{{ t('usage.occupancy') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="n in data!.nodes" :key="String(n.node_id)" :data-node="n.node_name">
              <td class="strong">
                {{ n.node_name }}
                <span class="faint">· {{ t('usage.gpuCount', { n: n.gpus }) }}</span>
              </td>
              <td class="right num">{{ hours(n.booked_gpu_hours) }}</td>
              <td class="right num">{{ hours(n.used_gpu_hours) }}</td>
              <td class="right num">{{ pct(n.avg_utilization) }}</td>
              <td class="right num">{{ pct(n.occupied_pct) }}</td>
              <td class="occ-col">
                <div class="occ" role="img" :aria-label="occupancyOf(n.occupancy).map((o) => `${kindName(o.kind)} ${Math.round(o.share)}%`).join(', ')">
                  <span
                    v-for="o in occupancyOf(n.occupancy)"
                    :key="o.kind"
                    :style="{ width: `${o.share}%`, background: kindColor(o.kind) }"
                    :title="`${kindName(o.kind)}: ${hours(o.hours)} h (${Math.round(o.share)}%)`"
                  />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="hasData" class="two">
      <section class="card">
        <div class="card-header"><h3 class="card-title">{{ multiLane ? t('usage.byLane') : t('usage.occupancy') }}</h3></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{{ t('usage.lane') }}</th>
                <th class="right">{{ t('usage.bookedGpuHoursShort') }}</th>
                <th class="right">{{ t('usage.avgUtilizationShort') }}</th>
                <th class="occ-col">{{ t('usage.occupancy') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="l in data!.lanes" :key="String(l.lane_id)">
                <td class="strong">
                  {{ l.lane_name }}
                  <span class="faint">· <template v-if="multiNode && l.node_name">{{ l.node_name }} · </template>{{ t('usage.gpuCount', { n: l.gpus }) }}</span>
                </td>
                <td class="right num">{{ hours(l.booked_gpu_hours) }}</td>
                <td class="right num">{{ pct(l.avg_utilization) }}</td>
                <td class="occ-col">
                  <div class="occ" role="img" :aria-label="occupancyOf(l.occupancy).map((o) => `${kindName(o.kind)} ${Math.round(o.share)}%`).join(', ')">
                    <span
                      v-for="o in occupancyOf(l.occupancy)"
                      :key="o.kind"
                      :style="{ width: `${o.share}%`, background: kindColor(o.kind) }"
                      :title="`${kindName(o.kind)}: ${hours(o.hours)} h (${Math.round(o.share)}%)`"
                    />
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="legend occ-legend">
          <span v-for="k in kindsPresent" :key="k" class="lg"><span class="sw" :style="{ background: kindColor(k) }" />{{ kindName(k) }}</span>
        </div>
      </section>
      <section class="card">
        <div class="card-header"><h3 class="card-title">{{ t('usage.byKind') }}</h3></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{{ t('usage.kind') }}</th>
                <th class="right">{{ t('usage.gpuHours') }}</th>
                <th class="right">{{ t('usage.usedGpuHoursShort') }}</th>
                <th class="right">{{ t('usage.avgUtilizationShort') }}</th>
                <th class="right">{{ t('usage.energy') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="k in data!.kinds" :key="k.kind">
                <td><span class="sw" :style="{ background: kindColor(k.kind) }" />{{ kindName(k.kind) }}</td>
                <td class="right num">{{ hours(k.gpu_hours) }}</td>
                <td class="right num">{{ data!.metered ? hours(k.used_gpu_hours) : '—' }}</td>
                <td class="right num">{{ pct(k.avg_utilization) }}</td>
                <td class="right num">{{ data!.metered ? energy(k.energy_wh) : '—' }}</td>
              </tr>
              <tr v-if="!data!.kinds.length">
                <td colspan="5" class="faint">{{ t('usage.noSamples') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <div v-if="hasData" class="two">
      <section v-if="meteredGpus.length" class="card">
        <div class="card-header"><h3 class="card-title">{{ t('usage.byGpu') }}</h3></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{{ t('usage.gpu') }}</th>
                <th class="right">{{ t('usage.usedGpuHoursShort') }}</th>
                <th class="right">{{ t('usage.avgUtilizationShort') }}</th>
                <th class="right">{{ t('usage.peakMemory') }}</th>
                <th class="right">{{ t('usage.energy') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in meteredGpus" :key="`${g.lane_id}-${g.gpu_index}`">
                <td>
                  <span class="mono">GPU {{ g.gpu_index }}</span>
                  <span v-if="multiLane" class="faint"> · {{ g.lane_name }}</span>
                </td>
                <td class="right num">{{ hours(g.used_gpu_hours) }}</td>
                <td class="right num">{{ pct(g.avg_utilization) }}</td>
                <td class="right num">
                  {{ memory(g.peak_memory_mb) }}<span v-if="g.memory_total_mb" class="faint"> / {{ memory(g.memory_total_mb) }}</span>
                </td>
                <td class="right num">{{ energy(g.energy_wh) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <div class="card-header"><h3 class="card-title">{{ t('usage.byModel') }}</h3></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{{ t('usage.model') }}</th>
                <th class="right">{{ t('usage.requests') }}</th>
                <th class="right">{{ t('usage.totalTokens') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in data!.models" :key="m.model_name">
                <td class="mono">{{ m.model_name }}</td>
                <td class="right num">{{ formatNumber(m.requests) }}</td>
                <td class="right num">{{ formatNumber(m.prompt_tokens + m.completion_tokens) }}</td>
              </tr>
              <tr v-if="!data!.models.length">
                <td colspan="3" class="faint">{{ t('usage.noRequests') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.small {
  font-size: 12.5px;
}
.tiles {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.tile-value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  margin: 4px 0 2px;
  font-variant-numeric: tabular-nums;
}
.note {
  margin: -4px 0 16px;
  font-size: 13px;
}
.usage > section.card {
  margin-bottom: 16px;
}
.card-header.wrap {
  flex-wrap: wrap;
  gap: 10px;
}
.legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--text-2);
}
.card-pad > .legend {
  margin-bottom: 10px;
}
.occ-legend {
  padding: 10px 16px 14px;
  gap: 10px 14px;
}
.lg {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.sw {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 2px;
  margin-right: 8px;
}
.lg .sw {
  margin-right: 0;
}
.two {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.two > .card:only-child {
  grid-column: 1 / -1;
}
.strong {
  font-weight: 600;
}
/* Tables scroll sideways on narrow screens instead of wrapping numbers and names. */
.table th,
.table td {
  white-space: nowrap;
}
.meter {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 120px;
}
.meter-track {
  flex: 1;
  height: 6px;
  border-radius: 3px;
  background: var(--track-line);
  overflow: hidden;
}
.meter-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--brand);
}
.occ-col {
  width: 40%;
  min-width: 120px;
}
.occ {
  display: flex;
  gap: 2px;
  height: 10px;
}
.occ span {
  display: block;
  height: 100%;
  min-width: 2px;
}
.occ span:first-child {
  border-radius: 3px 0 0 3px;
}
.occ span:last-child {
  border-radius: 0 3px 3px 0;
}
.occ span:only-child {
  border-radius: 3px;
}
@media (max-width: 1100px) {
  .tiles {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 900px) {
  .two {
    grid-template-columns: minmax(0, 1fr);
  }
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .tile-value {
    font-size: 22px;
  }
}
</style>
