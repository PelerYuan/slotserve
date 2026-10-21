<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NDropdown, NForm, NFormItem, NInput, NModal, NProgress, NTab, NTabPane, NTabs, useDialog } from 'naive-ui'
import { Download, FolderSearch, MoreHorizontal, Plus, RotateCw, ScrollText, Search, Star, Trash2, X } from 'lucide-vue-next'
import { NRadioButton, NRadioGroup } from 'naive-ui'
import LogDrawer from '@/components/LogDrawer.vue'
import ModelCatalog from '@/components/catalog/ModelCatalog.vue'
import { api } from '@/api/client'
import type { DownloadTask, HfFile, Model } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { useLanes } from '@/composables/lanes'
import { formatBytes, formatDateTime, formatDuration } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const route = useRoute()
const router = useRouter()
const view = computed<'installed' | 'catalog'>({
  get: () => (route.query.tab === 'catalog' ? 'catalog' : 'installed'),
  set: (v) => router.replace({ query: v === 'catalog' ? { tab: 'catalog' } : {} }),
})
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

async function refreshAll() {
  await Promise.all([loadModels(), loadDownloads()])
  window.clearTimeout(timer)
  schedulePoll()
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

const { lanes, multi, nameOf } = useLanes()
const defaultLabel = (m: Model) =>
  multi.value ? `${t('models.default')} · ${m.default_lane_ids.map(nameOf).join(', ')}` : t('models.default')

const canEdit = (m: Model) => session.user?.is_admin || m.created_by === session.user?.id

function menuFor(m: Model) {
  const items = []
  if (canEdit(m)) items.push({ key: 'edit', label: t('common.edit') })
  if (session.user?.is_admin)
    items.push(
      m.is_default
        ? { key: 'unset-default', label: t('models.unsetDefault') }
        : {
            key: 'default',
            label: multi.value ? t('models.setDefaultLane', { lane: lanes.value[0]?.name }) : t('models.setDefault'),
            disabled: m.status !== 'ready' || !m.file_exists,
          },
    )
  if (canEdit(m)) items.push({ type: 'divider', key: 'd' }, { key: 'delete', label: t('common.delete'), props: { style: 'color:var(--danger)' } })
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
const form = reactive({ name: '', args: '', description: '', engine: 'llama.cpp' as Model['engine'], path: '' })
const creating = ref(false)
const ENGINE_OPTIONS = ['llama.cpp', 'vllm', 'sglang', 'custom'] as const
const engineLabel: Record<string, string> = { 'llama.cpp': 'llama.cpp', vllm: 'vLLM', sglang: 'SGLang', custom: 'Custom' }
const argsPlaceholder = computed(
  () =>
    ({
      'llama.cpp': '-ngl 99 -c 8192 --flash-attn on',
      vllm: '--max-model-len 32768 --gpu-memory-utilization 0.9',
      sglang: '--context-length 32768 --mem-fraction-static 0.85',
      custom: '--max-batch-size 8',
    })[form.engine],
)
const pathPlaceholder = computed(() =>
  form.engine === 'llama.cpp' ? '/models/Qwen3-8B-Q4_K_M.gguf' : 'Qwen/Qwen3-8B  ·  /models/qwen3-8b',
)

function openCreate() {
  Object.assign(form, { name: '', args: '', description: '', engine: 'llama.cpp', path: '' })
  presetRows.value = []
  creating.value = true
}
const saving = ref(false)

function openEdit(m: Model) {
  editing.value = m
  form.name = m.name
  form.args = m.args
  form.description = m.description
  form.engine = m.engine
  form.path = m.file_path
  presetRows.value = m.presets.map((p) => ({ id: p.id, name: p.name, args: p.args, saved: { ...p } }))
}

// ---- presets ----

interface PresetRow {
  id: number | null
  name: string
  args: string
  saved: { name: string; args: string } | null
}
const presetRows = ref<PresetRow[]>([])
const presetBusy = ref(false)

function addPresetRow() {
  presetRows.value.push({ id: null, name: '', args: form.args, saved: null })
}

const rowDirty = (r: PresetRow) => !r.saved || r.saved.name !== r.name || r.saved.args !== r.args

async function savePreset(r: PresetRow) {
  if (!editing.value || !r.name.trim()) return
  presetBusy.value = true
  try {
    const saved = r.id
      ? await api.updatePreset(editing.value.id, r.id, r.name.trim(), r.args)
      : await api.createPreset(editing.value.id, r.name.trim(), r.args)
    Object.assign(r, { id: saved.id, name: saved.name, args: saved.args, saved: { name: saved.name, args: saved.args } })
    await loadModels()
  } catch (e) {
    showError(e)
  } finally {
    presetBusy.value = false
  }
}

async function removePreset(r: PresetRow) {
  if (r.id && editing.value) {
    try {
      await api.deletePreset(editing.value.id, r.id)
      await loadModels()
    } catch (e) {
      return showError(e)
    }
  }
  presetRows.value = presetRows.value.filter((x) => x !== r)
}

async function save() {
  saving.value = true
  try {
    if (creating.value) {
      await api.createModel({
        name: form.name.trim(),
        engine: form.engine,
        path: form.path.trim(),
        args: form.args,
        description: form.description,
      })
      creating.value = false
    } else if (editing.value) {
      const moved = form.engine !== editing.value.engine || form.path.trim() !== editing.value.file_path
      await api.updateModel(editing.value.id, {
        name: form.name.trim(),
        args: form.args,
        description: form.description,
        ...(moved ? { engine: form.engine, path: form.path.trim() } : {}),
      })
      editing.value = null
    }
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

// ---- Hugging Face ----

const dlTab = ref<'url' | 'hf'>('url')
const hf = reactive({ repo: '', revision: 'main', files: [] as HfFile[], picked: [] as string[], listing: false, error: '' })

function hfRepoFromInput(text: string) {
  const m = text.trim().match(/(?:huggingface\.co\/|hf-mirror\.com\/)?([\w.-]+\/[\w.-]+)/)
  return m ? m[1] : text.trim()
}

async function hfList() {
  hf.repo = hfRepoFromInput(hf.repo)
  if (!hf.repo) return
  hf.listing = true
  hf.error = ''
  hf.files = []
  hf.picked = []
  try {
    const res = await api.hfFiles(hf.repo, hf.revision || 'main')
    hf.files = res.files
  } catch (e) {
    showError(e)
  } finally {
    hf.listing = false
  }
}

function togglePick(path: string, on: boolean) {
  hf.picked = on ? [...hf.picked, path] : hf.picked.filter((p) => p !== path)
}

const pickedSize = computed(() =>
  hf.files.filter((f) => hf.picked.includes(f.path)).reduce((sum, f) => sum + f.size_bytes, 0),
)

async function hfDownload() {
  const files = hf.files.filter((f) => hf.picked.includes(f.path)).flatMap((f) => f.files)
  if (!files.length) return
  submitting.value = true
  try {
    await api.hfDownload(hf.repo, files, hf.revision || 'main')
    message.success(t('models.hf.queued', { n: files.length }))
    hf.picked = []
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
        <NButton secondary @click="openCreate">
          <template #icon><Plus :size="16" /></template>
          {{ t('models.add') }}
        </NButton>
        <NButton secondary :loading="scanning" @click="scan">
          <template #icon><FolderSearch :size="16" /></template>
          {{ t('models.scan') }}
        </NButton>
      </div>
    </div>

    <NTabs v-model:value="view" type="line" class="view-tabs" :tabs-padding="0">
      <NTab name="installed">{{ t('models.tabs.installed') }}</NTab>
      <NTab name="catalog">{{ t('models.tabs.catalog') }}</NTab>
    </NTabs>

    <ModelCatalog v-if="view === 'catalog'" @changed="refreshAll" />

    <section v-else class="card">
      <div v-if="!models.length" class="empty">{{ t('models.empty') }}</div>
      <ul v-else class="models">
        <li v-for="m in models" :key="m.id">
          <div class="m-main">
            <div class="m-name">
              <span>{{ m.name }}</span>
              <span v-if="m.is_default" class="badge brand"><Star :size="11" />{{ defaultLabel(m) }}</span>
              <span v-if="m.engine !== 'llama.cpp'" class="badge">{{ engineLabel[m.engine] }}</span>
              <span v-if="m.is_loaded" class="badge ok">{{ t('models.loaded') }}</span>
              <span v-if="!m.file_exists" class="badge danger">{{ t('models.missingFile') }}</span>
              <span v-if="m.exceeds_vram" class="badge warn" :title="t('models.exceedsVramHint')">{{ t('models.exceedsVram') }}</span>
            </div>
            <div class="m-file mono faint" :title="m.file_path">{{ m.file_name }}</div>
            <div v-if="m.description" class="m-desc muted">{{ m.description }}</div>
            <code class="m-args" :title="m.args">{{ m.args || '—' }}</code>
            <div v-if="m.presets.length" class="m-presets">
              <span class="faint small">{{ t('models.presets') }}</span>
              <span v-for="p in m.presets" :key="p.id" class="badge" :title="p.args">{{ p.name }}</span>
            </div>
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
      <NTabs v-model:value="dlTab" type="line" size="small" class="dl-tabs" :tabs-padding="20">
        <NTabPane name="url" :tab="t('models.hf.tabUrl')">
          <form class="dl-form card-pad" @submit.prevent="startDownload">
            <NInput v-model:value="dl.url" :placeholder="'https://huggingface.co/<repo>/resolve/main/<file>.gguf'" class="dl-url" clearable />
            <NInput v-model:value="dl.filename" :placeholder="guessedName || t('models.filenameHint')" class="dl-name" />
            <NButton type="primary" attr-type="submit" :loading="submitting" :disabled="!dl.url.trim()">
              <template #icon><Download :size="16" /></template>
              {{ t('models.startDownload') }}
            </NButton>
          </form>
        </NTabPane>
        <NTabPane name="hf" tab="Hugging Face">
          <form class="dl-form card-pad" @submit.prevent="hfList">
            <NInput v-model:value="hf.repo" placeholder="Qwen/Qwen3-8B-GGUF" class="dl-url" clearable />
            <NInput v-model:value="hf.revision" placeholder="main" class="dl-rev" />
            <NButton secondary attr-type="submit" :loading="hf.listing" :disabled="!hf.repo.trim()">
              <template #icon><Search :size="16" /></template>
              {{ t('models.hf.list') }}
            </NButton>
          </form>
          <div v-if="hf.files.length" class="hf-files">
            <label v-for="f in hf.files" :key="f.path" class="hf-file" :class="{ on: hf.picked.includes(f.path) }">
              <NCheckbox :checked="hf.picked.includes(f.path)" @update:checked="(v: boolean) => togglePick(f.path, v)" />
              <span class="mono hf-path">{{ f.path }}</span>
              <span v-if="f.parts > 1" class="badge">{{ t('models.hf.parts', { n: f.parts }) }}</span>
              <span v-if="f.kind === 'mmproj'" class="badge">mmproj</span>
              <span class="spacer" />
              <span class="num faint small">{{ formatBytes(f.size_bytes) }}</span>
            </label>
            <div class="hf-actions">
              <span class="faint small">{{ t('models.hf.selected', { n: hf.picked.length, size: formatBytes(pickedSize) }) }}</span>
              <NButton type="primary" :disabled="!hf.picked.length" :loading="submitting" @click="hfDownload">
                <template #icon><Download :size="16" /></template>
                {{ t('models.startDownload') }}
              </NButton>
            </div>
          </div>
        </NTabPane>
      </NTabs>
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
            :color="d.status === 'running' ? 'var(--brand)' : 'var(--border-strong)'"
            rail-color="var(--rail)"
          />
          <div v-if="d.error" class="dl-err small">{{ d.error }}</div>
        </li>
      </ul>
    </section>

    <NModal
      :show="!!editing || creating"
      preset="card"
      :title="creating ? t('models.addTitle') : t('models.editTitle')"
      style="width: 600px; max-width: calc(100vw - 32px)"
      :bordered="false"
      @update:show="(v: boolean) => !v && ((editing = null), (creating = false))"
    >
      <NForm :show-require-mark="false">
        <NFormItem :label="t('models.name')">
          <NInput v-model:value="form.name" />
        </NFormItem>
        <NFormItem :label="t('models.engine')">
          <NRadioGroup v-model:value="form.engine" size="small">
            <NRadioButton v-for="e in ENGINE_OPTIONS" :key="e" :value="e">{{ engineLabel[e] }}</NRadioButton>
          </NRadioGroup>
        </NFormItem>
        <NFormItem :label="t('models.path')" :feedback="t(`models.pathHint.${form.engine}`)">
          <NInput v-model:value="form.path" class="mono-input" :placeholder="pathPlaceholder" />
        </NFormItem>
        <NFormItem :label="t('models.args')" :feedback="t('models.argsHint')">
          <NInput v-model:value="form.args" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" class="mono-input" :placeholder="argsPlaceholder" />
        </NFormItem>
        <NFormItem :label="t('models.description')">
          <NInput v-model:value="form.description" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" />
        </NFormItem>
        <p v-if="editing?.is_loaded" class="faint small">{{ t('models.reloadHint') }}</p>
      </NForm>
      <div v-if="!creating" class="presets">
        <div class="presets-head">
          <div>
            <div class="presets-title">{{ t('models.presets') }}</div>
            <div class="faint small">{{ t('models.presetsHint') }}</div>
          </div>
          <NButton size="small" secondary @click="addPresetRow">
            <template #icon><Plus :size="14" /></template>
            {{ t('models.addPreset') }}
          </NButton>
        </div>
        <div v-for="(r, i) in presetRows" :key="r.id ?? `new-${i}`" class="preset-row">
          <NInput v-model:value="r.name" size="small" :placeholder="t('models.presetName')" class="preset-name" maxlength="64" />
          <NInput v-model:value="r.args" size="small" class="preset-args mono-input" placeholder="-c 32768" />
          <NButton size="small" type="primary" secondary :disabled="!rowDirty(r) || !r.name.trim()" :loading="presetBusy" @click="savePreset(r)">
            {{ t('common.save') }}
          </NButton>
          <NButton size="small" quaternary circle @click="removePreset(r)"><Trash2 :size="14" /></NButton>
        </div>
      </div>
      <template #footer>
        <div class="footer">
          <NButton @click="(editing = null), (creating = false)">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="saving" :disabled="!form.name.trim() || !form.path.trim()" @click="save">{{ t('common.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <LogDrawer v-model:show="showLogs" />
  </div>
</template>

<style scoped>
.view-tabs {
  margin: -4px 0 16px;
}
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
  color: var(--ok-text);
}
.badge.danger {
  background: var(--danger-soft);
  color: var(--danger-text);
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
.m-presets {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.dl-tabs :deep(.n-tabs-nav) {
  padding-top: 4px;
}
.dl-rev {
  width: 120px;
  flex: none;
}
.hf-files {
  margin: -6px 20px 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.hf-file {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.hf-file.on {
  background: var(--brand-softer);
}
.hf-path {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.hf-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  background: var(--surface-muted);
}
.presets {
  margin-top: 8px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
  display: grid;
  gap: 8px;
}
.presets-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}
.presets-title {
  font-weight: 600;
}
.preset-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.preset-name {
  width: 140px;
  flex: none;
}
.preset-args {
  flex: 1;
  min-width: 0;
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
  background: var(--neutral-soft);
  color: var(--text-2);
  flex: none;
}
.dl-status.running {
  background: var(--brand-soft);
  color: var(--brand);
}
.dl-status.done {
  background: var(--ok-soft);
  color: var(--ok-text);
}
.dl-status.failed {
  background: var(--danger-soft);
  color: var(--danger-text);
}
.dl-err {
  color: var(--danger-text);
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
