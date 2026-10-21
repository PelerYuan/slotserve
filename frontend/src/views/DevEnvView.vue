<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { NButton, NInput, NInputNumber, NModal, NSelect, NSwitch, useDialog, useMessage } from 'naive-ui'
import { ExternalLink, KeyRound, Plus, Terminal } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { DevInfo, DevSession, DevTemplate, DevWebKind, Reservation, SshKey } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, formatRange } from '@/utils/format'

const { t } = useI18n()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()
const isAdmin = computed(() => !!session.user?.is_admin)

const info = ref<DevInfo | null>(null)
const sessions = ref<DevSession[]>([])
const templates = ref<DevTemplate[]>([])
const keys = ref<SshKey[]>([])
const upcoming = ref<Reservation[]>([])
const everyone = ref(false)

const open = computed(() => sessions.value.filter((s) => s.state !== 'stopped'))
const history = computed(() => sessions.value.filter((s) => s.state === 'stopped').slice(0, 8))
const prepared = computed(() => new Set(sessions.value.map((s) => s.reservation_id)))
const notPrepared = computed(() => upcoming.value.filter((r) => !prepared.value.has(r.id)))
const shared = computed(() => templates.value.filter((x) => x.kind === 'builtin'))
const own = computed(() => templates.value.filter((x) => x.kind !== 'builtin'))

async function load() {
  try {
    const [i, s, tpl, k, mine] = await Promise.all([
      api.devenv.info(),
      api.devenv.sessions(everyone.value),
      api.devenv.templates(),
      api.sshKeys(),
      api.reservations(new Date(Date.now() - 3_600_000), new Date(Date.now() + 60 * 86_400_000), true),
    ])
    info.value = i
    sessions.value = s
    templates.value = tpl
    keys.value = k
    upcoming.value = mine.filter((r) => r.devenv_template_id && (r.phase === 'upcoming' || r.phase === 'pending' || r.phase === 'active'))
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
  return `Host slotserve-dev
  HostName ${info.value?.ssh_host ?? 'server'}
  Port ${s.ssh_port}
  User root`
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

// ---- SSH keys ------------------------------------------------------------------
const keyForm = reactive({ open: false, name: '', key: '' })
async function addKey() {
  busy.value = true
  try {
    await api.addSshKey(keyForm.name.trim(), keyForm.key.trim())
    Object.assign(keyForm, { open: false, name: '', key: '' })
    load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
function removeKey(k: SshKey) {
  dialog.warning({
    title: t('common.delete'),
    content: t('devenv.keyDeleteConfirm', { name: k.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      await api.deleteSshKey(k.id).catch(showError)
      load()
    },
  })
}

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

    <div v-if="info?.enabled && !keys.length" class="card card-pad notice warn">
      <KeyRound :size="16" />
      <span>{{ t('devenv.needKey') }}</span>
      <NButton size="small" type="primary" secondary @click="keyForm.open = true">{{ t('devenv.addKey') }}</NButton>
    </div>

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
          <span class="strong">{{ s.template_name ?? s.image }}</span>
          <span class="badge" :class="stateTone[s.state]">{{ t(`devenv.state.${s.state}`) }}</span>
          <span v-if="everyone" class="faint small">{{ s.username }}</span>
          <span class="spacer" />
          <span class="faint small num">
            <template v-if="s.start_at && s.end_at">{{ formatRange(s.start_at, s.end_at) }}</template>
            <template v-if="s.lane_name"> · {{ s.lane_name }}</template>
          </span>
        </div>
        <div class="mono small faint image">{{ s.image }}</div>

        <div v-if="s.state === 'pulling'" class="progress">
          <div class="bar"><span :style="{ width: `${s.progress ?? 3}%` }" /></div>
          <div class="faint small">
            {{ t('devenv.pulling', { pct: s.progress !== null ? Math.round(s.progress) : '…' }) }}
            <span v-if="s.detail" class="mono"> · {{ s.detail }}</span>
          </div>
        </div>
        <p v-else-if="s.state === 'ready' || s.state === 'waiting'" class="muted small">{{ t('devenv.waitingStart') }}</p>
        <p v-else-if="s.state === 'starting'" class="muted small">{{ t('devenv.starting') }}</p>

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
          <span class="strong">{{ r.devenv_template_name }}</span>
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
          <div v-if="isAdmin" class="row actions">
            <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
            <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
          </div>
        </article>
      </div>
      <h3 class="sub">{{ t('devenv.mine') }}</h3>
      <div v-if="!own.length" class="faint small">{{ t('devenv.mineEmpty', { n: info?.saved_limit ?? 3 }) }}</div>
      <div class="grid">
        <article v-for="x in own" :key="x.id" class="card card-pad tpl">
          <div class="row">
            <span class="strong">{{ x.name }}</span>
            <span class="badge" :class="{ brand: x.kind === 'saved' }">{{ t(`devenv.kind.${x.kind}`) }}</span>
            <span v-for="p in x.web_ports" :key="p.port" class="badge brand">{{ t(`devenv.web.kind.${p.kind}`) }}</span>
          </div>
          <div class="mono small faint image">{{ x.image }}</div>
          <p v-if="x.description" class="small muted">{{ x.description }}</p>
          <div class="row actions">
            <NButton size="tiny" quaternary @click="openTemplate(x)">{{ t('common.edit') }}</NButton>
            <NButton size="tiny" quaternary type="error" @click="removeTemplate(x)">{{ t('common.delete') }}</NButton>
          </div>
        </article>
      </div>
    </section>

    <!-- SSH keys -->
    <section class="block">
      <div class="block-head">
        <h2 class="block-title">{{ t('devenv.keys') }}</h2>
        <NButton size="small" secondary @click="keyForm.open = true">
          <template #icon><Plus :size="14" /></template>
          {{ t('devenv.addKey') }}
        </NButton>
      </div>
      <div v-if="!keys.length" class="faint small">{{ t('devenv.noKeys') }}</div>
      <div v-else class="card">
        <div v-for="k in keys" :key="k.id" class="key-row">
          <KeyRound :size="15" class="faint" />
          <span class="strong">{{ k.name }}</span>
          <span class="faint small">{{ k.key_type }}</span>
          <span class="mono small faint fp">{{ k.fingerprint }}</span>
          <span class="spacer" />
          <NButton size="tiny" quaternary type="error" @click="removeKey(k)">{{ t('common.delete') }}</NButton>
        </div>
      </div>
    </section>

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

    <NModal v-model:show="keyForm.open" preset="card" :title="t('devenv.addKey')" style="max-width: 560px">
      <div class="form">
        <label>
          <span class="field-label">{{ t('devenv.keyName') }}</span>
          <NInput v-model:value="keyForm.name" maxlength="64" :placeholder="t('devenv.keyNamePlaceholder')" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.publicKey') }}</span>
          <NInput v-model:value="keyForm.key" type="textarea" class="mono" placeholder="ssh-ed25519 AAAA… you@laptop" :autosize="{ minRows: 3, maxRows: 6 }" />
          <span class="faint small">{{ t('devenv.publicKeyHint') }}</span>
        </label>
      </div>
      <template #footer>
        <div class="footer">
          <NButton @click="keyForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!keyForm.key.trim()" @click="addKey">{{ t('common.save') }}</NButton>
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
.key-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  min-width: 0;
}
.key-row:last-child {
  border-bottom: 0;
}
.fp {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
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
@media (max-width: 640px) {
  .fp {
    display: none;
  }
}
</style>
