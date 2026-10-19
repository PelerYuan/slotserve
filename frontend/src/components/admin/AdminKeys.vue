<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useDialog } from 'naive-ui'
import { api } from '@/api/client'
import type { AdminApiKey } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()

const keys = ref<AdminApiKey[]>([])
const loaded = ref(false)

async function load() {
  try {
    keys.value = await api.admin.keys()
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(load)

function revoke(k: AdminApiKey) {
  dialog.warning({
    title: t('admin.keys.revoke'),
    content: t('admin.keys.revokeConfirm', { user: k.username, name: k.name }),
    positiveText: t('admin.keys.revoke'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.admin.revokeKey(k.id)
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <section class="card">
    <div v-if="loaded && !keys.length" class="empty">{{ t('admin.keys.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.keys.user') }}</th>
            <th>{{ t('admin.keys.name') }}</th>
            <th>{{ t('admin.keys.prefix') }}</th>
            <th>{{ t('admin.keys.created') }}</th>
            <th>{{ t('admin.keys.lastUsed') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="k in keys" :key="k.id">
            <td class="strong">{{ k.username }}</td>
            <td>{{ k.name }}</td>
            <td><code class="prefix">{{ k.prefix }}••••••••</code></td>
            <td class="muted num">{{ formatDateTime(k.created_at) }}</td>
            <td class="muted">{{ k.last_used_at ? relativeFromNow(k.last_used_at) : t('keys.never') }}</td>
            <td class="right">
              <NButton size="small" quaternary type="error" @click="revoke(k)">{{ t('admin.keys.revoke') }}</NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.strong {
  font-weight: 600;
}
.prefix {
  background: var(--surface-muted);
  border: 1px solid var(--border);
  padding: 2px 8px;
  border-radius: 6px;
  color: var(--text-2);
}
</style>
