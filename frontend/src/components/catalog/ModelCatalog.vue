<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NSelect, NSwitch, useDialog } from 'naive-ui'
import { Check, Download, Eye, EyeOff, Info, Plus, Search, X } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { Catalog, CatalogDeploy, CatalogEntry, CatalogTask } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes } from '@/utils/format'
import { bestFor, contextLabel, fitClass, installed, paramsLabel } from '@/utils/catalog'
import CatalogDetails from './CatalogDetails.vue'
import CatalogDeployDialog from './CatalogDeployDialog.vue'
import CatalogEntryForm from './CatalogEntryForm.vue'

const emit = defineEmits<{ changed: [] }>()
const { t, locale } = useI18n()
const dialog = useDialog()
const { showError, message } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

const data = ref<Catalog | null>(null)
const loading = ref(true)
const deploys = ref<CatalogDeploy[]>([])

async function load() {
  try {
    data.value = await api.catalog.list()
  } catch (e) {
    showError(e)
  } finally {
    loading.value = false
  }
}

let timer: number | undefined
let initialized = false
let stopped = false
async function pollDeploys() {
  window.clearTimeout(timer)
  const doneBefore = new Set(deploys.value.filter((d) => d.status === 'done').map((d) => d.id))
  deploys.value = await api.catalog.deploys().catch(() => deploys.value)
  // A deployment finished since the last look: the model list and the "installed" badges changed.
  const finished = initialized && deploys.value.some((d) => d.status === 'done' && !doneBefore.has(d.id))
  initialized = true
  if (finished) {
    await load()
    emit('changed')
  }
  if (stopped) return
  const busy = deploys.value.some((d) => d.status === 'downloading')
  timer = window.setTimeout(pollDeploys, busy ? 2000 : 10000)
}

onMounted(async () => {
  await Promise.all([load(), pollDeploys()])
})
onBeforeUnmount(() => {
  stopped = true
  window.clearTimeout(timer)
})

// ---- filters ----

const TASKS: CatalogTask[] = ['chat', 'code', 'reasoning', 'embedding', 'vision']
const SIZES = { any: [0, Infinity], small: [0, 4.5], medium: [4.5, 15], large: [15, 40], xl: [40, Infinity] } as const
type SizeKey = keyof typeof SIZES
const filters = reactive({
  q: '',
  task: 'all' as CatalogTask | 'all',
  size: 'any' as SizeKey,
  engine: 'any' as 'any' | 'llama.cpp' | 'vllm',
  lane: null as number | null,
  hidden: false,
})

const lanes = computed(() => data.value?.lanes ?? [])
const laneOptions = computed(() =>
  lanes.value.map((l) => ({ label: t('catalog.filter.fitsOn', { lane: l.name }), value: l.id })),
)
const sizeOptions = computed(() =>
  (Object.keys(SIZES) as SizeKey[]).map((k) => ({ label: t(`catalog.size.${k}`), value: k })),
)
const engineOptions = computed(() => [
  { label: t('catalog.filter.anyEngine'), value: 'any' },
  { label: 'llama.cpp (GGUF)', value: 'llama.cpp' },
  { label: 'vLLM / SGLang', value: 'vllm' },
])

const describe = (e: CatalogEntry) => (locale.value === 'zh-CN' && e.description.zh) || e.description.en

const entries = computed(() => {
  const q = filters.q.trim().toLowerCase()
  const [lo, hi] = SIZES[filters.size]
  return (data.value?.entries ?? []).filter((e) => {
    if (e.hidden && !filters.hidden) return false
    if (filters.task !== 'all' && !e.tasks.includes(filters.task)) return false
    if (e.params_b < lo || e.params_b >= hi) return false
    if (filters.engine === 'llama.cpp' && !e.variants.some((v) => v.format === 'gguf')) return false
    if (filters.engine === 'vllm' && !e.variants.some((v) => v.format === 'safetensors')) return false
    if (filters.lane != null && bestFor(e, filters.lane).status !== 'fits') return false
    if (!q) return true
    const hay = [e.name, e.family, e.publisher, e.license, ...e.tags, ...e.tasks, describe(e), ...e.variants.map((v) => v.quant)]
    return hay.join(' ').toLowerCase().includes(q)
  })
})
const hiddenCount = computed(() => (data.value?.entries ?? []).filter((e) => e.hidden).length)

const smallest = (e: CatalogEntry) => Math.min(...e.variants.map((v) => v.download_bytes))
const quants = (e: CatalogEntry) => [...new Set(e.variants.map((v) => v.quant))]

const activeDeploy = (e: CatalogEntry) => deploys.value.find((d) => d.entry_id === e.id && d.status === 'downloading')
const deployPct = (d: CatalogDeploy) =>
  d.total_bytes ? Math.min(100, Math.round((d.downloaded_bytes / d.total_bytes) * 100)) : 0

// ---- details, deploy, admin ----

const detailsId = ref<string | null>(null)
const details = computed(() => data.value?.entries.find((e) => e.id === detailsId.value) ?? null)
const deployTarget = ref<{ entry: CatalogEntry; variant: string | null } | null>(null)
const formShow = ref(false)
const editing = ref<CatalogEntry | null>(null)

function openDeploy(entry: CatalogEntry, variant: string | null = null) {
  deployTarget.value = { entry, variant }
}

async function onDeployed(d: CatalogDeploy) {
  deployTarget.value = null
  if (d.status === 'done') {
    message.success(t('catalog.deploy.ready', { name: d.model_name }))
    await load()
  } else {
    message.success(t('catalog.deploy.started', { n: d.files }))
  }
  emit('changed')
  await pollDeploys()
}

async function setHidden(e: CatalogEntry, hidden: boolean) {
  try {
    await api.catalog.setHidden(e.id, hidden)
    await load()
  } catch (err) {
    showError(err)
  }
}

function openForm(e: CatalogEntry | null) {
  editing.value = e
  formShow.value = true
}

function removeEntry(e: CatalogEntry) {
  dialog.warning({
    title: t('catalog.admin.deleteTitle'),
    content: t('catalog.admin.deleteConfirm', { name: e.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.catalog.deleteEntry(e.id)
        detailsId.value = null
        await load()
      } catch (err) {
        showError(err)
      }
    },
  })
}

async function dismiss(d: CatalogDeploy) {
  try {
    await api.catalog.dismiss(d.id)
    await pollDeploys()
    emit('changed')
  } catch (e) {
    showError(e)
  }
}

const recentDeploys = computed(() => deploys.value.filter((d) => d.status !== 'done').slice(0, 5))
const entryName = (id: string) => data.value?.entries.find((e) => e.id === id)?.name ?? id
</script>

<template>
  <div class="catalog">
    <div class="toolbar">
      <NInput v-model:value="filters.q" :placeholder="t('catalog.search')" clearable class="search">
        <template #prefix><Search :size="15" /></template>
      </NInput>
      <NSelect v-model:value="filters.size" :options="sizeOptions" :placeholder="t('ph.sizeAny')" class="sel" />
      <NSelect v-model:value="filters.engine" :options="engineOptions" :placeholder="t('catalog.filter.anyEngine')" class="sel" />
      <NSelect
        v-model:value="filters.lane"
        :options="laneOptions"
        :placeholder="t('catalog.filter.anyLane')"
        clearable
        class="sel lane-sel"
      />
      <template v-if="isAdmin">
        <label v-if="hiddenCount" class="hidden-toggle small">
          <NSwitch v-model:value="filters.hidden" size="small" />
          {{ t('catalog.admin.showHidden', { n: hiddenCount }) }}
        </label>
        <NButton secondary @click="openForm(null)">
          <template #icon><Plus :size="16" /></template>
          {{ t('catalog.admin.add') }}
        </NButton>
      </template>
    </div>
    <div class="tasks" role="tablist">
      <button
        v-for="task in ['all', ...TASKS]"
        :key="task"
        type="button"
        role="tab"
        class="task-pill"
        :class="{ on: filters.task === task }"
        :aria-selected="filters.task === task"
        @click="filters.task = task as CatalogTask | 'all'"
      >
        {{ t(`catalog.task.${task}`) }}
      </button>
    </div>

    <div v-if="recentDeploys.length" class="deploys">
      <div v-for="d in recentDeploys" :key="d.id" class="deploy-row" :class="d.status">
        <span class="deploy-name">{{ entryName(d.entry_id) }} <span class="faint">· {{ d.variant_id }}</span></span>
        <span class="badge" :class="{ warn: d.status === 'stalled', danger: d.status === 'failed', brand: d.status === 'downloading' }">
          {{ t(`catalog.deployStatus.${d.status}`) }}
        </span>
        <span class="spacer" />
        <span v-if="d.status === 'downloading'" class="faint small num">
          {{ formatBytes(d.downloaded_bytes) }} / {{ formatBytes(d.total_bytes) }} · {{ deployPct(d) }}%
        </span>
        <span v-else-if="d.error" class="err small" :title="d.error">{{ d.error }}</span>
        <NButton quaternary circle size="tiny" :title="d.status === 'downloading' ? t('common.cancel') : t('common.remove')" @click="dismiss(d)">
          <X :size="14" />
        </NButton>
      </div>
      <p class="faint small hint">{{ t('catalog.deploysHint') }}</p>
    </div>

    <div v-if="loading" class="empty">{{ t('catalog.loading') }}</div>
    <div v-else-if="!entries.length" class="empty">{{ t('catalog.empty') }}</div>
    <div v-else class="grid">
      <article v-for="e in entries" :key="e.id" class="cat-card" :class="{ dim: e.hidden }">
        <header class="cat-head">
          <div class="cat-title">
            <h3>{{ e.name }}</h3>
            <span v-if="installed(e)" class="badge ok"><Check :size="12" />{{ t('catalog.installed') }}</span>
            <span v-if="e.hidden" class="badge">{{ t('catalog.admin.hidden') }}</span>
            <span v-if="e.source === 'custom'" class="badge brand">{{ t('catalog.admin.custom') }}</span>
          </div>
          <div class="faint small">{{ e.publisher }}<template v-if="e.license"> · {{ e.license }}</template></div>
        </header>
        <div class="cat-tasks">
          <span v-for="task in e.tasks" :key="task" class="chip">{{ t(`catalog.task.${task}`) }}</span>
        </div>
        <p class="cat-desc muted">{{ describe(e) }}</p>
        <dl class="cat-specs">
          <div><dt>{{ t('catalog.params') }}</dt><dd class="num">{{ paramsLabel(e) }}</dd></div>
          <div><dt>{{ t('catalog.context') }}</dt><dd class="num">{{ contextLabel(e.context_length) }}</dd></div>
          <div><dt>{{ t('catalog.download') }}</dt><dd class="num">{{ t('catalog.from', { size: formatBytes(smallest(e)) }) }}</dd></div>
        </dl>
        <div class="quants">
          <span v-for="q in quants(e)" :key="q" class="quant mono">{{ q }}</span>
        </div>
        <div class="fits">
          <span
            v-for="lane in lanes"
            :key="lane.id"
            class="badge"
            :class="fitClass(bestFor(e, lane.id).status)"
            :title="t(`catalog.fitHint.${bestFor(e, lane.id).status}`)"
          >
            {{ lane.name }}:
            <template v-if="bestFor(e, lane.id).status === 'unknown'">{{ t('catalog.fit.unknown') }}</template>
            <template v-else>{{ t(`catalog.fit.${bestFor(e, lane.id).status}`) }} · {{ bestFor(e, lane.id).variant?.quant }}</template>
          </span>
        </div>
        <div v-if="activeDeploy(e)" class="progress">
          <div class="bar"><div :style="{ width: deployPct(activeDeploy(e)!) + '%' }" /></div>
          <span class="faint small num">{{ deployPct(activeDeploy(e)!) }}%</span>
        </div>
        <footer class="cat-foot">
          <NButton quaternary size="small" @click="detailsId = e.id">
            <template #icon><Info :size="15" /></template>
            {{ t('catalog.details') }}
          </NButton>
          <NButton v-if="isAdmin && e.hidden" quaternary size="small" @click="setHidden(e, false)">
            <template #icon><Eye :size="15" /></template>
            {{ t('catalog.admin.unhide') }}
          </NButton>
          <span class="spacer" />
          <NButton type="primary" size="small" secondary @click="openDeploy(e)">
            <template #icon><Download :size="15" /></template>
            {{ t('catalog.deploy.button') }}
          </NButton>
        </footer>
      </article>
    </div>
    <p v-if="data?.updated" class="faint small updated">
      {{ t('catalog.updated', { date: data.updated }) }}
      <template v-if="isAdmin && hiddenCount && !filters.hidden"> · <EyeOff :size="12" /> {{ t('catalog.admin.hiddenCount', { n: hiddenCount }) }}</template>
    </p>

    <CatalogDetails
      :entry="details"
      :lanes="lanes"
      @close="detailsId = null"
      @deploy="(v: string) => details && openDeploy(details, v)"
      @hide="(h: boolean) => details && setHidden(details, h)"
      @edit="() => details && openForm(details)"
      @remove="() => details && removeEntry(details)"
    />
    <CatalogDeployDialog
      v-if="deployTarget"
      :entry="deployTarget.entry"
      :variant="deployTarget.variant"
      :lanes="lanes"
      @close="deployTarget = null"
      @deployed="onDeployed"
    />
    <CatalogEntryForm v-model:show="formShow" :entry="editing" @saved="load" />
  </div>
</template>

<style scoped>
.small {
  font-size: 12.5px;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.search {
  flex: 1 1 240px;
}
.sel {
  width: 170px;
}
.lane-sel {
  width: 200px;
}
.hidden-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
}
.tasks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 12px 0 16px;
}
.task-pill {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-2);
  border-radius: 999px;
  padding: 4px 12px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.task-pill:hover {
  background: var(--hover);
}
.task-pill.on {
  background: var(--brand-soft);
  border-color: transparent;
  color: var(--brand);
  font-weight: 600;
}
.deploys {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  margin-bottom: 16px;
  overflow: hidden;
}
.deploy-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--border);
  min-width: 0;
}
.deploy-name {
  font-weight: 600;
  font-size: 13.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.err {
  color: var(--danger-text);
  max-width: 50%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hint {
  margin: 0;
  padding: 8px 14px;
  background: var(--surface-muted);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.cat-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 16px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
  min-width: 0;
}
.cat-card.dim {
  opacity: 0.6;
}
.cat-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.cat-title h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.cat-tasks,
.quants,
.fits {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.chip {
  font-size: 11.5px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--neutral-soft);
  color: var(--text-2);
}
.cat-desc {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.cat-specs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin: 0;
}
.cat-specs dt {
  font-size: 11.5px;
  color: var(--text-3, var(--text-2));
}
.cat-specs dd {
  margin: 1px 0 0;
  font-weight: 600;
  font-size: 13px;
}
.quant {
  font-size: 11px;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text-2);
}
.fits .badge {
  height: 20px;
  font-size: 11.5px;
}
.progress {
  display: flex;
  align-items: center;
  gap: 8px;
}
.bar {
  flex: 1;
  height: 5px;
  border-radius: 3px;
  background: var(--rail);
  overflow: hidden;
}
.bar > div {
  height: 100%;
  background: var(--brand);
  transition: width 0.4s;
}
.cat-foot {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.updated {
  margin-top: 14px;
  display: flex;
  align-items: center;
  gap: 4px;
}
@media (max-width: 760px) {
  .sel,
  .lane-sel {
    width: calc(50% - 4px);
    flex: 1 1 calc(50% - 4px);
  }
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
