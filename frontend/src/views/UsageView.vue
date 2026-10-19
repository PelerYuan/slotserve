<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NRadioButton, NRadioGroup } from 'naive-ui'
import { FileDown } from 'lucide-vue-next'
import UsageChart from '@/components/UsageChart.vue'
import { api } from '@/api/client'
import type { Usage } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { isDark } from '@/composables/theme'
import { formatCompact, formatNumber } from '@/utils/format'

// Categorical slots in fixed order (validated reference palette, stepped per theme); color follows the person.
const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6']
const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9']
const SERIES_C = computed(() => (isDark.value ? SERIES_DARK : SERIES_LIGHT))
const OTHER_C = computed(() => (isDark.value ? '#6b6b7b' : '#a3a6b8'))

const { t } = useI18n()
const { showError } = useErrorMessage()
const days = ref(30)

function exportCsv(kind: 'summary' | 'requests') {
  const a = document.createElement('a')
  a.href = api.usageExportUrl(days.value, kind)
  a.download = ''
  a.click()
}
const data = ref<Usage | null>(null)

async function load() {
  try {
    data.value = await api.usage(days.value)
  } catch (e) {
    showError(e)
  }
}
onMounted(load)
watch(days, load)

const totals = computed(() => {
  const u = data.value?.users ?? []
  return {
    requests: u.reduce((s, r) => s + r.requests, 0),
    tokens: u.reduce((s, r) => s + r.prompt_tokens + r.completion_tokens, 0),
    hours: u.reduce((s, r) => s + r.reserved_hours, 0),
  }
})

function localDate(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const dayList = computed(() => {
  const out: string[] = []
  const d = new Date()
  d.setDate(d.getDate() - days.value + 1)
  for (let i = 0; i < days.value; i++) {
    out.push(localDate(d))
    d.setDate(d.getDate() + 1)
  }
  return out
})

const colorOf = computed(() => {
  const names = [...new Set((data.value?.daily ?? []).map((r) => r.username))].sort()
  const map = new Map<string, string>()
  names.forEach((n, i) => map.set(n, i < SERIES_C.value.length ? SERIES_C.value[i] : OTHER_C.value))
  return map
})

const series = computed(() => {
  const byName = new Map<string, number[]>()
  const index = new Map(dayList.value.map((d, i) => [d, i]))
  for (const row of data.value?.daily ?? []) {
    const name = colorOf.value.get(row.username) === OTHER_C.value ? 'Other' : row.username
    if (!byName.has(name)) byName.set(name, Array(dayList.value.length).fill(0))
    const i = index.get(row.date)
    if (i != null) byName.get(name)![i] += row.tokens
  }
  return [...byName.entries()]
    .map(([name, values]) => ({ name, values, color: name === 'Other' ? OTHER_C.value : colorOf.value.get(name)! }))
    .sort((a, b) => (a.name === 'Other' ? 1 : b.name === 'Other' ? -1 : a.name.localeCompare(b.name)))
})

const hasData = computed(() => (data.value?.users.length ?? 0) > 0)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('usage.title') }}</h1>
        <p>{{ t('usage.subtitle') }}</p>
      </div>
      <div class="head-actions">
        <NRadioGroup v-model:value="days" size="small">
          <NRadioButton v-for="n in [7, 30, 90]" :key="n" :value="n">{{ t('usage.lastDays', { n }) }}</NRadioButton>
        </NRadioGroup>
        <NDropdown
          trigger="click"
          :options="[
            { key: 'summary', label: t('usage.exportSummary') },
            { key: 'requests', label: t('usage.exportRequests') },
          ]"
          @select="(k: 'summary' | 'requests') => exportCsv(k)"
        >
          <NButton size="small" secondary>
            <template #icon><FileDown :size="14" /></template>
            {{ t('usage.export') }}
          </NButton>
        </NDropdown>
      </div>
    </div>

    <div class="tiles">
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.requests') }}</div>
        <div class="tile-value">{{ formatCompact(totals.requests) }}</div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.totalTokens') }}</div>
        <div class="tile-value">{{ formatCompact(totals.tokens) }}</div>
      </div>
      <div class="card card-pad tile">
        <div class="faint small">{{ t('usage.reservedHours') }}</div>
        <div class="tile-value">{{ totals.hours.toFixed(1) }}</div>
      </div>
    </div>

    <section class="card">
      <div class="card-header">
        <h3 class="card-title">{{ t('usage.daily') }}</h3>
        <div v-if="series.length > 1" class="legend">
          <span v-for="s in series" :key="s.name" class="lg"><span class="sw" :style="{ background: s.color }" />{{ s.name }}</span>
        </div>
      </div>
      <div class="card-pad">
        <UsageChart v-if="series.length" :days="dayList" :series="series" />
        <div v-else class="empty">{{ t('usage.empty') }}</div>
      </div>
    </section>

    <div v-if="hasData" class="two">
      <section class="card">
        <div class="card-header"><h3 class="card-title">{{ t('usage.byUser') }}</h3></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>{{ t('usage.user') }}</th>
                <th class="right">{{ t('usage.requests') }}</th>
                <th class="right">{{ t('usage.promptTokens') }}</th>
                <th class="right">{{ t('usage.completionTokens') }}</th>
                <th class="right">{{ t('usage.reservedHours') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in data!.users" :key="u.user_id">
                <td class="strong">
                  <span class="sw" :style="{ background: colorOf.get(u.username) ?? OTHER_C }" />{{ u.username }}
                </td>
                <td class="right num">{{ formatNumber(u.requests) }}</td>
                <td class="right num">{{ formatNumber(u.prompt_tokens) }}</td>
                <td class="right num">{{ formatNumber(u.completion_tokens) }}</td>
                <td class="right num">{{ u.reserved_hours.toFixed(1) }}</td>
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
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.tile-value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  margin-top: 4px;
}
.legend {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--text-2);
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
  grid-template-columns: 1.4fr 1fr;
  gap: 16px;
  margin-top: 16px;
}
.strong {
  font-weight: 600;
}
@media (max-width: 900px) {
  .two,
  .tiles {
    grid-template-columns: 1fr;
  }
}
</style>
