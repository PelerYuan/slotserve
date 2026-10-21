<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, useDialog, useMessage } from 'naive-ui'
import { ExternalLink, KeyRound, Plus, Terminal, Users } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import StorageInfo from '@/components/StorageInfo.vue' // storage hook
// devenv_cpu: environments without a GPU
import CpuPanel from '@/components/devenv/CpuPanel.vue'
import CpuProjectControls from '@/components/devenv/CpuProjectControls.vue'
import CpuSessionInfo from '@/components/devenv/CpuSessionInfo.vue'
import CpuStartButton from '@/components/devenv/CpuStartButton.vue'
import { api } from '@/api/client'
import type { DevInfo, DevProject, DevSession, DevTemplate, DevWebKind, Reservation } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, formatDateTime, formatRange, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

const info = ref<DevInfo | null>(null)
const sessions = ref<DevSession[]>([])
const templates = ref<DevTemplate[]>([])
const hasKeys = ref(true)
const projects = ref<DevProject[]>([])
const upcoming = ref<Reservation[]>([])
const everyone = ref(false)

const sharedSessions = computed(() => sessions.value.filter((s) => s.kind === 'shared'))
const own = computed(() => sessions.value.filter((s) => s.kind !== 'shared'))
const open = computed(() => own.value.filter((s) => s.state !== 'stopped'))
const history = computed(() => own.value.filter((s) => s.state === 'stopped').slice(0, 8))
// A way in: an SSH key, or the environment password while password logins are allowed.
const canLogIn = computed(() => hasKeys.value || (!!info.value?.password_login && !!info.value?.password_set))
const prepared = computed(() => new Set(sessions.value.map((s) => s.reservation_id)))
const notPrepared = computed(() => upcoming.value.filter((r) => !prepared.value.has(r.id)))
const shared = computed(() => templates.value.filter((x) => x.kind === 'builtin'))
const ownTemplates = computed(() => templates.value.filter((x) => x.kind !== 'builtin'))

async function load() {
  try {
    const [i, s, tpl, k, p, mine] = await Promise.all([
      api.devenv.info(),
      api.devenv.sessions(everyone.value),
      api.devenv.templates(),
      api.sshKeys(),
      api.devenv.projects(everyone.value),
      api.reservations(new Date(Date.now() - 3_600_000), new Date(Date.now() + 60 * 86_400_000), true),
    ])
    info.value = i
    sessions.value = s
    templates.value = tpl
    hasKeys.value = k.length > 0
    projects.value = p
    upcoming.value = mine.filter(
      (r) =>
        (r.devenv_template_id || r.devenv_project_id) &&
        (r.phase === 'upcoming' || r.phase === 'pending' || r.phase === 'active'),
    )
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

function book() {
  router.push({ name: 'schedule', query: { book: 'devenv' } })
}

function describeTemplate(x: DevTemplate) {
  return x.builtin_key ? t(`devenv.builtin.${x.builtin_key}`) : x.description
}

function vscodeConfig(s: DevSession) {
  return `Host ${s.kind === 'shared' ? `slotserve-${s.lane_name ?? 'shared'}`.replace(/[^\w.-]/g, '-') : 'slotserve-dev'}
  HostName ${info.value?.ssh_host ?? 'server'}
  Port ${s.ssh_port}
  User ${s.ssh_user ?? 'root'}`
}

// ---- session actions --------------------------------------------------------
async function restart(s: DevSession) {
  try {
    await api.devenv.restart(s.id)
    message.success(t('devenv.restarting'))
    load()
  } catch (e) {
    showError(e)
  }
}

const saveForm = reactive({ open: false, id: 0, name: '' })
const busy = ref(false)
function openSave(s: DevSession) {
  Object.assign(saveForm, { open: true, id: s.id, name: s.template_name ?? '' })
}
async function save() {
  busy.value = true
  try {
    await api.devenv.save(saveForm.id, saveForm.name.trim())
    saveForm.open = false
    message.success(t('devenv.saving'))
    load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

const logs = reactive({ open: false, text: '', title: '' })
async function showLogs(s: DevSession) {
  try {
    logs.text = (await api.devenv.logs(s.id)).text || t('devenv.noLogs')
    logs.title = s.template_name ?? s.image
    logs.open = true
  } catch (e) {
    showError(e)
  }
}

// ---- templates ----------------------------------------------------------------
const tplForm = reactive({
  open: false,
  id: null as number | null,
  kind: 'custom' as DevTemplate['kind'],
  name: '',
  image: '',
  description: '',
  command: '',
  env: '',
  ports: [] as { name: string; port: number | null; kind: DevWebKind }[],
  keepEntrypoint: false,
  paths: '',
  shared: false,
})
const webKinds: DevWebKind[] = ['desktop', 'jupyter', 'vscode', 'http']
const webKindOptions = computed(() => webKinds.map((k) => ({ value: k, label: t(`devenv.web.kind.${k}`) })))
function addPort() {
  tplForm.ports.push({ name: '', port: null, kind: 'http' })
}
function openLabel(p: { name: string; kind: DevWebKind }) {
  return p.kind === 'http' ? t('devenv.web.openNamed', { name: p.name }) : t(`devenv.web.open.${p.kind}`)
}
function openTemplate(x?: DevTemplate) {
  Object.assign(tplForm, {
    open: true,
    id: x?.id ?? null,
    kind: x?.kind ?? 'custom',
    name: x?.name ?? '',
    image: x?.image ?? '',
    description: x ? (x.builtin_key ? '' : x.description) : '',
    command: x?.command ?? '',
    env: x ? Object.entries(x.env).map(([k, v]) => `${k}=${v}`).join('\n') : '',
    ports: (x?.web_ports ?? []).map((p) => ({ ...p })),
    keepEntrypoint: x?.keep_entrypoint ?? false,
    paths: x ? x.persist_paths.join('\n') : '',
    shared: false,
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
  const body = {
    name: tplForm.name.trim(),
    image: tplForm.image.trim(),
    description: tplForm.description.trim(),
    command: tplForm.command.trim() || null,
    env: parseEnv(tplForm.env),
    web_ports: tplForm.ports
      .filter((p) => p.port)
      .map((p) => ({ name: p.name.trim(), port: p.port as number, kind: p.kind })),
    keep_entrypoint: tplForm.keepEntrypoint,
    persist_paths: parseLines(tplForm.paths),
  }
  try {
    if (tplForm.id) {
      const patch: Record<string, unknown> = { ...body }
      if (tplForm.kind === 'saved') delete patch.image
      if (tplForm.kind === 'builtin' && !body.description) delete patch.description
      await api.devenv.updateTemplate(tplForm.id, patch)
    } else {
      await api.devenv.createTemplate({ ...body, shared: tplForm.shared })
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

const stateTone: Record<string, string> = {
  running: 'ok',
  failed: 'danger',
  pulling: 'warn',
  starting: 'warn',
  stopping: 'warn',
  ready: 'brand',
  waiting: '',
  stopped: '',
}
</script>

<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1>{{ t('devenv.title') }}</h1>
        <p>{{ t('devenv.subtitle') }}</p>
      </div>
      <NButton type="primary" :disabled="!info?.enabled" @click="book">
        <template #icon><Terminal :size="16" /></template>
        {{ t('devenv.book') }}
      </NButton>
    </header>

    <div v-if="info && !info.enabled" class="card card-pad notice">{{ t('devenv.disabled') }}</div>
    <div v-else-if="info && !info.docker.ok" class="card card-pad notice bad">
      {{ t('devenv.dockerDown') }}
      <span v-if="isAdmin && info.docker.error" class="mono small"> {{ info.docker.error }}</span>
    </div>

    <div v-if="info?.enabled && !canLogIn" class="card card-pad notice warn">
      <KeyRound :size="16" />
      <span>{{ info.password_login ? t('devenv.needKey') : t('devenv.needKeyOnly') }}</span>
      <NButton size="small" type="primary" secondary @click="router.push({ name: 'keys' })">{{ t('devenv.credentials') }}</NButton>
    </div>
    <p v-else-if="info?.enabled" class="faint small cred-hint">
      <KeyRound :size="13" />
      {{ t('devenv.credentialsHint') }}
      <RouterLink :to="{ name: 'keys' }">{{ t('devenv.credentials') }}</RouterLink>
    </p>

    <!-- shared environments of idle lanes -->
    <section v-if="sharedSessions.length" class="block">
      <div class="block-head">
        <h2 class="block-title">{{ t('devenv.sharedTitle') }}</h2>
      </div>
      <article v-for="s in sharedSessions" :key="`shared${s.id}`" class="card card-pad env" data-kind="shared">
        <div class="row env-head">
          <Users :size="15" class="faint" />
          <span class="strong">{{ s.lane_name }}</span>
          <span class="badge brand">{{ t('devenv.sharedBadge') }}</span>
          <span class="badge" :class="stateTone[s.state]">{{ t(`devenv.state.${s.state}`) }}</span>
          <span class="spacer" />
          <span class="faint small">{{ s.template_name ?? s.image }}</span>
        </div>
        <p class="muted small">{{ t('devenv.sharedHint', { user: s.ssh_user ?? '?' }) }}</p>
        <div v-if="s.state === 'pulling'" class="progress">
          <div class="bar"><span :style="{ width: `${s.progress ?? 3}%` }" /></div>
          <div class="faint small">{{ t('devenv.pulling', { pct: s.progress !== null ? Math.round(s.progress) : '…' }) }}</div>
        </div>
        <template v-if="s.state === 'running' && s.ssh_command">
          <div class="ssh">
            <div class="faint small">{{ t('devenv.connect') }}</div>
            <CodeBlock :code="s.ssh_command" inline />
          </div>
          <details class="vscode">
            <summary class="small">{{ t('devenv.vscode') }}</summary>
            <CodeBlock :code="vscodeConfig(s)" />
          </details>
        </template>
        <div v-if="isAdmin && s.error" class="failure"><pre class="mono small">{{ s.error }}</pre></div>
        <div v-if="isAdmin" class="row wrap actions">
          <NButton v-if="s.state === 'running' || s.state === 'failed'" size="small" secondary @click="restart(s)">
            {{ s.state === 'failed' ? t('devenv.retry') : t('devenv.restart') }}
          </NButton>
          <NButton size="small" quaternary @click="showLogs(s)">{{ t('devenv.logs') }}</NButton>
        </div>
      </article>
    </section>

    <!-- devenv_cpu: environments without a GPU -->
    <CpuPanel v-if="info?.enabled" :templates="templates" :projects="projects" :can-log-in="canLogIn" @changed="load" />

    <!-- environments -->
    <section class="block">
      <div class="block-head">
        <h2 class="block-title">{{ t('devenv.sessions') }}</h2>
        <label v-if="isAdmin" class="row small toggle">
          <NSwitch v-model:value="everyone" size="small" @update:value="load" />{{ t('devenv.everyone') }}
        </label>
      </div>
      <div v-if="!open.length && !notPrepared.length" class="card card-pad empty">{{ t('devenv.noSessions') }}</div>

      <article v-for="s in open" :key="s.id" class="card card-pad env">
        <div class="row env-head">
          <span class="strong">{{ s.project_name ?? s.template_name ?? s.image }}</span>
          <span class="badge" :class="stateTone[s.state]">{{ t(`devenv.state.${s.state}`) }}</span>
          <span v-if="s.project_name && s.template_name" class="faint small">{{ s.template_name }}</span>
          <span v-if="everyone" class="faint small">{{ s.username }}</span>
          <span class="spacer" />
          <span class="faint small num">
            <template v-if="s.start_at && s.end_at">{{ formatRange(s.start_at, s.end_at) }}</template>
            <template v-if="s.lane_name"> · {{ s.lane_name }}</template>
          </span>
        </div>
        <div class="mono small faint image">{{ s.image }}</div>
        <CpuSessionInfo v-if="s.kind === 'cpu'" :session="s" :projects="projects" @changed="load" />

        <div v-if="s.state === 'pulling'" class="progress">
          <div class="bar"><span :style="{ width: `${s.progress ?? 3}%` }" /></div>
          <div class="faint small">
            {{ t('devenv.pulling', { pct: s.progress !== null ? Math.round(s.progress) : '…' }) }}
            <span v-if="s.detail" class="mono"> · {{ s.detail }}</span>
          </div>
        </div>
        <p v-else-if="(s.state === 'ready' || s.state === 'waiting') && s.kind !== 'cpu'" class="muted small">
          {{ t('devenv.waitingStart') }}
        </p>
        <p v-else-if="s.state === 'starting'" class="muted small">
          {{ s.detail === 'snapshot' ? t('devenv.snapshot') : t('devenv.starting') }}
        </p>

        <div v-if="s.state === 'running' && s.web.length" class="row wrap web">
          <NButton
            v-for="p in s.web"
            :key="p.port"
            tag="a"
            :href="p.url"
            target="_blank"
            rel="noopener"
            type="primary"
            size="small"
          >
            <template #icon><ExternalLink :size="14" /></template>
            {{ openLabel(p) }}
          </NButton>
          <span class="faint small">{{ t('devenv.web.newTab') }}</span>
        </div>

        <template v-if="s.state === 'running' && s.ssh_command">
          <div class="ssh">
            <div class="faint small">{{ t('devenv.connect') }}</div>
            <CodeBlock :code="s.ssh_command" inline />
          </div>
          <details class="vscode">
            <summary class="small">{{ t('devenv.vscode') }}</summary>
            <p class="faint small">{{ t('devenv.vscodeHint') }}</p>
            <CodeBlock :code="vscodeConfig(s)" />
          </details>
          <p v-if="info?.host_key_fingerprint" class="faint small">
            {{ t('devenv.hostKey') }} <span class="mono">{{ info.host_key_fingerprint }}</span>
          </p>
          <p v-if="s.detail === 'saving'" class="small warn-text">{{ t('devenv.saving') }}</p>
        </template>

        <div v-if="s.state === 'failed'" class="failure">
          <pre class="mono small">{{ s.error }}</pre>
        </div>
        <p v-else-if="s.error" class="small warn-text">{{ s.error }}</p>

        <div class="row wrap actions">
          <NButton v-if="s.state === 'running'" size="small" secondary :disabled="s.detail === 'saving'" @click="openSave(s)">
            {{ t('devenv.save') }}
          </NButton>
          <NButton v-if="s.state === 'running' || s.state === 'failed'" size="small" secondary @click="restart(s)">
            {{ s.state === 'failed' ? t('devenv.retry') : t('devenv.restart') }}
          </NButton>
          <NButton size="small" quaternary @click="showLogs(s)">{{ t('devenv.logs') }}</NButton>
        </div>
      </article>

      <article v-for="r in notPrepared" :key="`r${r.id}`" class="card card-pad env">
        <div class="row env-head">
          <span class="strong">{{ r.devenv_project_name ?? r.devenv_template_name }}</span>
          <span class="badge">{{ r.status === 'pending' ? t('phase.pending') : t('phase.upcoming') }}</span>
          <span class="spacer" />
          <span class="faint small num">{{ formatRange(r.start_at, r.end_at) }}</span>
        </div>
        <p class="muted small">{{ t('devenv.preparedAhead', { minutes: info?.pull_minutes ?? 60 }) }}</p>
      </article>

      <details v-if="history.length" class="history">
        <summary class="small muted">{{ t('devenv.history') }}</summary>
        <ul>
          <li v-for="s in history" :key="s.id" class="small">
            <span class="strong">{{ s.template_name ?? s.image }}</span>
            <span class="faint num"> · {{ s.started_at ? formatDateTime(s.started_at) : '—' }}</span>
            <span v-if="everyone" class="faint"> · {{ s.username }}</span>
          </li>
        </ul>
      </details>
    </section>

    <!-- kept environments -->
    <section class="block">
      <div class="block-head">
        <h2 class="block-title">{{ t('devenv.projects') }}</h2>
      </div>
      <p class="muted small">{{ t('devenv.projectsHint') }}</p>
      <div v-if="!projects.length" class="faint small">{{ t('devenv.projectsEmpty', { n: info?.projects_limit ?? 3 }) }}</div>
      <div class="grid">
        <article v-for="p in projects" :key="p.id" class="card card-pad tpl" data-kind="project">
          <div class="row">
            <span class="strong">{{ p.name }}</span>
            <span class="badge" :class="projectTone[p.state]">{{ t(`devenv.projectState.${p.state}`) }}</span>
            <span v-if="everyone && p.owner" class="faint small">{{ p.owner }}</span>
          </div>
          <div class="mono small faint image">{{ p.template_name ?? p.image }}</div>
          <p class="small muted">
            <template v-if="p.last_used_at">{{ t('devenv.lastUsed', { time: relativeFromNow(p.last_used_at) }) }}</template>
            <template v-if="p.size_bytes !== null"> · {{ formatBytes(p.size_bytes) }}</template>
            <template v-if="p.upcoming"> · {{ t('devenv.upcomingBookings', { n: p.upcoming }) }}</template>
          </p>
          <p v-if="p.persist_paths.length" class="mono small faint">{{ p.persist_paths.join('  ') }}</p>
          <CpuProjectControls :project="p" @changed="load" />
          <div class="row actions">
            <NButton size="tiny" quaternary @click="openProject(p)">{{ t('common.edit') }}</NButton>
            <NButton size="tiny" quaternary type="error" :disabled="p.state === 'running'" @click="removeProject(p)">
              {{ t('common.delete') }}
            </NButton>
          </div>
        </article>
      </div>
      <p v-if="info?.project_days" class="faint small">{{ t('devenv.projectDays', { n: info.project_days }) }}</p>
    </section>

    <!-- templates -->
    <section class="block">
      <div class="block-head">
        <h2 class="block-title">{{ t('devenv.templates') }}</h2>
        <NButton v-if="info?.custom_images || isAdmin" size="small" secondary @click="openTemplate()">
          <template #icon><Plus :size="14" /></template>
          {{ t('devenv.newTemplate') }}
        </NButton>
      </div>
      <div class="grid">
        <article v-for="x in shared" :key="x.id" class="card card-pad tpl" :class="{ off: !x.enabled }">
          <div class="row">
            <span class="strong">{{ x.name }}</span>
            <span v-if="!x.enabled" class="badge">{{ t('devenv.off') }}</span>
            <span v-for="p in x.web_ports" :key="p.port" class="badge brand">{{ t(`devenv.web.kind.${p.kind}`) }}</span>
            <span class="spacer" />
            <template v-if="isAdmin">
              <NSwitch :value="x.enabled" size="small" @update:value="(v: boolean) => toggleTemplate(x, v)" />
            </template>
          </div>
          <div class="mono small faint image">{{ x.image }}</div>
          <p class="small muted">{{ describeTemplate(x) }}</p>
          <div v-if="x.enabled" class="row actions"><CpuStartButton :template-id="x.id" @started="load" /></div>
          <div v-if="isAdmin" class="row actions">
            <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
            <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
          </div>
        </article>
      </div>
      <h3 class="sub">{{ t('devenv.mine') }}</h3>
      <div v-if="!ownTemplates.length" class="faint small">{{ t('devenv.mineEmpty', { n: info?.saved_limit ?? 3 }) }}</div>
      <div class="grid">
        <article v-for="x in ownTemplates" :key="x.id" class="card card-pad tpl">
          <div class="row">
            <span class="strong">{{ x.name }}</span>
            <span class="badge" :class="{ brand: x.kind === 'saved' }">{{ t(`devenv.kind.${x.kind}`) }}</span>
            <span v-for="p in x.web_ports" :key="p.port" class="badge brand">{{ t(`devenv.web.kind.${p.kind}`) }}</span>
          </div>
          <div class="mono small faint image">{{ x.image }}</div>
          <p v-if="x.description" class="small muted">{{ x.description }}</p>
          <div class="row actions">
            <CpuStartButton :template-id="x.id" @started="load" />
            <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
            <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
          </div>
        </article>
      </div>
    </section>

    <!-- storage hook: storages from Admin -> Storage -->
    <StorageInfo v-if="info?.enabled" />

    <!-- dialogs -->
    <NModal v-model:show="saveForm.open" preset="card" :title="t('devenv.save')" style="max-width: 460px">
      <p class="muted small">{{ t('devenv.saveHint') }}</p>
      <NInput v-model:value="saveForm.name" :placeholder="t('devenv.templateName')" maxlength="64" />
      <template #footer>
        <div class="footer">
          <NButton @click="saveForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!saveForm.name.trim()" @click="save">{{ t('devenv.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <NModal v-model:show="logs.open" preset="card" :title="logs.title" style="max-width: 820px">
      <pre class="log mono">{{ logs.text }}</pre>
    </NModal>

    <NModal
      v-model:show="tplForm.open"
      preset="card"
      :title="tplForm.id ? t('devenv.editTemplate') : t('devenv.newTemplate')"
      style="max-width: 560px"
    >
      <div class="form">
        <label>
          <span class="field-label">{{ t('devenv.templateName') }}</span>
          <NInput v-model:value="tplForm.name" maxlength="64" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.image') }}</span>
          <NInput v-model:value="tplForm.image" class="mono" :disabled="tplForm.kind === 'saved'" placeholder="pytorch/pytorch:2.14.1-cuda12.6-cudnn9-devel" />
          <span class="faint small">{{ t('devenv.imageHint') }}</span>
        </label>
        <label>
          <span class="field-label">{{ t('devenv.description') }}</span>
          <NInput v-model:value="tplForm.description" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" />
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
            <NSelect v-model:value="p.kind" :options="webKindOptions" style="width: 150px" />
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
          <NInput v-model:value="projectForm.name" maxlength="64" />
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
.notice.warn {
  background: var(--brand-softer);
}
.cred-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin: -4px 0 16px;
}
.block {
  margin-bottom: 28px;
}
.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
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
.empty {
  color: var(--text-3);
  text-align: center;
}
.env {
  margin-bottom: 12px;
  display: grid;
  gap: 10px;
}
.env-head {
  gap: 10px;
}
.image {
  word-break: break-all;
}
.progress .bar {
  height: 8px;
  border-radius: 999px;
  background: var(--surface-muted);
  overflow: hidden;
  margin-bottom: 6px;
}
.progress .bar span {
  display: block;
  height: 100%;
  background: var(--brand);
  transition: width 0.4s ease;
}
.ssh {
  display: grid;
  gap: 6px;
}
.web {
  gap: 10px;
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
.vscode summary {
  cursor: pointer;
  color: var(--text-2);
}
.vscode p {
  margin: 6px 0;
}
.failure pre {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: #0f1117;
  color: #fca5a5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}
.warn-text {
  color: var(--warning, #b45309);
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
}
.tpl p {
  margin: 0;
}
.tpl.off {
  opacity: 0.6;
}
.history ul {
  margin: 8px 0 0;
  padding-left: 18px;
}
.log {
  margin: 0;
  max-height: 60vh;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: #0f1117;
  color: #c9ced8;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
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
</style>
