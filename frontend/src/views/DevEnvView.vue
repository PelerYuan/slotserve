<script setup lang="ts">
// Dev environments (F03/F04/F10/F11): with environments open, they come first and "Open a new environment" is
// folded; otherwise the page starts with the three-step start card. Then shared environments, kept ones,
// templates, direct ports, the CPU limits, and files and storage.
import { computed, h, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, useDialog } from 'naive-ui'
import { KeyRound, Plus, Terminal } from 'lucide-vue-next'
import CpuPanel from '@/components/devenv/CpuPanel.vue'
import CpuProjectControls from '@/components/devenv/CpuProjectControls.vue'
import CpuStartButton from '@/components/devenv/CpuStartButton.vue'
import PublicPorts from '@/components/devenv/PublicPorts.vue'
import QuickBook from '@/features/quickbook/components/QuickBook.vue'
import { api } from '@/api/client'
import type { DevInfo, DevProject, DevSession, DevTemplate, DevTemplateInput, DevWebKind, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { devenvApi, type DevSessionX } from '@/features/devenv/api'
import EnvCard from '@/features/devenv/components/EnvCard.vue'
import PreviewLine from '@/features/devenv/components/PreviewLine.vue'
import SharedEnvCard from '@/features/devenv/components/SharedEnvCard.vue'
import StartCard from '@/features/devenv/components/StartCard.vue'
import { environmentTitles, sshAliases } from '@/features/devenv/naming'
import StorageInfo from '@/features/devenv/StorageInfo.vue'
import { formatBytes, formatDateTime, formatRange, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

const info = ref<DevInfo | null>(null)
const sessions = ref<DevSessionX[]>([])
const templates = ref<(DevTemplate & { compat?: boolean })[]>([])
const hasKeys = ref(true)
const projects = ref<DevProject[]>([])
const bookings = ref<Reservation[]>([])
const everyone = ref(false)
const loaded = ref(false)

const sharedSessions = computed(() => sessions.value.filter((s) => s.kind === 'shared'))
const own = computed(() => sessions.value.filter((s) => s.kind !== 'shared'))
const open = computed(() => own.value.filter((s) => s.state !== 'stopped'))
const history = computed(() => own.value.filter((s) => s.state === 'stopped').slice(0, 8))
// A way in over SSH: a key, or the environment password while password logins are allowed. Only the SSH path
// needs it; web interfaces open without one (F03).
const canLogIn = computed(() => hasKeys.value || (!!info.value?.password_login && !!info.value?.password_set))
const prepared = computed(() => new Set(sessions.value.map((s) => s.reservation_id)))
const upcoming = computed(() =>
  bookings.value.filter(
    (r) =>
      (r.devenv_template_id || r.devenv_project_id) &&
      (r.phase === 'upcoming' || r.phase === 'pending' || r.phase === 'active') &&
      !prepared.value.has(r.id),
  ),
)
const builtin = computed(() => templates.value.filter((x) => x.kind === 'builtin'))
const ownTemplates = computed(() => templates.value.filter((x) => x.kind !== 'builtin'))

const titles = computed(() => environmentTitles([...open.value, ...sharedSessions.value], bookings.value))
const aliases = computed(() => sshAliases(titles.value))
const sshHost = computed(() => info.value?.ssh_host ?? window.location.hostname)
const bookingOf = (s: DevSession) => bookings.value.find((r) => r.id === s.reservation_id) ?? null
const projectOf = (s: DevSession) => projects.value.find((p) => p.id === s.project_id) ?? null

async function load() {
  try {
    const [i, s, tpl, k, p, mine] = await Promise.all([
      api.devenv.info(),
      devenvApi.sessions(everyone.value),
      api.devenv.templates(),
      api.sshKeys(),
      api.devenv.projects(everyone.value),
      api.reservations(new Date(Date.now() - 86_400_000), new Date(Date.now() + 60 * 86_400_000), true),
    ])
    info.value = i
    sessions.value = s
    templates.value = tpl
    hasKeys.value = k.length > 0
    projects.value = p
    bookings.value = mine
    loaded.value = true
  } catch (e) {
    showError(e)
  }
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(load, 4000)
})
onBeforeUnmount(() => window.clearInterval(timer))

// ---- opening: the start card, QuickBook for the GPU path ---------------------------------------------------------
const showStart = ref(false)
const startVisible = computed(() => !open.value.length || showStart.value)
async function started(s: DevSession) {
  showStart.value = false
  await load()
  await nextTick()
  document.querySelector(`[data-session="${s.id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
const quickBook = reactive({ show: false, templateId: null as number | null, projectId: null as number | null })
function book(target?: { templateId: number | null; projectId: number | null }) {
  Object.assign(quickBook, { show: true, templateId: target?.templateId ?? null, projectId: target?.projectId ?? null })
}

function cancelBooking(r: Reservation) {
  dialog.warning({
    title: t('devenvUi.upcoming.cancel'),
    content: t('devenvUi.upcoming.cancelConfirm', { range: formatRange(r.start_at, r.end_at) }),
    positiveText: t('devenvUi.upcoming.cancel'),
    negativeText: t('common.close'),
    onPositiveClick: async () => {
      try {
        await devenvApi.end(r.id)
        load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

function describeTemplate(x: DevTemplate) {
  return x.builtin_key ? t(`devenv.builtin.${x.builtin_key}`) : x.description
}

// ---- templates ----------------------------------------------------------------
const busy = ref(false)
const tplForm = reactive({
  open: false,
  id: null as number | null,
  kind: 'custom' as DevTemplate['kind'],
  name: '',
  image: '',
  description: '',
  command: '',
  env: '',
  ports: [] as { name: string; port: number | null; kind: DevWebKind; mode: 'login' | 'direct' }[],
  keepEntrypoint: false,
  paths: '',
  shared: false,
  compat: false,
})
const webKinds: DevWebKind[] = ['desktop', 'jupyter', 'vscode', 'http']
const webKindOptions = computed(() => webKinds.map((k) => ({ value: k, label: t(`devenv.web.kind.${k}`) })))
const portModeOptions = computed(() =>
  (['login', 'direct'] as const).map((m) => ({ value: m, label: t(`devenv.ports.mode.${m}`) })),
)
function addPort() {
  tplForm.ports.push({ name: '', port: null, kind: 'http', mode: 'login' })
}
function openTemplate(x?: DevTemplate & { compat?: boolean }) {
  Object.assign(tplForm, {
    open: true,
    id: x?.id ?? null,
    kind: x?.kind ?? 'custom',
    name: x?.name ?? '',
    image: x?.image ?? '',
    description: x ? (x.builtin_key ? '' : x.description) : '',
    command: x?.command ?? '',
    env: x ? Object.entries(x.env).map(([k, v]) => `${k}=${v}`).join('\n') : '',
    ports: (x?.web_ports ?? []).map((p) => ({ ...p, mode: p.mode ?? 'login' })),
    keepEntrypoint: x?.keep_entrypoint ?? false,
    paths: x ? x.persist_paths.join('\n') : '',
    shared: false,
    compat: x?.compat ?? false,
  })
}
function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const i = line.indexOf('=')
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return out
}
function parseLines(text: string): string[] {
  return text
    .split('\n')
    .map((x) => x.trim())
    .filter(Boolean)
}
async function saveTemplate() {
  busy.value = true
  const body: Record<string, unknown> = {
    name: tplForm.name.trim(),
    image: tplForm.image.trim(),
    description: tplForm.description.trim(),
    command: tplForm.command.trim() || null,
    env: parseEnv(tplForm.env),
    web_ports: tplForm.ports
      .filter((p) => p.port)
      .map((p) => ({ name: p.name.trim(), port: p.port as number, kind: p.kind, mode: p.mode })),
    keep_entrypoint: tplForm.keepEntrypoint,
    persist_paths: parseLines(tplForm.paths),
  }
  if (isAdmin.value) body.compat = tplForm.compat
  try {
    if (tplForm.id) {
      if (tplForm.kind === 'saved') delete body.image
      if (tplForm.kind === 'builtin' && !body.description) delete body.description
      await api.devenv.updateTemplate(tplForm.id, body)
    } else {
      await api.devenv.createTemplate({ ...(body as unknown as DevTemplateInput), shared: tplForm.shared })
    }
    tplForm.open = false
    load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
async function toggleTemplate(x: DevTemplate, enabled: boolean) {
  try {
    await api.devenv.updateTemplate(x.id, { enabled })
    load()
  } catch (e) {
    showError(e)
  }
}
function removeTemplate(x: DevTemplate) {
  dialog.warning({
    title: t('common.delete'),
    content: x.kind === 'saved' ? t('devenv.deleteSavedConfirm', { name: x.name }) : t('devenv.deleteConfirm', { name: x.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.devenv.deleteTemplate(x.id)
        load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

// ---- kept environments (projects) -----------------------------------------------
const projectForm = reactive({ open: false, id: 0, name: '', paths: '' })
function openProject(p: DevProject) {
  Object.assign(projectForm, { open: true, id: p.id, name: p.name, paths: p.persist_paths.join('\n') })
}
async function saveProject() {
  busy.value = true
  try {
    await api.devenv.updateProject(projectForm.id, {
      name: projectForm.name.trim(),
      persist_paths: parseLines(projectForm.paths),
    })
    projectForm.open = false
    load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
const deleteData = ref(false)
function removeProject(p: DevProject) {
  deleteData.value = false
  dialog.warning({
    title: t('common.delete'),
    content: () =>
      h('div', { class: 'confirm' }, [
        h('p', t('devenv.deleteProjectConfirm', { name: p.name })),
        h(NCheckbox, {
          checked: deleteData.value,
          'onUpdate:checked': (v: boolean) => (deleteData.value = v),
          label: t('devenv.deleteData'),
        }),
      ]),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.devenv.deleteProject(p.id, deleteData.value)
        load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

const projectTone: Record<string, string> = { running: 'ok', paused: 'brand', missing: 'danger', new: '' }
</script>

<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1>{{ t('devenv.title') }}</h1>
        <p>{{ t('devenvUi.subtitle') }}</p>
      </div>
      <NButton type="primary" secondary :disabled="!info?.enabled" @click="book()">
        <template #icon><Terminal :size="16" /></template>
        {{ t('devenv.book') }}
      </NButton>
    </header>

    <div v-if="info && !info.enabled" class="card card-pad notice">{{ t('devenv.disabled') }}</div>
    <div v-else-if="info && !info.docker.ok" class="card card-pad notice bad">
      {{ t('devenv.dockerDown') }}
      <span v-if="isAdmin && info.docker.error" class="mono small"> {{ info.docker.error }}</span>
    </div>

    <template v-if="info?.enabled && loaded">
      <!-- your environments first, when there are any -->
      <section v-if="open.length || upcoming.length || isAdmin" class="block" data-test="my-environments">
        <div class="block-head">
          <h2 class="block-title">{{ t('devenv.sessions') }}</h2>
          <div class="row head-actions">
            <label v-if="isAdmin" class="row small toggle">
              <NSwitch v-model:value="everyone" size="small" @update:value="load" />{{ t('devenv.everyone') }}
            </label>
            <NButton v-if="open.length" size="small" :secondary="!showStart" data-test="open-new" @click="showStart = !showStart">
              <template #icon><Plus :size="14" /></template>
              {{ t('devenvUi.start.openNew') }}
            </NButton>
          </div>
        </div>
        <StartCard
          v-if="open.length && showStart"
          :templates="templates"
          :projects="projects"
          :info="info"
          :can-log-in="canLogIn"
          @started="started"
          @book="book"
          @credentials="load"
        />
        <p v-if="!open.length && !upcoming.length" class="faint small">{{ t('devenvUi.empty') }}</p>
        <EnvCard
          v-for="s in open"
          :key="s.id"
          :session="s"
          :title="titles.get(s.id) ?? s.image"
          :alias="aliases.get(s.id) ?? 'slotserve'"
          :ssh-host="sshHost"
          :booking="bookingOf(s)"
          :project="projectOf(s)"
          :show-owner="everyone && !s.mine"
          :can-log-in="canLogIn"
          :password-login="!!info.password_login"
          @changed="load"
        />
        <article v-for="r in upcoming" :key="`r${r.id}`" class="card card-pad env upcoming" data-test="upcoming">
          <div class="row wrap env-head">
            <span class="strong">{{ r.note?.trim() || r.devenv_project_name || r.devenv_template_name }}</span>
            <span class="badge">{{ r.status === 'pending' ? t('phase.pending') : t('phase.upcoming') }}</span>
            <span class="faint small num">{{ formatRange(r.start_at, r.end_at) }}<template v-if="r.lane_name"> · {{ r.lane_name }}</template></span>
            <span class="spacer" />
            <NButton size="small" quaternary @click="cancelBooking(r)">{{ t('devenvUi.upcoming.cancel') }}</NButton>
          </div>
          <p class="muted small">{{ t('devenv.preparedAhead', { minutes: info.pull_minutes ?? 60 }) }}</p>
        </article>
      </section>

      <!-- open a new one: the first thing on the page when nothing is open -->
      <StartCard
        v-if="!open.length"
        :templates="templates"
        :projects="projects"
        :info="info"
        :can-log-in="canLogIn"
        @started="started"
        @book="book"
        @credentials="load"
      />
      <p v-if="!open.length && !upcoming.length && !isAdmin" class="faint small empty-line">{{ t('devenvUi.empty') }}</p>

      <!-- shared environments of idle lanes -->
      <section v-if="sharedSessions.length" class="block">
        <div class="block-head">
          <h2 class="block-title">{{ t('devenv.sharedTitle') }}</h2>
        </div>
        <SharedEnvCard
          v-for="s in sharedSessions"
          :key="`shared${s.id}`"
          :session="s"
          :alias="aliases.get(s.id) ?? 'slotserve-shared'"
          :ssh-host="sshHost"
          :has-keys="hasKeys"
          @changed="load"
        />
      </section>

      <details v-if="history.length" class="history block">
        <summary class="small muted">{{ t('devenv.history') }}</summary>
        <ul>
          <li v-for="s in history" :key="s.id" class="small">
            <span class="strong">{{ s.project_name ?? s.template_name ?? s.image }}</span>
            <span class="faint num"> · {{ s.started_at ? formatDateTime(s.started_at) : '—' }}</span>
            <span v-if="everyone" class="faint"> · {{ s.username }}</span>
          </li>
        </ul>
      </details>

      <!-- kept environments -->
      <section v-if="projects.length" class="block">
        <div class="block-head">
          <h2 class="block-title">{{ t('devenv.projects') }}</h2>
        </div>
        <p class="muted small">{{ t('devenv.projectsHint') }}</p>
        <div class="grid">
          <article v-for="p in projects" :key="p.id" class="card card-pad tpl" data-kind="project">
            <div class="row wrap">
              <span class="strong">{{ p.name }}</span>
              <span class="badge" :class="projectTone[p.state]">{{ t(`devenv.projectState.${p.state}`) }}</span>
              <span v-if="everyone && p.owner" class="faint small">{{ p.owner }}</span>
            </div>
            <div class="small faint">{{ p.template_name ?? p.image }}</div>
            <p class="small muted">
              <template v-if="p.last_used_at">{{ t('devenv.lastUsed', { time: relativeFromNow(p.last_used_at) }) }}</template>
              <template v-if="p.size_bytes !== null"> · {{ formatBytes(p.size_bytes) }}</template>
              <template v-if="p.upcoming"> · {{ t('devenv.upcomingBookings', { n: p.upcoming }) }}</template>
            </p>
            <p v-if="p.persist_paths.length" class="mono small faint">{{ p.persist_paths.join('  ') }}</p>
            <CpuProjectControls :project="p" @changed="load" />
            <div class="row wrap actions">
              <NButton v-if="p.mine" size="tiny" secondary @click="book({ templateId: null, projectId: p.id })">{{ t('devenvUi.bookGpu') }}</NButton>
              <NButton size="tiny" quaternary @click="openProject(p)">{{ t('common.edit') }}</NButton>
              <NButton size="tiny" quaternary type="error" :disabled="p.state === 'running'" @click="removeProject(p)">
                {{ t('common.delete') }}
              </NButton>
            </div>
          </article>
        </div>
        <p v-if="info.project_days" class="faint small">{{ t('devenv.projectDays', { n: info.project_days }) }}</p>
      </section>

      <!-- templates -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title">{{ t('devenv.templates') }}</h2>
          <NButton v-if="info.custom_images || isAdmin" size="small" secondary @click="openTemplate()">
            <template #icon><Plus :size="14" /></template>
            {{ t('devenv.newTemplate') }}
          </NButton>
        </div>
        <div class="grid">
          <article v-for="x in builtin" :key="x.id" class="card card-pad tpl" :class="{ off: !x.enabled }" data-test="template">
            <div class="row wrap">
              <span class="strong">{{ x.name }}</span>
              <span v-if="!x.enabled" class="badge">{{ t('devenv.off') }}</span>
              <span v-for="p in x.web_ports" :key="p.port" class="badge brand">{{ t(`devenv.web.kind.${p.kind}`) }}</span>
              <span v-if="!x.web_ports.length" class="badge">SSH</span>
              <span v-if="isAdmin && x.compat" class="badge warn" :title="t('devenvUi.compatHint')">{{ t('devenvUi.compat') }}</span>
              <span class="spacer" />
              <NSwitch v-if="isAdmin" :value="x.enabled" size="small" @update:value="(v: boolean) => toggleTemplate(x, v)" />
            </div>
            <p class="small muted">{{ describeTemplate(x) }}</p>
            <details class="tpl-details">
              <summary class="small faint">{{ t('devenvUi.details') }}</summary>
              <div class="mono small faint image">{{ x.image }}</div>
            </details>
            <div v-if="x.enabled" class="row wrap actions">
              <CpuStartButton :template-id="x.id" @started="load" />
              <NButton size="tiny" quaternary @click="book({ templateId: x.id, projectId: null })">{{ t('devenvUi.bookGpu') }}</NButton>
            </div>
            <div v-if="isAdmin" class="row actions">
              <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
              <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
            </div>
          </article>
        </div>
        <h3 class="sub">{{ t('devenv.mine') }}</h3>
        <div v-if="!ownTemplates.length" class="faint small">{{ t('devenv.mineEmpty', { n: info.saved_limit ?? 3 }) }}</div>
        <div class="grid">
          <article v-for="x in ownTemplates" :key="x.id" class="card card-pad tpl">
            <div class="row wrap">
              <span class="strong">{{ x.name }}</span>
              <span class="badge" :class="{ brand: x.kind === 'saved' }">{{ t(`devenv.kind.${x.kind}`) }}</span>
              <span v-for="p in x.web_ports" :key="p.port" class="badge brand">{{ t(`devenv.web.kind.${p.kind}`) }}</span>
              <span v-if="everyone && x.owner" class="faint small">{{ x.owner }}</span>
            </div>
            <p v-if="x.description" class="small muted">{{ x.description }}</p>
            <details class="tpl-details">
              <summary class="small faint">{{ t('devenvUi.details') }}</summary>
              <div class="mono small faint image">{{ x.image }}</div>
            </details>
            <div class="row wrap actions">
              <CpuStartButton :template-id="x.id" @started="load" />
              <NButton size="tiny" quaternary @click="book({ templateId: x.id, projectId: null })">{{ t('devenvUi.bookGpu') }}</NButton>
              <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
              <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
            </div>
          </article>
        </div>
        <p class="faint small share-hint">{{ t('devenvUi.shareHint') }}</p>
      </section>

      <!-- direct ports (host ports) -->
      <PublicPorts :is-admin="isAdmin" />

      <!-- environments without a GPU: limits (cores of the shared pool for admins only) -->
      <CpuPanel :templates="templates" :projects="projects" @changed="load" />

      <!-- files and storage -->
      <StorageInfo />

      <p class="faint small cred-hint">
        <KeyRound :size="13" />
        {{ t('devenv.credentialsHint') }}
        <RouterLink :to="{ name: 'keys' }">{{ t('devenv.credentials') }}</RouterLink>
      </p>
    </template>

    <!-- dialogs -->
    <NModal
      v-model:show="tplForm.open"
      preset="card"
      :title="tplForm.id ? t('devenv.editTemplate') : t('devenv.newTemplate')"
      style="max-width: 560px"
    >
      <div class="form">
        <label>
          <span class="field-label">{{ t('devenv.templateName') }}</span>
          <NInput v-model:value="tplForm.name" maxlength="64" :placeholder="t('ph.templateName')" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.image') }}</span>
          <NInput v-model:value="tplForm.image" class="mono" :disabled="tplForm.kind === 'saved'" placeholder="pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel" />
          <span class="faint small">{{ t('devenv.imageHint') }}</span>
        </label>
        <label>
          <span class="field-label">{{ t('devenv.description') }}</span>
          <NInput v-model:value="tplForm.description" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" :placeholder="t('ph.description')" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.command') }}</span>
          <NInput v-model:value="tplForm.command" class="mono" :placeholder="t('devenv.commandPlaceholder')" />
          <span class="faint small">{{ t('devenv.commandHint') }}</span>
        </label>
        <label>
          <span class="field-label">{{ t('devenv.env') }}</span>
          <NInput v-model:value="tplForm.env" type="textarea" class="mono" placeholder="HF_HOME=/workspace/.cache/huggingface" :autosize="{ minRows: 2, maxRows: 6 }" />
        </label>
        <fieldset class="web-fields">
          <legend class="field-label">{{ t('devenv.web.section') }}</legend>
          <div v-for="(p, i) in tplForm.ports" :key="i" class="row wrap web-row">
            <NInput v-model:value="p.name" maxlength="32" :placeholder="t('devenv.web.name')" style="width: 140px" />
            <NInputNumber
              v-model:value="p.port"
              :min="1"
              :max="65535"
              :show-button="false"
              :placeholder="t('devenv.web.port')"
              style="width: 120px"
            />
            <NSelect v-model:value="p.kind" :options="webKindOptions" :placeholder="t('ph.webKind')" style="width: 150px" />
            <NSelect v-model:value="p.mode" :options="portModeOptions" :placeholder="t('ph.portMode')" style="width: 160px" />
            <NButton size="small" quaternary type="error" @click="tplForm.ports.splice(i, 1)">{{ t('common.delete') }}</NButton>
          </div>
          <div>
            <NButton size="small" secondary :disabled="tplForm.ports.length >= 10" @click="addPort">
              <template #icon><Plus :size="14" /></template>
              {{ t('devenv.web.addPort') }}
            </NButton>
          </div>
          <span class="faint small">{{ t('devenv.web.portHint') }}</span>
          <label class="row small toggle">
            <NSwitch v-model:value="tplForm.keepEntrypoint" size="small" />{{ t('devenv.web.keepEntrypoint') }}
          </label>
          <span class="faint small">{{ t('devenv.web.keepEntrypointHint') }}</span>
        </fieldset>
        <label>
          <span class="field-label">{{ t('devenv.persistPaths') }}</span>
          <NInput v-model:value="tplForm.paths" type="textarea" class="mono" placeholder="/root" :autosize="{ minRows: 1, maxRows: 6 }" />
          <span class="faint small">{{ t('devenv.persistPathsHint') }}</span>
        </label>
        <label v-if="isAdmin && !tplForm.id" class="row small toggle">
          <NSwitch v-model:value="tplForm.shared" size="small" />{{ t('devenv.shared') }}
        </label>
        <template v-if="isAdmin">
          <label class="row small toggle">
            <NSwitch v-model:value="tplForm.compat" size="small" />{{ t('devenvUi.compatSwitch') }}
          </label>
          <span class="faint small">{{ t('devenvUi.compatHint') }}</span>
        </template>
      </div>
      <template #footer>
        <div class="footer">
          <NButton @click="tplForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!tplForm.name.trim() || !tplForm.image.trim()" @click="saveTemplate">
            {{ t('common.save') }}
          </NButton>
        </div>
      </template>
    </NModal>

    <NModal v-model:show="projectForm.open" preset="card" :title="t('devenv.editProject')" style="max-width: 520px">
      <div class="form">
        <label>
          <span class="field-label">{{ t('devenv.templateName') }}</span>
          <NInput v-model:value="projectForm.name" maxlength="64" :placeholder="t('ph.keptName')" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.persistPaths') }}</span>
          <NInput v-model:value="projectForm.paths" type="textarea" class="mono" placeholder="/root" :autosize="{ minRows: 2, maxRows: 6 }" />
          <span class="faint small">{{ t('devenv.persistPathsHint') }}</span>
        </label>
      </div>
      <template #footer>
        <div class="footer">
          <NButton @click="projectForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!projectForm.name.trim()" @click="saveProject">{{ t('common.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <QuickBook
      v-model:show="quickBook.show"
      intent="devenv"
      :template-id="quickBook.templateId ?? undefined"
      :project-id="quickBook.projectId ?? undefined"
      @done="load"
    >
      <template #summary="{ kind, templateId, projectId, laneId }">
        <PreviewLine v-if="kind === 'devenv'" :template-id="templateId" :project-id="projectId" :lane-id="laneId" gpu />
      </template>
    </QuickBook>
  </div>
</template>

<style scoped>
.notice {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  color: var(--text-2);
}
.notice.bad {
  color: var(--danger);
  background: var(--danger-soft);
}
.cred-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0 0;
}
.empty-line {
  margin: -12px 0 24px;
}
.block {
  margin-bottom: 28px;
}
.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.head-actions {
  gap: 12px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
}
.sub {
  font-size: 13px;
  font-weight: 600;
  margin: 18px 0 10px;
  color: var(--text-2);
}
.env {
  margin-bottom: 12px;
  display: grid;
  gap: 8px;
}
.env-head {
  gap: 10px;
}
.env p {
  margin: 0;
}
.image {
  word-break: break-all;
}
.web-fields {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.web-fields legend {
  padding: 0 4px;
}
.web-row {
  gap: 8px;
}
.actions {
  gap: 8px;
}
.toggle {
  gap: 6px;
  cursor: pointer;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.tpl {
  display: grid;
  gap: 6px;
  align-content: start;
  min-width: 0;
}
.tpl p {
  margin: 0;
}
.tpl.off {
  opacity: 0.6;
}
.tpl-details summary {
  cursor: pointer;
}
.share-hint {
  margin: 12px 0 0;
}
.history summary {
  cursor: pointer;
}
.history ul {
  margin: 8px 0 0;
  padding-left: 18px;
}
.form {
  display: grid;
  gap: 14px;
}
.field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
}
.form label {
  display: grid;
  gap: 6px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 600px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
