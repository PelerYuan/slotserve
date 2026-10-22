<script setup lang="ts">
// The ports of one dev environment: how each is opened (SlotServe login or a direct host port), whether something
// listens on it, and adding/removing ports while it runs.
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NInputNumber, NModal, NRadioButton, NRadioGroup, NTooltip, useDialog } from 'naive-ui'
import { Copy, ExternalLink, Plus, X } from 'lucide-vue-next'
import { ApiError } from '@/api/client'
import type { DevSession } from '@/api/types'
import { absolute, portsApi, type DevPort, type DevPortMode, type DevPorts } from '@/api/devenvPorts'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { devenvApi } from '@/features/devenv/api'
import { useWebOpen } from '@/features/devenv/useWebOpen'

const props = defineProps<{ session: DevSession; canManage: boolean }>()
const { t } = useI18n()
const dialog = useDialog()
const { showError } = useErrorMessage()
const web = useWebOpen()

const data = ref<DevPorts | null>(null)
const busy = ref(false)
const shared = computed(() => props.session.kind === 'shared')

async function load() {
  try {
    data.value = await portsApi.list(props.session.id)
  } catch (e) {
    if (!data.value) showError(e)
  }
}

let timer: number | undefined
onMounted(() => {
  load()
  timer = window.setInterval(load, 8000)
})
onBeforeUnmount(() => window.clearInterval(timer))
watch(() => props.session.state, load)

// Apps without a login of their own (VS Code, desktop, JupyterLab: SlotServe's login protects them) need a
// confirmation before they go direct: the server answers 400 devenv_port_unauthenticated until it is given.
async function putPort(port: number, body: { name?: string; mode: DevPortMode }): Promise<boolean> {
  busy.value = true
  try {
    data.value = await devenvApi.putPort(props.session.id, port, body)
    return true
  } catch (e) {
    if (e instanceof ApiError && e.code === 'devenv_port_unauthenticated') {
      return confirmUnauthenticated(port, body, String(e.params.kind ?? ''))
    }
    showError(e)
    return false
  } finally {
    busy.value = false
  }
}
function confirmUnauthenticated(port: number, body: { name?: string; mode: DevPortMode }, kind: string): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.warning({
      title: t('devenvUi.ports.unauthTitle', { port }),
      content: t('devenvUi.ports.unauthBody', {
        app: kind && ['vscode', 'desktop', 'jupyter'].includes(kind) ? t(`devenv.web.kind.${kind}`) : t('devenvUi.ports.thisApp'),
      }),
      positiveText: t('devenvUi.ports.unauthConfirm'),
      negativeText: t('common.cancel'),
      onPositiveClick: async () => {
        busy.value = true
        try {
          data.value = await devenvApi.putPort(props.session.id, port, { ...body, confirm_unauthenticated: true })
          resolve(true)
        } catch (err) {
          showError(err)
          resolve(false)
        } finally {
          busy.value = false
        }
      },
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
      onMaskClick: () => resolve(false),
    })
  })
}

async function setMode(p: DevPort, mode: DevPortMode) {
  await putPort(p.port, { mode })
}

function remove(p: DevPort) {
  dialog.warning({
    title: p.declared ? t('devenv.ports.reset') : t('devenv.ports.remove'),
    content: p.declared ? t('devenv.ports.resetConfirm', { port: p.port }) : t('devenv.ports.removeConfirm', { port: p.port }),
    positiveText: p.declared ? t('devenv.ports.reset') : t('devenv.ports.remove'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        data.value = await portsApi.remove(props.session.id, p.port)
      } catch (e) {
        showError(e)
      }
    },
  })
}

const form = reactive({ open: false, port: null as number | null, name: '', mode: 'login' as DevPortMode })
function openAdd(port?: number) {
  Object.assign(form, { open: true, port: port ?? null, name: '', mode: 'login' })
}
async function add() {
  if (!form.port) return
  if (await putPort(form.port, { name: form.name.trim(), mode: form.mode })) form.open = false
}

const copied = ref<string | null>(null)
async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // http LAN origins have no clipboard API
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
  }
  copied.value = text
  window.setTimeout(() => (copied.value = null), 1500)
}

function address(p: DevPort): string {
  return p.mode === 'direct' && p.direct_url ? p.direct_url : absolute(p.url)
}
const statusTone: Record<string, string> = { active: 'ok', starting: 'warn', error: 'danger', off: '', paused: '', login: 'brand' }
const listenTone: Record<string, string> = { all: 'ok', local: 'warn', none: '' }
// codes (devenv_public_port_limit, ...) are translated; Docker's messages are shown as they are
function errorText(error: string): string {
  return /^[a-z_]+$/.test(error) ? t(`errors.${error}`) : error
}
const running = computed(() => props.session.state === 'running')
</script>

<template>
  <div v-if="data && (data.ports.length || (canManage && data.can_add && !shared))" class="ports">
    <div class="row head">
      <span class="faint small">{{ t('devenv.ports.title') }}</span>
      <span class="spacer" />
      <NButton v-if="canManage && data.can_add && !shared" size="tiny" secondary @click="openAdd()">
        <template #icon><Plus :size="13" /></template>
        {{ t('devenv.ports.add') }}
      </NButton>
    </div>
    <div v-for="p in data.ports" :key="p.port" class="port" :data-port="p.port">
      <div class="row wrap line">
        <span class="strong">{{ p.name }}</span>
        <span class="mono small faint">:{{ p.port }}</span>
        <span class="badge" :class="statusTone[p.status]">{{ t(`devenv.ports.status.${p.status}`) }}</span>
        <NTooltip v-if="running && p.listening">
          <template #trigger>
            <span class="badge" :class="listenTone[p.listening]">{{ t(`devenv.ports.listening.${p.listening}`) }}</span>
          </template>
          {{ t(`devenv.ports.listeningHint.${p.listening}`) }}
        </NTooltip>
        <span class="spacer" />
        <NRadioGroup
          v-if="canManage && !shared"
          size="small"
          :value="p.mode"
          :disabled="busy"
          @update:value="(m: DevPortMode) => setMode(p, m)"
        >
          <NRadioButton value="login">{{ t('devenv.ports.mode.login') }}</NRadioButton>
          <NRadioButton value="direct" :disabled="!data.enabled || (!data.can_direct && p.mode !== 'direct')">
            {{ t('devenv.ports.mode.direct') }}
          </NRadioButton>
        </NRadioGroup>
        <span v-else class="badge">{{ t(`devenv.ports.mode.${p.mode}`) }}</span>
        <NButton
          v-if="canManage && !shared && (!p.declared || p.id !== null)"
          size="tiny"
          quaternary
          :title="p.declared ? t('devenv.ports.reset') : t('devenv.ports.remove')"
          @click="remove(p)"
        >
          <template #icon><X :size="13" /></template>
        </NButton>
      </div>
      <div v-if="running || p.mode === 'direct'" class="row wrap line">
        <span class="mono small addr">{{ address(p) }}</span>
        <NButton
          v-if="running && p.mode === 'login'"
          size="tiny"
          quaternary
          data-test="port-open"
          @click="web.open(session.id, p.port)"
        >
          <template #icon><ExternalLink :size="13" /></template>
          {{ t('devenv.ports.open') }}
        </NButton>
        <NButton
          v-else-if="running && p.status === 'active' && p.direct_url"
          tag="a"
          :href="p.direct_url"
          target="_blank"
          rel="noopener"
          size="tiny"
          quaternary
        >
          <template #icon><ExternalLink :size="13" /></template>
          {{ t('devenv.ports.open') }}
        </NButton>
        <NButton size="tiny" quaternary @click="copy(address(p))">
          <template #icon><Copy :size="13" /></template>
          {{ copied === address(p) ? t('common.copied') : t('common.copy') }}
        </NButton>
      </div>
      <div v-if="p.error" class="small danger-text">{{ errorText(p.error) }}</div>
    </div>
    <div v-if="canManage && !shared && data.detected.length && data.can_add" class="row wrap detected small">
      <span class="faint">{{ t('devenv.ports.detected') }}</span>
      <NButton v-for="port in data.detected" :key="port" size="tiny" dashed @click="openAdd(port)">
        <template #icon><Plus :size="12" /></template>
        {{ port }}
      </NButton>
    </div>
    <p v-if="data.ports.some((p) => p.mode === 'direct')" class="faint small hint">{{ t('devenv.ports.directHint') }}</p>

    <NModal v-model:show="form.open" preset="card" :title="t('devenv.ports.add')" style="max-width: 460px">
      <div class="form">
        <label>
          <span class="field-label">{{ t('devenv.ports.port') }}</span>
          <NInputNumber v-model:value="form.port" :min="1" :max="65535" :show-button="false" placeholder="8000" />
        </label>
        <label>
          <span class="field-label">{{ t('devenv.ports.name') }}</span>
          <NInput v-model:value="form.name" maxlength="32" :placeholder="t('devenv.ports.namePlaceholder')" />
        </label>
        <div class="field">
          <span class="field-label">{{ t('devenv.ports.how') }}</span>
          <NRadioGroup v-model:value="form.mode">
            <NRadioButton value="login">{{ t('devenv.ports.mode.login') }}</NRadioButton>
            <NRadioButton value="direct" :disabled="!data.enabled || !data.can_direct">{{ t('devenv.ports.mode.direct') }}</NRadioButton>
          </NRadioGroup>
          <span class="faint small">{{ form.mode === 'direct' ? t('devenv.ports.directExplain') : t('devenv.ports.loginExplain') }}</span>
          <span v-if="!data.enabled" class="faint small">{{ t('devenv.ports.directOff') }}</span>
          <span v-else-if="!data.can_direct" class="faint small">
            {{ data.limit > 0 ? t('devenv.ports.directLimit', { n: data.limit }) : t('devenv.ports.directAdmins') }}
          </span>
        </div>
      </div>
      <template #footer>
        <div class="footer">
          <NButton @click="form.open = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="busy" :disabled="!form.port" @click="add">{{ t('devenv.ports.add') }}</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.ports {
  display: grid;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.head {
  gap: 8px;
}
.port {
  display: grid;
  gap: 2px;
  padding: 6px 0;
  border-top: 1px solid var(--border);
}
.line {
  gap: 8px;
  align-items: center;
}
.addr {
  word-break: break-all;
}
.detected {
  gap: 6px;
  align-items: center;
}
.hint {
  margin: 0;
}
.danger-text {
  color: var(--danger);
}
.form {
  display: grid;
  gap: 14px;
}
.form label,
.field {
  display: grid;
  gap: 6px;
}
.field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
