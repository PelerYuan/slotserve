<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NRadioButton, NRadioGroup, NSwitch } from 'naive-ui'
import { ChevronDown, ChevronUp, Link2, MessageCircle, Search } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AdminChannels, SettingValue } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { settingsApi, type Setting, type SettingsPreview } from '../api'
import { dependencyMet, useSettingText } from '../text'
import SettingRow from './SettingRow.vue'
import ChangePreview from './ChangePreview.vue'
import SettingHistory from './SettingHistory.vue'

// Admin → System settings (F07), built from the settings registry: policy cards of the basic settings by default,
// every setting by group (expert ones folded) on request, search over labels in both languages, variable names
// and keywords, "changed only", saving per card / group with an impact preview first, history and undo per
// setting, and the read-only deployment parameters.
const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { showError, message } = useErrorMessage()
const { label, groupLabel, cardLabel, display, haystack } = useSettingText()

const all = ref<Setting[]>([])
const loaded = ref(false)
const draft = reactive<Record<string, SettingValue | null>>({})
const editable = computed(() => all.value.filter((s) => !s.restart_required))
const deployment = computed(() => all.value.filter((s) => s.restart_required))
const byKey = computed<Record<string, Setting>>(() => Object.fromEntries(all.value.map((s) => [s.key, s])))

const VIEW_STORE = 'slotserve.settings.view'
function storedView(): 'cards' | 'all' {
  try {
    return localStorage.getItem(VIEW_STORE) === 'all' ? 'all' : 'cards'
  } catch {
    return 'cards'
  }
}
const view = ref<'cards' | 'all'>(storedView())
watch(view, (v) => {
  try {
    localStorage.setItem(VIEW_STORE, v)
  } catch {
    /* storage unavailable */
  }
})
const query = ref(typeof route.query.q === 'string' ? route.query.q : '')
const expert = ref(false)
const changedOnly = ref(false)
const showDeployment = ref(false)
const expanded = ref<Set<string>>(new Set())
const highlight = ref<string | null>(null)

function adopt(list: Setting[], keys?: Set<string>) {
  const fresh = new Map(list.map((s) => [s.key, s]))
  all.value = all.value.length ? all.value.map((s) => fresh.get(s.key) ?? s) : list
  for (const s of list) if (!keys || keys.has(s.key) || !(s.key in draft)) draft[s.key] = s.secret ? '' : s.value
}

async function load() {
  try {
    all.value = []
    adopt(await settingsApi.list(true))
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}

const channels = ref<AdminChannels | null>(null)
onMounted(async () => {
  await load()
  focusRequested()
  channels.value = await api.admin.channels().catch(() => null)
})

// Admin → Overview links a setting as ?q=<key>: show it and draw attention to it.
function focusRequested() {
  const key = typeof route.query.q === 'string' ? route.query.q : ''
  if (!byKey.value[key]) return
  highlight.value = key
  nextTick(() => document.getElementById(`setting-${key}`)?.scrollIntoView({ block: 'center' }))
  window.setTimeout(() => (highlight.value = null), 2500)
}
watch(
  () => route.query.q,
  (q) => {
    if (typeof q === 'string' && q !== query.value) {
      query.value = q
      focusRequested()
    }
  },
)

// ---- what is shown ------------------------------------------------------------------------------------------------
const valueOf = (key: string) => {
  const s = byKey.value[key]
  if (s?.secret) return draft[key] || (s.has_value ? 'set' : '')
  return key in draft ? draft[key] : s?.value
}
// A setting applies when what it depends on is on, all the way up ("Direct ports" → "Dev environments").
function applies(s: Setting, depth = 0): boolean {
  if (!dependencyMet(s.depends_on, valueOf)) return false
  const parent = s.depends_on ? byKey.value[s.depends_on.split('=')[0]] : undefined
  return !parent || depth > 5 || applies(parent, depth + 1)
}
const parentLabel = (s: Setting) => {
  const parent = s.depends_on ? byKey.value[s.depends_on.split('=')[0]] : undefined
  return parent ? label(parent) : ''
}
const isDirty = (key: string) => {
  const s = byKey.value[key]
  const d = draft[key]
  if (!s || s.restart_required || d === null || d === undefined) return false
  return s.secret ? d !== '' : d !== s.value
}
const terms = computed(() => query.value.trim().toLowerCase().split(/\s+/).filter(Boolean))
const matches = (s: Setting) => {
  if (terms.value.length) {
    const hay = haystack(s)
    if (!terms.value.every((term) => hay.includes(term))) return false
  }
  if (changedOnly.value && !(s.overridden || isDirty(s.key))) return false
  return true
}
const filtering = computed(() => terms.value.length > 0 || changedOnly.value)
const mode = computed(() => (filtering.value ? 'filter' : view.value))

// Policy cards: the basic settings by their card (basic settings without a card get one for their group); "More"
// opens the advanced settings of the same group in the card.
const CARD_GROUP: Record<string, string> = {
  join: 'access',
  fairness: 'booking',
  notify: 'notify',
  devenv: 'devenv',
  models: 'models',
}
const cards = computed(() => {
  const order: string[] = []
  const items: Record<string, Setting[]> = {}
  for (const s of editable.value) {
    if (s.tier !== 'basic') continue
    const id = s.card ?? s.group
    if (!items[id]) {
      items[id] = []
      order.push(id)
    }
    items[id].push(s)
  }
  return order.map((id) => {
    const group = CARD_GROUP[id] ?? id
    const more = editable.value.filter(
      (s) => s.group === group && s.tier !== 'basic' && (s.tier !== 'expert' || expert.value),
    )
    const open = expanded.value.has(id)
    return {
      id,
      group,
      keys: [...items[id], ...more].map((s) => s.key),
      rows: [...items[id], ...(open ? more : [])].filter((s) => applies(s)),
      more: more.filter((s) => applies(s)).length,
      open,
    }
  })
})
function toggleCard(id: string) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

// All settings by group, in registry order.
const groups = computed(() => {
  const order: string[] = []
  const items: Record<string, Setting[]> = {}
  for (const s of editable.value) {
    if (!items[s.group]) {
      items[s.group] = []
      order.push(s.group)
    }
    items[s.group].push(s)
  }
  return order
    .map((id) => {
      // search and "changed only" also find settings that are not in use now (marked as such)
      const list = items[id].filter((s) => (filtering.value || applies(s)) && matches(s))
      const showExpert = expert.value || filtering.value
      return {
        id,
        keys: items[id].map((s) => s.key),
        rows: list.filter((s) => s.tier !== 'expert' || showExpert),
        hiddenExpert: showExpert ? 0 : list.filter((s) => s.tier === 'expert').length,
      }
    })
    .filter((g) => g.rows.length || g.hiddenExpert)
})
const deploymentRows = computed(() => (filtering.value ? deployment.value.filter(matches) : deployment.value))
const resultCount = computed(
  () => groups.value.reduce((n, g) => n + g.rows.length, 0) + (terms.value.length ? deploymentRows.value.length : 0),
)

const dirtyIn = (keys: string[]) => keys.filter(isDirty)
const totalCount = computed(() => editable.value.length)

const platformLine = computed(() => {
  const list = channels.value?.channels.filter((c) => c.switched_on || c.configured) ?? []
  const connected = list.filter((c) => c.connected).length
  if (!connected) return t('settingsUi.platformsNone')
  return `${t('settingsUi.platformsSome', { connected, total: list.length })} · ${list
    .filter((c) => c.connected)
    .map((c) => c.label)
    .join(', ')}`
})

function discard(keys: string[]) {
  for (const k of keys) {
    const s = byKey.value[k]
    if (s) draft[k] = s.secret ? '' : s.value
  }
}

// ---- saving: preview first ----------------------------------------------------------------------------------------
const preview = reactive({
  open: false,
  data: null as SettingsPreview | null,
  changes: {} as Record<string, SettingValue | null>,
  undoId: null as number | null,
  busy: false,
})
const busySection = ref<string | null>(null)

async function startSave(section: string, changes: Record<string, SettingValue | null>) {
  if (!Object.keys(changes).length) return
  busySection.value = section
  try {
    const data = await settingsApi.preview(changes)
    if (!data.changes.length) {
      message.info(t('settingsUi.nothingChanged'))
      discard(Object.keys(changes))
      return
    }
    Object.assign(preview, { open: true, data, changes, undoId: null })
  } catch (e) {
    showError(e)
  } finally {
    busySection.value = null
  }
}

const saveKeys = (section: string, keys: string[]) =>
  startSave(section, Object.fromEntries(dirtyIn(keys).map((k) => [k, draft[k]])))
const resetKey = (key: string) => startSave(key, { [key]: null })

async function confirm(notify: boolean) {
  preview.busy = true
  try {
    const list =
      preview.undoId !== null
        ? await settingsApi.undo(preview.undoId, notify)
        : await settingsApi.save(preview.changes, notify)
    const keys = new Set((preview.data?.changes ?? []).map((c) => c.key))
    for (const k of Object.keys(preview.changes)) keys.add(k)
    adopt(list, keys)
    preview.open = false
    message.success(preview.undoId !== null ? t('settingsUi.undone') : t('settingsUi.saved'))
    if (preview.undoId !== null) history.open = false
    session.config = await api.config().catch(() => session.config)
  } catch (e) {
    showError(e)
  } finally {
    preview.busy = false
  }
}

// ---- history and undo ---------------------------------------------------------------------------------------------
const history = reactive({ open: false, setting: null as Setting | null })
const openHistory = (s: Setting) => Object.assign(history, { open: true, setting: s })

async function startUndo(auditId: number) {
  try {
    const data = await settingsApi.undoPreview(auditId)
    Object.assign(preview, { open: true, data, changes: {}, undoId: auditId })
  } catch (e) {
    showError(e)
  }
}

const scrollTo = (id: string) => document.getElementById(`group-${id}`)?.scrollIntoView({ behavior: 'smooth' })
const clearQuery = () => {
  query.value = ''
  if (route.query.q) router.replace({ query: { ...route.query, q: undefined } })
}
</script>

<template>
  <div class="settings-page" data-testid="settings-page">
    <div class="toolbar">
      <NInput v-model:value="query" clearable :placeholder="t('settingsUi.searchPh')" class="search" data-testid="settings-search" @clear="clearQuery">
        <template #prefix><Search :size="15" class="faint" /></template>
      </NInput>
      <NRadioGroup v-model:value="view" size="small" :disabled="filtering">
        <NRadioButton value="cards" data-view="cards">{{ t('settingsUi.views.cards') }}</NRadioButton>
        <NRadioButton value="all" data-view="all">{{ t('settingsUi.views.all') }}</NRadioButton>
      </NRadioGroup>
      <label class="toggle"><NSwitch v-model:value="changedOnly" size="small" data-testid="changed-only" />{{ t('settingsUi.changedOnly') }}</label>
      <label v-if="mode !== 'cards'" class="toggle"><NSwitch v-model:value="expert" size="small" />{{ t('settingsUi.showExpert') }}</label>
    </div>

    <!-- policy cards ------------------------------------------------------------------------------------------- -->
    <template v-if="mode === 'cards'">
      <div class="cards">
        <section v-for="c in cards" :key="c.id" class="card policy" :class="{ wide: c.open }" :data-card="c.id">
          <div class="policy-head">
            <h3 class="card-title">{{ cardLabel(c.id) }}</h3>
            <NButton v-if="c.more" size="tiny" quaternary @click="toggleCard(c.id)">
              {{ c.open ? t('settingsUi.less') : `${t('settingsUi.more')} (${c.more})` }}
              <template #icon><ChevronUp v-if="c.open" :size="13" /><ChevronDown v-else :size="13" /></template>
            </NButton>
          </div>
          <div class="policy-rows" :class="{ columns: c.open }">
            <SettingRow
              v-for="s in c.rows"
              :key="s.key"
              :setting="s"
              :value="draft[s.key] ?? null"
              :dirty="isDirty(s.key)"
              :highlight="highlight === s.key"
              compact
              @update:value="(v) => (draft[s.key] = v)"
              @reset="resetKey(s.key)"
              @history="openHistory(s)"
            />
            <div v-if="c.id === 'join'" class="platforms" data-testid="invite-links">
              <div class="platform-title"><Link2 :size="14" />{{ t('settingsUi.inviteLinks') }}</div>
              <div class="platform-row">
                <span class="muted platform-text">{{ t('settingsUi.inviteLinksHint') }}</span>
                <NButton size="tiny" secondary @click="router.push({ query: { tab: 'users' } })">{{ t('settingsUi.manage') }}</NButton>
              </div>
            </div>
            <div v-if="c.id === 'notify'" class="platforms" data-testid="chat-platforms">
              <div class="platform-title"><MessageCircle :size="14" />{{ t('settingsUi.platforms') }}</div>
              <div class="platform-row">
                <span class="muted platform-text">{{ platformLine }}</span>
                <NButton size="tiny" secondary @click="router.push({ query: { tab: 'channels' } })">{{ t('settingsUi.connect') }}</NButton>
              </div>
            </div>
          </div>
          <div class="policy-foot">
            <span v-if="dirtyIn(c.keys).length" class="faint small">{{ t('settingsUi.unsaved', { n: dirtyIn(c.keys).length }) }}</span>
            <span class="spacer" />
            <NButton v-if="dirtyIn(c.keys).length" size="small" quaternary @click="discard(c.keys)">{{ t('settingsUi.discard') }}</NButton>
            <NButton
              size="small"
              type="primary"
              :disabled="!dirtyIn(c.keys).length"
              :loading="busySection === c.id"
              :data-save="c.id"
              @click="saveKeys(c.id, c.keys)"
            >
              {{ t('settingsUi.save') }}
            </NButton>
          </div>
        </section>
      </div>
      <div class="below-cards">
        <NButton secondary @click="view = 'all'">{{ t('settingsUi.showAll', { n: totalCount }) }}</NButton>
      </div>
    </template>

    <!-- every setting by group, or the search / "changed only" results ------------------------------------------ -->
    <div v-else class="all-layout">
      <nav v-if="!filtering" class="toc">
        <div class="label">{{ t('settingsUi.toc') }}</div>
        <a v-for="g in groups" :key="g.id" href="#" @click.prevent="scrollTo(g.id)">{{ groupLabel(g.id) }}</a>
        <a href="#" @click.prevent="showDeployment = true; $nextTick(() => scrollTo('deployment'))">{{ groupLabel('deployment') }}</a>
      </nav>
      <div class="all-main">
        <p v-if="filtering && loaded" class="muted result-line">
          <template v-if="resultCount">{{ t('settingsUi.results', { n: resultCount }) }}</template>
          <template v-else-if="terms.length">{{ t('settingsUi.noResults', { q: query.trim() }) }}</template>
          <template v-else>{{ t('settingsUi.noChanged') }}</template>
        </p>
        <section v-for="g in groups" :id="`group-${g.id}`" :key="g.id" class="card group" :data-group="g.id">
          <div class="group-title">
            <span>{{ groupLabel(g.id) }}</span>
          </div>
          <SettingRow
            v-for="s in g.rows"
            :key="s.key"
            :setting="s"
            :value="draft[s.key] ?? null"
            :dirty="isDirty(s.key)"
            :highlight="highlight === s.key"
            :inactive="applies(s) ? '' : parentLabel(s)"
            @update:value="(v) => (draft[s.key] = v)"
            @reset="resetKey(s.key)"
            @history="openHistory(s)"
          />
          <button v-if="g.hiddenExpert" class="expert-more" @click="expert = true">
            {{ t('settingsUi.expertHidden', { n: g.hiddenExpert }) }} ▸
          </button>
          <div class="group-foot">
            <span v-if="dirtyIn(g.keys).length" class="faint small">{{ t('settingsUi.unsaved', { n: dirtyIn(g.keys).length }) }}</span>
            <span class="spacer" />
            <NButton v-if="dirtyIn(g.keys).length" size="small" quaternary @click="discard(g.keys)">{{ t('settingsUi.discard') }}</NButton>
            <NButton
              size="small"
              type="primary"
              :disabled="!dirtyIn(g.keys).length"
              :loading="busySection === g.id"
              :data-save="g.id"
              @click="saveKeys(g.id, g.keys)"
            >
              {{ t('settingsUi.save') }}
            </NButton>
          </div>
        </section>
        <section
          v-if="(filtering && terms.length && deploymentRows.length) || (!filtering && view === 'all')"
          id="group-deployment"
          class="card group deployment"
          data-group="deployment"
        >
          <div class="group-title">
            <span>{{ t('settingsUi.deployment.title') }}</span>
            <NButton v-if="!filtering" size="tiny" quaternary @click="showDeployment = !showDeployment">
              {{ showDeployment ? t('settingsUi.deployment.hide') : t('settingsUi.deployment.show', { n: deployment.length }) }}
            </NButton>
          </div>
          <template v-if="showDeployment || filtering">
            <p class="muted deploy-sub">{{ t('settingsUi.deployment.sub') }}</p>
            <div class="table-wrap">
              <table class="table">
                <tbody>
                  <tr v-for="s in deploymentRows" :key="s.key" :data-setting="s.key">
                    <td>
                      <div class="strong">
                        {{ label(s) }}
                        <span v-if="s.danger" class="badge danger">{{ t('settingsUi.badges.danger') }}</span>
                      </div>
                      <code class="env">{{ s.env_var }}</code>
                    </td>
                    <td class="mono small value">{{ s.secret ? (s.has_value ? '••••••' : t('settingsUi.secretUnset')) : display(s, s.value) }}</td>
                    <td class="faint small">{{ t(`settingsUi.sources.${s.source}`) }}</td>
                    <td class="right"><span class="badge">{{ t('settingsUi.badges.restart') }}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </section>
      </div>
    </div>

    <ChangePreview
      v-model:show="preview.open"
      :preview="preview.data"
      :settings="byKey"
      :undo="preview.undoId !== null"
      :busy="preview.busy"
      @confirm="confirm"
    />
    <SettingHistory v-model:show="history.open" :setting="history.setting" @undo="startUndo" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  margin-bottom: 16px;
}
.search {
  flex: 1 1 320px;
  max-width: 460px;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  align-items: start;
}
.policy {
  display: flex;
  flex-direction: column;
}
.policy.wide {
  grid-column: 1 / -1;
}
.policy-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px 6px;
}
.policy-rows :deep(.setting:last-child) {
  border-bottom: none;
}
.policy-rows.columns {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  column-gap: 16px;
}
.policy-rows.columns :deep(.setting:last-child) {
  border-bottom: 1px solid var(--border);
}
.platforms {
  padding: 9px 16px;
  font-size: 13px;
}
.platform-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  font-size: 13.5px;
}
.platform-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}
.platform-text {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
}
.policy-foot,
.group-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid var(--border);
  background: var(--surface-muted);
  border-radius: 0 0 var(--radius) var(--radius);
}
.small {
  font-size: 12px;
}
.below-cards {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}
.all-layout {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.all-layout:has(> .all-main:only-child) {
  grid-template-columns: minmax(0, 1fr);
}
.toc {
  position: sticky;
  top: 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 13px;
}
.toc .label {
  margin-bottom: 4px;
}
.toc a {
  padding: 4px 8px;
  border-radius: 6px;
  color: var(--text-2);
}
.toc a:hover {
  background: var(--hover);
  color: var(--text);
}
.result-line {
  margin: 0 0 12px;
}
.group {
  margin-bottom: 16px;
  scroll-margin-top: 16px;
}
.group-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
  background: var(--surface-muted);
  border-bottom: 1px solid var(--border);
  border-radius: var(--radius) var(--radius) 0 0;
}
.expert-more {
  display: block;
  width: 100%;
  padding: 10px 20px;
  border: none;
  border-bottom: 1px solid var(--border);
  background: none;
  color: var(--brand);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.deploy-sub {
  margin: 12px 20px 4px;
  font-size: 13px;
}
.strong {
  font-weight: 600;
}
.env {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-3);
}
.value {
  overflow-wrap: anywhere;
  max-width: 360px;
}
@media (max-width: 960px) {
  .all-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .toc {
    display: none;
  }
}
@media (max-width: 760px) {
  .cards,
  .policy-rows.columns {
    grid-template-columns: minmax(0, 1fr);
  }
  .search {
    max-width: none;
  }
}
</style>
