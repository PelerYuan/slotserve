<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NPagination, NSwitch } from 'naive-ui'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { RequestLog } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, formatNumber } from '@/utils/format'

const PAGE = 50
const { t } = useI18n()
const { showError } = useErrorMessage()

const items = ref<RequestLog[]>([])
const total = ref(0)
const page = ref(1)
const errorsOnly = ref(false)
const loaded = ref(false)

async function load() {
  try {
    const res = await api.admin.requests(PAGE, (page.value - 1) * PAGE, errorsOnly.value)
    items.value = res.items
    total.value = res.total
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(load)
watch(page, load)
watch(errorsOnly, () => (page.value === 1 ? load() : (page.value = 1)))

const tone = (code: number) => (code >= 500 ? 'danger' : code >= 400 ? 'warn' : 'ok')
const duration = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`)
</script>

<template>
  <div class="toolbar">
    <label class="switch">
      <NSwitch v-model:value="errorsOnly" size="small" />
      <span>{{ t('admin.requests.errorsOnly') }}</span>
    </label>
    <NButton size="small" quaternary @click="load">
      <template #icon><RefreshCw :size="14" /></template>
      {{ t('common.refresh') }}
    </NButton>
  </div>
  <section class="card">
    <div v-if="loaded && !items.length" class="empty">{{ t('admin.requests.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.requests.time') }}</th>
            <th>{{ t('admin.requests.user') }}</th>
            <th>{{ t('admin.requests.model') }}</th>
            <th>{{ t('admin.requests.endpoint') }}</th>
            <th>{{ t('admin.requests.status') }}</th>
            <th class="right">{{ t('admin.requests.duration') }}</th>
            <th class="right">{{ t('admin.requests.tokens') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in items" :key="r.id">
            <td class="muted num nowrap">{{ formatDateTime(r.created_at) }}</td>
            <td class="nowrap">
              <span class="strong">{{ r.username }}</span>
              <span v-if="r.key_name" class="faint small"> · {{ r.key_name }}</span>
            </td>
            <td class="mono small">{{ r.model_name || '—' }}</td>
            <td class="mono small nowrap">
              {{ r.endpoint }}<span v-if="r.stream" class="faint"> · stream</span>
            </td>
            <td><span class="badge" :class="tone(r.status_code)">{{ r.status_code }}</span></td>
            <td class="right num muted nowrap">{{ duration(r.duration_ms) }}</td>
            <td class="right num nowrap">
              {{ formatNumber(r.prompt_tokens) }} <span class="faint">→</span> {{ formatNumber(r.completion_tokens) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="total > PAGE" class="pager">
      <NPagination v-model:page="page" :page-size="PAGE" :item-count="total" />
    </div>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
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
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
</style>
