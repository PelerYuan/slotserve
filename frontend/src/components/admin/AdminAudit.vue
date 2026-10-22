<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDatePicker, NInput, NPagination, NSelect } from 'naive-ui'
import { Download, Undo2 } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AdminUser, AuditEntry } from '@/api/types'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime } from '@/utils/format'
import { settingsApi, type AuditQuery, type Setting, type SettingsPreview } from '@/features/settings/api'
import { useSettingText } from '@/features/settings/text'
import ChangePreview from '@/features/settings/components/ChangePreview.vue'

// Admin → Audit log (F07): filter by person, action (or a whole domain), target and time; CSV of the same filter;
// settings changes read "label: before → after" and can be undone through the preview.
const PAGE = 30
const { t, te } = useI18n()
const { showError, message } = useErrorMessage()
const { label: settingLabel, display } = useSettingText()

type Entry = AuditEntry & { ip?: string }
const items = ref<Entry[]>([])
const total = ref(0)
const page = ref(1)
const loaded = ref(false)
const users = ref<AdminUser[]>([])
const actions = ref<string[]>([])
const settings = ref<Record<string, Setting>>({})

const filters = reactive({
  actor: null as number | null,
  actions: [] as string[],
  target: '',
  range: null as [number, number] | null,
})

const query = computed<AuditQuery>(() => ({
  actor_id: filters.actor,
  action: filters.actions.join(',') || null,
  target: filters.target.trim() || null,
  since: filters.range ? new Date(filters.range[0]).toISOString() : null,
  // the end day counts in full
  until: filters.range ? new Date(filters.range[1] + 24 * 3600 * 1000).toISOString() : null,
}))

async function load() {
  try {
    const res = await settingsApi.audit({ ...query.value, limit: PAGE, offset: (page.value - 1) * PAGE })
    items.value = res.items
    total.value = res.total
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}

onMounted(async () => {
  load()
  const [u, a, s] = await Promise.all([
    api.admin.users().catch(() => [] as AdminUser[]),
    settingsApi.auditActions().catch(() => [] as string[]),
    settingsApi.list(true).catch(() => [] as Setting[]),
  ])
  users.value = u
  actions.value = a
  settings.value = Object.fromEntries(s.map((x) => [x.key, x]))
})
watch(page, load)
let timer = 0
watch(
  query,
  () => {
    window.clearTimeout(timer)
    timer = window.setTimeout(() => {
      if (page.value !== 1) page.value = 1
      else load()
    }, 250)
  },
  { deep: true },
)

const actionLabel = (a: string) => {
  const key = `admin.audit.actions.${a.replace('.', '_')}`
  return te(key) ? t(key) : a
}
const domainLabel = (d: string) =>
  te(`settingsUi.audit.domains.${d}`) ? t(`settingsUi.audit.domains.${d}`) : d

// Actions grouped by domain; each group offers "all of it" (domain.*) first.
const actionOptions = computed(() => {
  const byDomain = new Map<string, string[]>()
  for (const a of actions.value) {
    const d = a.split('.')[0]
    byDomain.set(d, [...(byDomain.get(d) ?? []), a])
  }
  return [...byDomain.entries()].map(([d, list]) => ({
    type: 'group' as const,
    key: d,
    label: domainLabel(d),
    children: [
      { label: t('settingsUi.audit.domain', { name: domainLabel(d) }), value: `${d}.*` },
      ...list.map((a) => ({ label: actionLabel(a), value: a })),
    ],
  }))
})
const userOptions = computed(() => users.value.map((u) => ({ label: u.username, value: u.id })))
const csvUrl = computed(() => settingsApi.auditCsvUrl(query.value))
const filtered = computed(() => !!(filters.actor || filters.actions.length || filters.target || filters.range))
function clear() {
  Object.assign(filters, { actor: null, actions: [], target: '', range: null })
}

// ---- details --------------------------------------------------------------------------------------------------------
const SETTINGS_ACTIONS = new Set(['settings.update', 'settings.sensitive_update', 'settings.undo'])
// An empty GPU list means every visible GPU; say so instead of showing "gpus=".
const noGpus = (k: string, v: unknown) => k === 'gpus' && (v === '' || v === null || (Array.isArray(v) && !v.length))

function settingValue(key: string, v: unknown, reset: boolean): string {
  if (reset) return t('settingsUi.audit.reset')
  const s = settings.value[key]
  if (!s) return typeof v === 'string' ? v : JSON.stringify(v)
  return display(s, v as never)
}

interface Line {
  text: string
}
function details(e: AuditEntry): Line[] {
  const d = e.detail
  if (SETTINGS_ACTIONS.has(e.action) && d.before && typeof d.before === 'object') {
    const before = d.before as Record<string, unknown>
    const after = (d.after ?? {}) as Record<string, unknown>
    const reset = new Set((d.reset as string[] | undefined) ?? [])
    return Object.keys(before).map((k) => ({
      text: `${settingLabel({ key: k })}: ${settingValue(k, before[k], false)} → ${settingValue(k, after[k], reset.has(k))}`,
    }))
  }
  const text = Object.entries(d)
    .map(([k, v]) => `${k}=${noGpus(k, v) ? t('admin.audit.allGpus') : typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join('  ')
  return text ? [{ text }] : []
}
const undoable = (e: AuditEntry) => e.action === 'settings.update' || e.action === 'settings.sensitive_update'

// ---- undo -----------------------------------------------------------------------------------------------------------
const preview = reactive({ open: false, data: null as SettingsPreview | null, id: 0, busy: false })
async function startUndo(e: AuditEntry) {
  try {
    preview.data = await settingsApi.undoPreview(e.id)
    preview.id = e.id
    preview.open = true
  } catch (err) {
    showError(err)
  }
}
async function confirmUndo(notify: boolean) {
  preview.busy = true
  try {
    await settingsApi.undo(preview.id, notify)
    preview.open = false
    message.success(t('settingsUi.undone'))
    await load()
  } catch (err) {
    showError(err)
  } finally {
    preview.busy = false
  }
}
</script>

<template>
  <div class="filters" data-testid="audit-filters">
    <NSelect
      v-model:value="filters.actor"
      :options="userOptions"
      :placeholder="t('settingsUi.audit.whoPh')"
      clearable
      filterable
      class="f-who"
    />
    <NSelect
      v-model:value="filters.actions"
      :options="actionOptions"
      :placeholder="t('settingsUi.audit.actionPh')"
      multiple
      clearable
      filterable
      max-tag-count="responsive"
      class="f-action"
    />
    <NInput v-model:value="filters.target" :placeholder="t('settingsUi.audit.targetPh')" clearable class="f-target" />
    <NDatePicker
      v-model:value="filters.range"
      type="daterange"
      clearable
      :start-placeholder="t('settingsUi.audit.rangeStart')"
      :end-placeholder="t('settingsUi.audit.rangeEnd')"
      :placeholder="t('settingsUi.audit.range')"
      class="f-range"
    />
    <span class="spacer" />
    <NButton v-if="filtered" quaternary size="small" @click="clear">{{ t('settingsUi.audit.clear') }}</NButton>
    <NButton secondary size="small" tag="a" :href="csvUrl" download data-testid="audit-csv">
      <template #icon><Download :size="14" /></template>
      {{ t('settingsUi.audit.csv') }}
    </NButton>
  </div>

  <section class="card">
    <div v-if="loaded" class="count faint">{{ t('settingsUi.audit.total', { n: total }) }}</div>
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
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in items" :key="e.id" :data-audit="e.action">
            <td class="muted num nowrap">
              {{ formatDateTime(e.created_at) }}
              <div v-if="e.ip" class="faint small">{{ t('settingsUi.audit.ip', { ip: e.ip }) }}</div>
            </td>
            <td class="strong">{{ e.actor_name || t('admin.audit.system') }}</td>
            <td>
              <span class="badge" :class="{ danger: /delete|disable|revoke|sensitive|reject|failed/.test(e.action) }">{{ actionLabel(e.action) }}</span>
            </td>
            <td class="mono small target">{{ e.target || '—' }}</td>
            <td class="small detail">
              <div v-for="(l, i) in details(e)" :key="i" class="detail-line" :title="l.text">{{ l.text }}</div>
              <span v-if="!details(e).length" class="faint">—</span>
            </td>
            <td class="right">
              <NButton v-if="undoable(e)" size="tiny" quaternary @click="startUndo(e)">
                <template #icon><Undo2 :size="13" /></template>
                {{ t('settingsUi.undo') }}
              </NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="total > PAGE" class="pager">
      <NPagination v-model:page="page" :page-size="PAGE" :item-count="total" />
    </div>
  </section>

  <ChangePreview
    v-model:show="preview.open"
    :preview="preview.data"
    :settings="settings"
    undo
    :busy="preview.busy"
    @confirm="confirmUndo"
  />
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.f-who {
  width: 160px;
}
.f-action {
  width: 260px;
}
.f-target {
  width: 180px;
}
.f-range {
  width: 260px;
}
.count {
  padding: 10px 16px 0;
  font-size: 12px;
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
.target {
  max-width: 200px;
  overflow-wrap: anywhere;
}
.detail {
  max-width: 420px;
  color: var(--text-2);
}
.detail-line {
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
@media (max-width: 760px) {
  .f-who,
  .f-action,
  .f-target,
  .f-range {
    width: 100%;
  }
}
</style>
