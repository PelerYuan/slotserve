<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NModal, useDialog } from 'naive-ui'
import { KeyRound, Plus } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { ApiKey, ApiKeyCreated } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()

const keys = ref<ApiKey[]>([])
const creating = ref(false)
const newName = ref('')
const busy = ref(false)
const created = ref<ApiKeyCreated | null>(null)
const baseUrl = computed(() => `${window.location.origin}/v1`)

async function load() {
  keys.value = await api.keys().catch((e) => (showError(e), keys.value))
}
onMounted(load)

// Shown again on request, when admins allow keeping a copy (Admin > System settings).
const revealed = ref<{ name: string; key: string } | null>(null)
async function reveal(k: ApiKey) {
  try {
    revealed.value = { name: k.name, key: (await api.revealKey(k.id)).key }
  } catch (e) {
    showError(e)
  }
}

async function create() {
  if (!newName.value.trim()) return
  busy.value = true
  try {
    created.value = await api.createKey(newName.value.trim())
    creating.value = false
    newName.value = ''
    await load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

function revoke(k: ApiKey) {
  dialog.warning({
    title: t('keys.revoke'),
    content: t('keys.revokeConfirm', { name: k.name }),
    positiveText: t('keys.revoke'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.revokeKey(k.id)
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('keys.title') }}</h1>
        <p>{{ t('keys.subtitle') }}</p>
      </div>
      <NButton type="primary" @click="creating = true">
        <template #icon><Plus :size="16" /></template>
        {{ t('keys.create') }}
      </NButton>
    </div>

    <section class="card endpoint card-pad">
      <div class="label">{{ t('keys.endpoint') }}</div>
      <CodeBlock :code="baseUrl" inline />
    </section>

    <section class="card">
      <div v-if="!keys.length" class="empty">
        <KeyRound :size="28" :stroke-width="1.5" class="empty-icon" />
        <div>{{ t('keys.empty') }}</div>
      </div>
      <div v-else class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>{{ t('keys.name') }}</th>
              <th>{{ t('keys.prefix') }}</th>
              <th>{{ t('keys.created') }}</th>
              <th>{{ t('keys.lastUsed') }}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="k in keys" :key="k.id">
              <td class="strong">{{ k.name }}</td>
              <td><code class="prefix">{{ k.prefix }}••••••••</code></td>
              <td class="muted num">{{ formatDateTime(k.created_at) }}</td>
              <td class="muted">{{ k.last_used_at ? relativeFromNow(k.last_used_at) : t('keys.never') }}</td>
              <td class="right nowrap">
                <NButton v-if="k.revealable" size="small" quaternary @click="reveal(k)">{{ t('keys.reveal') }}</NButton>
                <NButton size="small" quaternary type="error" @click="revoke(k)">{{ t('keys.revoke') }}</NButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <NModal v-model:show="creating" preset="card" :title="t('keys.create')" style="width: 440px; max-width: calc(100vw - 32px)" :bordered="false">
      <NInput v-model:value="newName" :placeholder="t('keys.namePlaceholder')" maxlength="64" autofocus @keyup.enter="create" />
      <template #footer>
        <div class="footer">
          <NButton @click="creating = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :disabled="!newName.trim()" :loading="busy" @click="create">{{ t('common.create') }}</NButton>
        </div>
      </template>
    </NModal>

    <NModal
      :show="!!created"
      preset="card"
      :title="t('keys.createdTitle')"
      style="width: 560px; max-width: calc(100vw - 32px)"
      :bordered="false"
      :mask-closable="false"
      @update:show="(v: boolean) => !v && (created = null)"
    >
      <p class="muted hint">{{ created?.revealable ? t('keys.createdHintRevealable') : t('keys.createdHint') }}</p>
      <CodeBlock v-if="created" :code="created.key" inline />
      <template #footer>
        <div class="footer">
          <NButton type="primary" @click="created = null">{{ t('common.close') }}</NButton>
        </div>
      </template>
    </NModal>

    <NModal
      :show="!!revealed"
      preset="card"
      :title="revealed?.name"
      style="width: 560px; max-width: calc(100vw - 32px)"
      :bordered="false"
      @update:show="(v: boolean) => !v && (revealed = null)"
    >
      <CodeBlock v-if="revealed" :code="revealed.key" inline />
      <template #footer>
        <div class="footer">
          <NButton type="primary" @click="revealed = null">{{ t('common.close') }}</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.nowrap {
  white-space: nowrap;
}
.endpoint {
  margin-bottom: 16px;
  display: grid;
  gap: 8px;
}
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
.empty-icon {
  color: var(--text-3);
  margin-bottom: 8px;
}
.hint {
  margin: 0 0 14px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
