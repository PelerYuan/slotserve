<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NModal, NSelect, NTab, NTabs, useDialog, useMessage } from 'naive-ui'
import { KeyRound, LockKeyhole, Plus } from 'lucide-vue-next'
import CodeBlock from '@/components/CodeBlock.vue'
import { api } from '@/api/client'
import type { SshKey } from '@/api/types'
import { session } from '@/composables/session'
import { useStatus } from '@/composables/useStatus'
import { playgroundApi, type KeyInfo } from '@/features/playground/api'
import { codeSamples, type CodeLang } from '@/features/playground/code'
import SuggestionCard from '@/features/suggestions/components/SuggestionCard.vue'
import { offerSuggestion } from '@/features/suggestions/useSuggestions'
import { useRouter } from 'vue-router'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'

// "Access credentials": API keys, plus SSH keys and the environment password when dev environments are on.
const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()

const router = useRouter()
const { status } = useStatus()
const keys = ref<KeyInfo[]>([])
const creating = ref(false)
const newName = ref('')
const newExpiry = ref<number>(0)
const busy = ref(false)
const created = ref<(KeyInfo & { key: string }) | null>(null)
const baseUrl = computed(() => `${window.location.origin}/v1`)
const devenv = computed(() => !!session.config?.devenv_enabled)

// Expiry of a new key (days; 0 = never). Keys only call models (scope llm).
const expiryOptions = computed(() => [
  { label: t('keys.expiryNever'), value: 0 },
  ...[30, 90, 365].map((d) => ({ label: t('keys.expiryDays', { n: d }), value: d })),
])

// Ready-to-run code with the new key filled in: it is shown only once.
const codeLang = ref<CodeLang>('python')
const createdCode = computed(() =>
  created.value
    ? codeSamples({
        baseUrl: baseUrl.value,
        key: created.value.key,
        model: status.value?.default_model ?? status.value?.runtime.model_name ?? 'default',
        prompt: 'Hello',
      })
    : null,
)

async function load() {
  keys.value = await playgroundApi.keys().catch((e) => (showError(e), keys.value))
}

// ---- SSH keys and environment password (dev environments) ----------------------
const sshKeys = ref<SshKey[]>([])
const password = ref<{ set: boolean; login_enabled: boolean } | null>(null)
async function loadDevenv() {
  if (!devenv.value) return
  try {
    const [k, p] = await Promise.all([api.sshKeys(), api.devenv.password()])
    sshKeys.value = k
    password.value = p
  } catch (e) {
    showError(e)
  }
}
onMounted(() => {
  load()
  loadDevenv()
})

// Shown again on request, when admins allow keeping a copy (Admin > System settings).
const revealed = ref<{ name: string; key: string } | null>(null)
async function reveal(k: KeyInfo) {
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
    created.value = await playgroundApi.createKey(newName.value.trim(), newExpiry.value || null)
    creating.value = false
    newName.value = ''
    newExpiry.value = 0
    await load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

function revoke(k: KeyInfo) {
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

function closeCreated() {
  created.value = null
  if (devenv.value) void offerSuggestion('key_created')
}

const keyForm = reactive({ open: false, name: '', key: '' })
async function addSshKey() {
  busy.value = true
  try {
    await api.addSshKey(keyForm.name.trim(), keyForm.key.trim())
    Object.assign(keyForm, { open: false, name: '', key: '' })
    loadDevenv()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
function removeSshKey(k: SshKey) {
  dialog.warning({
    title: t('common.delete'),
    content: t('devenv.keyDeleteConfirm', { name: k.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      await api.deleteSshKey(k.id).catch(showError)
      loadDevenv()
    },
  })
}

const pwForm = reactive({ open: false, password: '', repeat: '' })
const pwError = computed(() => {
  if (pwForm.password && pwForm.password.length < 8) return t('keys.minLength')
  if (pwForm.repeat && pwForm.repeat !== pwForm.password) return t('keys.mismatch')
  return ''
})
function openPassword() {
  Object.assign(pwForm, { open: true, password: '', repeat: '' })
}
async function savePassword() {
  if (pwError.value || pwForm.password !== pwForm.repeat) return
  busy.value = true
  try {
    await api.devenv.setPassword(pwForm.password)
    Object.assign(pwForm, { open: false, password: '', repeat: '' })
    message.success(t('keys.passwordSaved'))
    loadDevenv()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}
function clearPassword() {
  dialog.warning({
    title: t('keys.clearPassword'),
    content: t('keys.clearConfirm'),
    positiveText: t('keys.clearPassword'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await api.devenv.clearPassword()
        message.success(t('keys.passwordRemoved'))
        loadDevenv()
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
    </div>

    <!-- API keys -->
    <section class="block" data-section="api-keys">
      <div class="block-head">
        <div>
          <h2 class="block-title">{{ t('keys.apiKeys') }}</h2>
          <p class="muted small">{{ t('keys.apiKeysHint') }}</p>
        </div>
        <NButton type="primary" @click="creating = true">
          <template #icon><Plus :size="16" /></template>
          {{ t('keys.create') }}
        </NButton>
      </div>

      <SuggestionCard
        id="key_created"
        class="suggest"
        :text="t('suggestions.keyCreated')"
        :action="t('suggestions.openDevenv')"
        @action="router.push({ name: 'devenv' })"
      />

      <div class="card endpoint card-pad">
        <div class="label">{{ t('keys.endpoint') }}</div>
        <CodeBlock :code="baseUrl" inline />
      </div>

      <div class="card">
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
                <th>{{ t('keys.expires') }}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="k in keys" :key="k.id">
                <td class="strong">{{ k.name }}</td>
                <td><code class="prefix">{{ k.prefix }}••••••••</code></td>
                <td class="muted num">{{ formatDateTime(k.created_at) }}</td>
                <td class="muted">{{ k.last_used_at ? relativeFromNow(k.last_used_at) : t('keys.never') }}</td>
                <td class="muted num" :class="{ expired: k.expired }">
                  {{ k.expired ? t('keys.expired') : k.expires_at ? formatDateTime(k.expires_at) : t('keys.noExpiry') }}
                </td>
                <td class="right nowrap">
                  <NButton v-if="k.revealable" size="small" quaternary @click="reveal(k)">{{ t('keys.reveal') }}</NButton>
                  <NButton size="small" quaternary type="error" @click="revoke(k)">{{ t('keys.revoke') }}</NButton>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <template v-if="devenv">
      <!-- SSH public keys -->
      <section class="block" data-section="ssh-keys">
        <div class="block-head">
          <div>
            <h2 class="block-title">{{ t('keys.ssh') }}</h2>
            <p class="muted small">{{ t('keys.sshHint') }}</p>
          </div>
          <NButton secondary @click="keyForm.open = true">
            <template #icon><Plus :size="14" /></template>
            {{ t('devenv.addKey') }}
          </NButton>
        </div>
        <div v-if="!sshKeys.length" class="card card-pad faint small">{{ t('devenv.noKeys') }}</div>
        <div v-else class="card">
          <div v-for="k in sshKeys" :key="k.id" class="key-row">
            <KeyRound :size="15" class="faint" />
            <span class="strong">{{ k.name }}</span>
            <span class="faint small">{{ k.key_type }}</span>
            <span class="mono small faint fp">{{ k.fingerprint }}</span>
            <span class="spacer" />
            <NButton size="tiny" quaternary type="error" @click="removeSshKey(k)">{{ t('common.delete') }}</NButton>
          </div>
        </div>
      </section>

      <!-- Environment password -->
      <section class="block" data-section="env-password">
        <div class="block-head">
          <div>
            <h2 class="block-title">{{ t('keys.password') }}</h2>
            <p class="muted small">{{ t('keys.passwordHint') }}</p>
          </div>
        </div>
        <div class="card card-pad pw">
          <LockKeyhole :size="16" class="faint" />
          <span>{{ password?.set ? t('keys.passwordSet') : t('keys.passwordNotSet') }}</span>
          <span class="spacer" />
          <NButton size="small" secondary @click="openPassword">
            {{ password?.set ? t('keys.changePassword') : t('keys.setPassword') }}
          </NButton>
          <NButton v-if="password?.set" size="small" quaternary type="error" @click="clearPassword">
            {{ t('keys.clearPassword') }}
          </NButton>
        </div>
        <p v-if="password && !password.login_enabled" class="small warn-text">{{ t('keys.passwordOff') }}</p>
      </section>
    </template>

    <NModal v-model:show="creating" preset="card" :title="t('keys.create')" style="width: 440px; max-width: calc(100vw - 32px)" :bordered="false">
      <label class="key-name">
        <span class="field-label">{{ t('keys.nameLabel') }}</span>
        <NInput v-model:value="newName" :placeholder="t('keys.namePlaceholder')" maxlength="64" autofocus @keyup.enter="create" />
      </label>
      <label class="key-name">
        <span class="field-label">{{ t('keys.expiry') }}</span>
        <NSelect v-model:value="newExpiry" :options="expiryOptions" :placeholder="t('keys.expiry')" data-field="key-expiry" />
      </label>
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
      @update:show="(v: boolean) => !v && closeCreated()"
    >
      <p class="muted hint">{{ created?.revealable ? t('keys.createdHintRevealable') : t('keys.createdHint') }}</p>
      <CodeBlock v-if="created" :code="created.key" inline />
      <template v-if="createdCode">
        <div class="label run-label">{{ t('keys.createdCode') }}</div>
        <NTabs v-model:value="codeLang" type="line" size="small">
          <NTab name="python">Python</NTab>
          <NTab name="curl">cURL</NTab>
          <NTab name="js">JavaScript</NTab>
        </NTabs>
        <CodeBlock :code="createdCode[codeLang]" data-field="created-key-code" />
      </template>
      <template #footer>
        <div class="footer">
          <NButton type="primary" @click="closeCreated">{{ t('common.close') }}</NButton>
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
          <NButton type="primary" :loading="busy" :disabled="!keyForm.key.trim()" @click="addSshKey">{{ t('common.save') }}</NButton>
        </div>
      </template>
    </NModal>

    <NModal v-model:show="pwForm.open" preset="card" :title="password?.set ? t('keys.changePassword') : t('keys.setPassword')" style="max-width: 440px">
      <form class="form" @submit.prevent="savePassword">
        <input type="text" autocomplete="username" :value="session.user?.username" hidden />
        <label>
          <span class="field-label">{{ t('keys.newPassword') }}</span>
          <NInput v-model:value="pwForm.password" type="password" show-password-on="click" maxlength="128" :placeholder="t('keys.minLength')" :input-props="{ autocomplete: 'new-password' }" />
        </label>
        <label>
          <span class="field-label">{{ t('keys.repeatPassword') }}</span>
          <NInput v-model:value="pwForm.repeat" type="password" show-password-on="click" maxlength="128" :placeholder="t('ph.repeatPassword')" :input-props="{ autocomplete: 'new-password' }" />
        </label>
        <span v-if="pwError" class="small err">{{ pwError }}</span>
        <span v-else class="faint small">{{ t('keys.passwordHint') }}</span>
      </form>
      <template #footer>
        <div class="footer">
          <NButton @click="pwForm.open = false">{{ t('common.cancel') }}</NButton>
          <NButton
            type="primary"
            :loading="busy"
            :disabled="!!pwError || pwForm.password.length < 8 || pwForm.password !== pwForm.repeat"
            @click="savePassword"
          >
            {{ t('common.save') }}
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.nowrap {
  white-space: nowrap;
}
.block {
  margin-bottom: 28px;
}
.block-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.block-title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.block-head p {
  margin: 4px 0 0;
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
.key-row,
.pw {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.key-row {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
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
.warn-text {
  color: var(--warning, #b45309);
}
.err {
  color: var(--danger);
}
.form {
  display: grid;
  gap: 14px;
}
.form label {
  display: grid;
  gap: 6px;
}
.key-name {
  display: grid;
  gap: 6px;
}
.key-name + .key-name {
  margin-top: 14px;
}
.run-label {
  margin: 18px 0 2px;
}
.expired {
  color: var(--danger-text);
}
.suggest {
  margin-bottom: 12px;
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
@media (max-width: 640px) {
  .fp {
    display: none;
  }
  .block-head {
    flex-wrap: wrap;
  }
}
</style>
