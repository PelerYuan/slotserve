<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NDropdown, NForm, NFormItem, NInput, NModal, NProgress, useDialog } from 'naive-ui'
import { Download, FolderSearch, MoreHorizontal, RotateCw, ScrollText, Star, X } from 'lucide-vue-next'
import LogDrawer from '@/components/LogDrawer.vue'
import { api } from '@/api/client'
import type { DownloadTask, Model } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, formatDateTime, formatDuration } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError, message } = useErrorMessage()

const models = ref<Model[]>([])
const downloads = ref<DownloadTask[]>([])
const scanning = ref(false)
const showLogs = ref(false)

async function loadModels() {
  models.value = await api.models().catch((e) => (showError(e), models.value))
}
async function loadDownloads() {
  downloads.value = await api.downloads().catch(() => downloads.value)
}

let timer: number | undefined
function schedulePoll() {
  const busy = downloads.value.some((d) => d.status === 'running' || d.status === 'queued')
  timer = window.setTimeout(async () => {
    const before = downloads.value.filter((d) => d.status === 'done').length
    await loadDownloads()
    if (downloads.value.filter((d) => d.status === 'done').length !== before) await loadModels()
    schedulePoll()
  }, busy ? 1500 : 8000)
}
onMounted(async () => {
  await Promise.all([loadModels(), loadDownloads()])
  schedulePoll()
})
onBeforeUnmount(() => window.clearTimeout(timer))

async function scan() {
  scanning.value = true
  try {
    const r = await api.scanModels()
    message.success(t('models.scanResult', { added: r.added.length, skipped: r.skipped }))
    await loadModels()
  } catch (e) {
    showError(e)
  } finally {
    scanning.value = false
  }
}

const canEdit = (m: Model) => session.user?.is_admin || m.created_by === session.user?.id

function menuFor(m: Model) {
  const items = []
  if (canEdit(m)) items.push({ key: 'edit', label: t('common.edit') })
  if (session.user?.is_admin)
    items.push(
      m.is_default
        ? { key: 'unset-default', label: t('models.unsetDefault') }
        : { key: 'default', label: t('models.setDefault'), disabled: m.status !== 'ready' || !m.file_exists },
    )
  if (canEdit(m)) items.push({ type: 'divider', key: 'd' }, { key: 'delete', label: t('common.delete'), props: { style: 'color:#e5484d' } })
  return items
}

async function onMenu(key: string, m: Model) {
  try {
    if (key === 'edit') openEdit(m)
    else if (key === 'default') await api.setDefault(m.id)
    else if (key === 'unset-default') await api.clearDefault(m.id)
    else if (key === 'delete') return confirmDelete(m)
    await loadModels()
  } catch (e) {
    showError(e)
  }
}

// ---- edit ----

const editing = ref<Model | null>(null)
const form = reactive({ name: '', args: '', description: '' })
const saving = ref(false)

function openEdit(m: Model) {
  editing.value = m
  form.name = m.name
  form.args = m.args
  form.description = m.description
}

async function save() {
  if (!editing.value) return
  saving.value = true
  try {
    await api.updateModel(editing.value.id, { name: form.name.trim(), args: form.args, description: form.description })
    editing.value = null
    await loadModels()
  } catch (e) {
    showError(e)
  } finally {
    saving.value = false
  }
}

// ---- delete ----

function confirmDelete(m: Model) {
  const withFile = ref(false)
  dialog.error({
    title: t('models.deleteTitle'),
    content: () =>
      h('div', [
        h('p', { style: 'margin:0 0 12px' }, t('models.deleteConfirm', { name: m.name })),
        h(NCheckbox, { checked: withFile.value, 'onUpdate:checked': (v: boolean) => (withFile.value = v) }, () =>
          t('models.deleteFile'),
        ),
      ]),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.deleteModel(m.id, withFile.value)
        await loadModels()
      } catch (e) {
        showError(e)
      }
    },
  })
}

// ---- downloads ----

const dl = reactive({ url: '', filename: '' })
const submitting = ref(false)
const guessedName = computed(() => {
  try {
    return decodeURIComponent(new URL(dl.url).pathname.split('/').pop() ?? '')
  } catch {
    return ''
  }
})

async function startDownload() {
  if (!dl.url.trim()) return
  submitting.value = true
  try {
    await api.createDownload(dl.url.trim(), dl.filename.trim() || undefined)
    dl.url = ''
    dl.filename = ''
    await loadDownloads()
  } catch (e) {
    showError(e)
  } finally {
    submitting.value = false
  }
}

async function downloadAction(d: DownloadTask, action: 'retry' | 'remove') {
  try {
    if (action === 'retry') await api.retryDownload(d.id)
    else await api.removeDownload(d.id)
    await loadDownloads()
  } catch (e) {
    showError(e)
  }
}

function pct(d: DownloadTask) {
  return d.total_bytes ? Math.min(100, (d.downloaded_bytes / d.total_bytes) * 100) : 0
}
function eta(d: DownloadTask) {
  if (!d.total_bytes || !d.speed_bps) return null
  return (d.total_bytes - d.downloaded_bytes) / d.speed_bps
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h1>{{ t('models.title') }}</h1>
        <p>{{ t('models.subtitle') }}</p>
      </div>
      <div class="row">
        <NButton quaternary @click="showLogs = true">
          <template #icon><ScrollText :size="16" /></template>
          {{ t('overview.viewLogs') }}
        </NButton>
        <NButton secondary :loading="scanning" @click="scan">
          <template #icon><FolderSearch :size="16" /></template>
          {{ t('models.scan') }}
        </NButton>
      </div>
    </div>

    <section class="card">
      <div v-if="!models.length" class="empty">{{ t('models.empty') }}</div>
      <ul v-else class="models">
        <li v-for="m in models" :key="m.id">
          <div class="m-main">
            <div class="m-name">
              <span>{{ m.name }}</span>
              <span v-if="m.is_default" class="badge brand"><Star :size="11" />{{ t('models.default') }}</span>
              <span v-if="m.is_loaded" class="badge ok">{{ t('models.loaded') }}</span>
              <span v-if="!m.file_exists" class="badge danger">{{ t('models.missingFile') }}</span>
            </div>
            <div class="m-file mono faint" :title="m.file_path">{{ m.file_name }}</div>
            <div v-if="m.description" class="m-desc muted">{{ m.description }}</div>
            <code class="m-args" :title="m.args">{{ m.args || '—' }}</code>
          </div>
          <div class="m-stats">
            <div>
              <div class="faint small">{{ t('models.size') }}</div>
              <div class="num stat">{{ formatBytes(m.size_bytes) }}</div>
            </div>
            <div>
              <div class="faint small">{{ t('models.estLoad') }}</div>
              <div class="num stat">{{ formatDuration(m.est_load_seconds) }}</div>
            </div>
            <div class="hide-sm">
              <div class="faint small">{{ t('models.owner') }}</div>
              <div class="stat">{{ m.created_by_name ?? '—' }}</div>
            </div>
          </div>
          <NDropdown v-if="menuFor(m).length" trigger="click" :options="menuFor(m)" placement="bottom-end" @select="(k: string) => onMenu(k, m)">
            <NButton quaternary circle size="small"><MoreHorizontal :size="17" /></NButton>
          </NDropdown>
        </li>
      </ul>
    </section>

    <section class="card dl-card">
      <div class="card-header">
        <div>
          <h3 class="card-title">{{ t('models.downloads') }}</h3>
          <div class="card-sub">{{ t('models.downloadsHint') }}</div>
        </div>
      </div>
      <form class="dl-form card-pad" @submit.prevent="startDownload">
        <NInput v-model:value="dl.url" :placeholder="'https://huggingface.co/<repo>/resolve/main/<file>.gguf'" class="dl-url" clearable />
        <NInput v-model:value="dl.filename" :placeholder="guessedName || t('models.filenameHint')" class="dl-name" />
        <NButton type="primary" attr-type="submit" :loading="submitting" :disabled="!dl.url.trim()">
          <template #icon><Download :size="16" /></template>
          {{ t('models.startDownload') }}
        </NButton>
      </form>
      <div v-if="!downloads.length" class="empty slim">{{ t('models.noDownloads') }}</div>
      <ul v-else class="dls">
        <li v-for="d in downloads" :key="d.id">
          <div class="dl-top">
            <span class="mono dl-file">{{ d.filename }}</span>
            <span class="dl-status" :class="d.status">{{ t(`models.dl.${d.status}`) }}</span>
            <span class="spacer" />
            <span class="faint small num">
              <template v-if="d.status === 'running'">
                {{ formatBytes(d.downloaded_bytes) }} / {{ formatBytes(d.total_bytes) }}
                <template v-if="d.speed_bps"> · {{ formatBytes(d.speed_bps) }}/s</template>
                <template v-if="eta(d)"> · {{ formatDuration(eta(d)) }}</template>
              </template>
              <template v-else-if="d.status === 'done'">{{ formatBytes(d.total_bytes ?? d.downloaded_bytes) }}</template>
              <template v-else>{{ d.username }} · {{ formatDateTime(d.created_at) }}</template>
            </span>
            <NButton v-if="d.status === 'failed' || d.status === 'cancelled'" quaternary circle size="tiny" :title="t('common.retry')" @click="downloadAction(d, 'retry')">
              <RotateCw :size="14" />
            </NButton>
            <NButton quaternary circle size="tiny" :title="d.status === 'running' || d.status === 'queued' ? t('common.cancel') : t('common.remove')" @click="downloadAction(d, 'remove')">
              <X :size="14" />
            </NButton>
          </div>
          <NProgress
            v-if="d.status === 'running' || (d.status !== 'done' && d.downloaded_bytes > 0)"
            type="line"
            :percentage="pct(d)"
            :show-indicator="false"
            :height="5"
            :color="d.status === 'running' ? '#4f46e5' : '#c7c7d1'"
            rail-color="#ececf4"
          />
          <div v-if="d.error" class="dl-err small">{{ d.error }}</div>
        </li>
      </ul>
    </section>

    <NModal
      :show="!!editing"
      preset="card"
      :title="t('models.editTitle')"
      style="width: 600px; max-width: calc(100vw - 32px)"
      :bordered="false"
      @update:show="(v: boolean) => !v && (editing = null)"
    >
      <NForm :show-require-mark="false">
        <NFormItem :label="t('models.name')">
          <NInput v-model:value="form.name" />
        </NFormItem>
        <NFormItem :label="t('models.args')" :feedback="t('models.argsHint')">
          <NInput v-model:value="form.args" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" class="mono-input" placeholder="-ngl 99 -c 8192 --flash-attn on" />
        </NFormItem>
        <NFormItem :label="t('models.description')">
          <NInput v-model:value="form.description" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" />
        </NFormItem>
        <p v-if="editing?.is_loaded" class="faint small">{{ t('models.reloadHint') }}</p>
      </NForm>
      <template #footer>
        <div class="footer">
          <NButton @click="editing = null">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="saving" :disabled="!form.name.trim()" @click="save">{{ t('common.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <LogDrawer v-model:show="showLogs" />
  </div>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.models {
  list-style: none;
  margin: 0;
  padding: 0;
}
.models li {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 18px 20px;
}
.models li + li {
  border-top: 1px solid var(--border);
}
.m-main {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 3px;
}
.m-name {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-weight: 600;
  font-size: 15px;
  letter-spacing: -0.01em;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 5px;
  letter-spacing: 0;
}
.badge.brand {
  background: var(--brand-soft);
  color: var(--brand);
}
.badge.ok {
  background: var(--ok-soft);
  color: #08875d;
}
.badge.danger {
  background: var(--danger-soft);
  color: #c4282d;
}
.m-file {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.m-desc {
  font-size: 13px;
}
.m-args {
  margin-top: 6px;
  justify-self: start;
  max-width: 100%;
  background: var(--surface-muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 11.5px;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.m-stats {
  display: flex;
  gap: 28px;
  flex: none;
}
.m-stats > div {
  min-width: 72px;
}
.stat {
  font-weight: 600;
  margin-top: 1px;
}
.dl-card {
  margin-top: 16px;
}
.dl-form {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--border);
}
.dl-url {
  flex: 3;
}
.dl-name {
  flex: 1.2;
}
.empty.slim {
  padding: 24px;
}
.dls {
  list-style: none;
  margin: 0;
  padding: 0;
}
.dls li {
  padding: 12px 20px;
  display: grid;
  gap: 8px;
}
.dls li + li {
  border-top: 1px solid var(--border);
}
.dl-top {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.dl-file {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.dl-status {
  font-size: 11.5px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 5px;
  background: #f0f0f3;
  color: var(--text-2);
  flex: none;
}
.dl-status.running {
  background: var(--brand-soft);
  color: var(--brand);
}
.dl-status.done {
  background: var(--ok-soft);
  color: #08875d;
}
.dl-status.failed {
  background: var(--danger-soft);
  color: #c4282d;
}
.dl-err {
  color: #c4282d;
}
.mono-input :deep(textarea) {
  font-family: var(--mono);
  font-size: 12.5px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 760px) {
  .models li {
    flex-wrap: wrap;
    gap: 12px;
  }
  .m-main {
    flex-basis: calc(100% - 48px);
  }
  .m-stats {
    order: 3;
    width: 100%;
  }
  .hide-sm {
    display: none;
  }
  .dl-form {
    flex-direction: column;
  }
}
</style>
