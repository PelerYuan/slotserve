<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NPagination } from 'naive-ui'
import { api } from '@/api/client'
import type { AuditEntry } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime } from '@/utils/format'

const PAGE = 30
const { t, te } = useI18n()
const { showError } = useErrorMessage()

const items = ref<AuditEntry[]>([])
const total = ref(0)
const page = ref(1)
const loaded = ref(false)

async function load() {
  try {
    const res = await api.admin.audit(PAGE, (page.value - 1) * PAGE)
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

const actionLabel = (a: string) => {
  const key = `admin.audit.actions.${a.replace('.', '_')}`
  return te(key) ? t(key) : a
}

// An empty GPU list means every visible GPU; say so instead of showing "gpus=".
const noGpus = (k: string, v: unknown) => k === 'gpus' && (v === '' || v === null || (Array.isArray(v) && !v.length))

function detail(d: Record<string, unknown>) {
  return Object.entries(d)
    .map(([k, v]) => `${k}=${noGpus(k, v) ? t('admin.audit.allGpus') : typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('  ')
}
</script>

<template>
  <section class="card">
    <div v-if="loaded && !items.length" class="empty">{{ t('admin.audit.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.audit.time') }}</th>
            <th>{{ t('admin.audit.actor') }}</th>
            <th>{{ t('admin.audit.action') }}</th>
            <th>{{ t('admin.audit.target') }}</th>
            <th>{{ t('admin.audit.detail') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in items" :key="e.id">
            <td class="muted num nowrap">{{ formatDateTime(e.created_at) }}</td>
            <td class="strong">{{ e.actor_name || t('admin.audit.system') }}</td>
            <td><span class="badge" :class="{ danger: /delete|disable|revoke/.test(e.action) }">{{ actionLabel(e.action) }}</span></td>
            <td class="mono small">{{ e.target || '—' }}</td>
            <td class="faint mono small detail" :title="detail(e.detail)">{{ detail(e.detail) || '—' }}</td>
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
.strong {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.nowrap {
  white-space: nowrap;
}
.detail {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}
</style>
