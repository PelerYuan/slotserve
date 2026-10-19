<script setup lang="ts">
import { computed, h, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NDropdown, NInput, NModal, NSwitch, useDialog, useMessage } from 'naive-ui'
import { MoreHorizontal, Plus, Search, ShieldCheck } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AdminUser } from '@/api/types'
import { session } from '@/composables/session'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatCompact, formatDate, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()

const users = ref<AdminUser[]>([])
const query = ref('')
const loaded = ref(false)

async function load() {
  try {
    users.value = await api.admin.users()
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(load)

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  return q ? users.value.filter((u) => u.username.toLowerCase().includes(q)) : users.value
})

const isSelf = (u: AdminUser) => u.id === session.user?.id

function randomPassword() {
  const alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  const bytes = crypto.getRandomValues(new Uint8Array(12))
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('')
}

// ---- create ---------------------------------------------------------------
const createForm = reactive({ open: false, username: '', password: '', isAdmin: false, busy: false })

function openCreate() {
  Object.assign(createForm, { open: true, username: '', password: randomPassword(), isAdmin: false })
}

async function submitCreate() {
  createForm.busy = true
  try {
    await api.admin.createUser(createForm.username.trim(), createForm.password, createForm.isAdmin)
    message.success(t('admin.users.created'))
    createForm.open = false
    await load()
  } catch (e) {
    showError(e)
  } finally {
    createForm.busy = false
  }
}

// ---- reset password -------------------------------------------------------
const resetForm = reactive({ user: null as AdminUser | null, password: '', busy: false })

async function submitReset() {
  if (!resetForm.user) return
  resetForm.busy = true
  try {
    await api.admin.resetPassword(resetForm.user.id, resetForm.password)
    message.success(t('admin.users.passwordReset'))
    resetForm.user = null
  } catch (e) {
    showError(e)
  } finally {
    resetForm.busy = false
  }
}

// ---- delete ---------------------------------------------------------------
const deleteForm = reactive({ user: null as AdminUser | null, confirm: '', busy: false })

async function submitDelete() {
  if (!deleteForm.user) return
  deleteForm.busy = true
  try {
    await api.admin.deleteUser(deleteForm.user.id)
    message.success(t('admin.users.deleted'))
    deleteForm.user = null
    await load()
  } catch (e) {
    showError(e)
  } finally {
    deleteForm.busy = false
  }
}

// ---- row actions ----------------------------------------------------------
async function patch(u: AdminUser, body: { is_admin?: boolean; disabled?: boolean }) {
  try {
    const updated = await api.admin.updateUser(u.id, body)
    users.value = users.value.map((x) => (x.id === u.id ? updated : x))
    message.success(t('admin.users.updated'))
  } catch (e) {
    showError(e)
  }
}

function actionsFor(u: AdminUser) {
  const self = isSelf(u)
  return [
    { key: 'admin', label: u.is_admin ? t('admin.users.revokeAdmin') : t('admin.users.makeAdmin'), disabled: self },
    { key: 'password', label: t('admin.users.resetPassword') },
    { type: 'divider', key: 'd' },
    {
      key: 'disable',
      label: u.disabled_at ? t('admin.users.enable') : t('admin.users.disable'),
      disabled: self,
    },
    {
      key: 'delete',
      label: () => h('span', { style: self ? undefined : 'color: var(--danger)' }, t('admin.users.delete')),
      disabled: self,
    },
  ]
}

function onAction(key: string, u: AdminUser) {
  if (key === 'admin') patch(u, { is_admin: !u.is_admin })
  else if (key === 'password') Object.assign(resetForm, { user: u, password: randomPassword() })
  else if (key === 'delete') Object.assign(deleteForm, { user: u, confirm: '' })
  else if (key === 'disable') {
    if (u.disabled_at) patch(u, { disabled: false })
    else
      dialog.warning({
        title: t('admin.users.disable'),
        content: t('admin.users.disableConfirm', { name: u.username }),
        positiveText: t('admin.users.disable'),
        negativeText: t('common.cancel'),
        onPositiveClick: () => patch(u, { disabled: true }),
      })
  }
}

const usernameOk = computed(() => /^[A-Za-z0-9_.-]{3,32}$/.test(createForm.username.trim()))
</script>

<template>
  <div class="toolbar">
    <NInput v-model:value="query" :placeholder="t('admin.users.search')" clearable class="search">
      <template #prefix><Search :size="15" class="faint" /></template>
    </NInput>
    <NButton type="primary" @click="openCreate">
      <template #icon><Plus :size="16" /></template>
      {{ t('admin.users.create') }}
    </NButton>
  </div>

  <section class="card">
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('admin.users.user') }}</th>
            <th>{{ t('admin.users.status') }}</th>
            <th>{{ t('admin.users.lastActive') }}</th>
            <th class="right">{{ t('admin.users.usage30d') }}</th>
            <th class="right">{{ t('admin.users.keys') }}</th>
            <th class="right">{{ t('admin.users.upcoming') }}</th>
            <th>{{ t('admin.users.joined') }}</th>
            <th />
          </tr>
        </thead>
        <TransitionGroup tag="tbody" name="row">
          <tr v-for="u in filtered" :key="u.id" :class="{ off: u.disabled_at }">
            <td>
              <div class="who">
                <span class="avatar">{{ u.username.slice(0, 1).toUpperCase() }}</span>
                <span class="name">{{ u.username }}</span>
                <span v-if="isSelf(u)" class="faint small">({{ t('common.you') }})</span>
                <span v-if="u.is_admin" class="badge brand">
                  <ShieldCheck :size="12" :stroke-width="2.2" />{{ t('common.admin') }}
                </span>
              </div>
            </td>
            <td>
              <span v-if="u.disabled_at" class="badge danger">{{ t('admin.users.disabled') }}</span>
              <span v-else class="badge ok">{{ t('admin.users.active') }}</span>
            </td>
            <td class="muted">{{ u.last_active_at ? relativeFromNow(u.last_active_at) : t('admin.users.never') }}</td>
            <td class="right num">
              <div>{{ formatCompact(u.tokens_30d) }} tokens</div>
              <div class="faint small">{{ t('admin.users.requests', { n: u.requests_30d }) }}</div>
            </td>
            <td class="right num">{{ u.key_count }}</td>
            <td class="right num">{{ u.upcoming_reservations }}</td>
            <td class="muted num">{{ formatDate(u.created_at) }}</td>
            <td class="right">
              <NDropdown trigger="click" :options="actionsFor(u)" placement="bottom-end" @select="(k: string) => onAction(k, u)">
                <NButton quaternary size="small" circle>
                  <template #icon><MoreHorizontal :size="16" /></template>
                </NButton>
              </NDropdown>
            </td>
          </tr>
        </TransitionGroup>
      </table>
    </div>
    <div v-if="loaded && !filtered.length" class="empty">—</div>
  </section>

  <!-- create -->
  <NModal
    v-model:show="createForm.open"
    preset="card"
    :title="t('admin.users.createTitle')"
    :bordered="false"
    style="width: 440px; max-width: calc(100vw - 32px)"
  >
    <p class="muted hint">{{ t('admin.users.createHint') }}</p>
    <div class="fields">
      <label>
        <span class="field-label">{{ t('auth.username') }}</span>
        <NInput v-model:value="createForm.username" :placeholder="t('auth.usernameRule')" autofocus />
      </label>
      <label>
        <span class="field-label">{{ t('auth.password') }}</span>
        <div class="row">
          <NInput v-model:value="createForm.password" class="mono-input" />
          <NButton secondary @click="createForm.password = randomPassword()">{{ t('admin.users.generate') }}</NButton>
        </div>
      </label>
      <label class="switch-row">
        <span>{{ t('admin.users.isAdmin') }}</span>
        <NSwitch v-model:value="createForm.isAdmin" />
      </label>
    </div>
    <template #footer>
      <div class="footer">
        <NButton @click="createForm.open = false">{{ t('common.cancel') }}</NButton>
        <NButton
          type="primary"
          :loading="createForm.busy"
          :disabled="!usernameOk || createForm.password.length < 6"
          @click="submitCreate"
        >
          {{ t('common.create') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <!-- reset password -->
  <NModal
    :show="!!resetForm.user"
    preset="card"
    :title="t('admin.users.resetTitle', { name: resetForm.user?.username })"
    :bordered="false"
    style="width: 440px; max-width: calc(100vw - 32px)"
    @update:show="(v: boolean) => !v && (resetForm.user = null)"
  >
    <p class="muted hint">{{ t('admin.users.resetHint') }}</p>
    <div class="row">
      <NInput v-model:value="resetForm.password" class="mono-input" />
      <NButton secondary @click="resetForm.password = randomPassword()">{{ t('admin.users.generate') }}</NButton>
    </div>
    <template #footer>
      <div class="footer">
        <NButton @click="resetForm.user = null">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="resetForm.busy" :disabled="resetForm.password.length < 6" @click="submitReset">
          {{ t('common.save') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <!-- delete -->
  <NModal
    :show="!!deleteForm.user"
    preset="card"
    :title="t('admin.users.deleteTitle', { name: deleteForm.user?.username })"
    :bordered="false"
    style="width: 460px; max-width: calc(100vw - 32px)"
    @update:show="(v: boolean) => !v && (deleteForm.user = null)"
  >
    <p class="muted hint">{{ t('admin.users.deleteHint') }}</p>
    <NInput v-model:value="deleteForm.confirm" :placeholder="deleteForm.user?.username" />
    <template #footer>
      <div class="footer">
        <NButton @click="deleteForm.user = null">{{ t('common.cancel') }}</NButton>
        <NButton
          type="error"
          :loading="deleteForm.busy"
          :disabled="deleteForm.confirm !== deleteForm.user?.username"
          @click="submitDelete"
        >
          {{ t('admin.users.delete') }}
        </NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-bottom: 12px;
}
.search {
  max-width: 280px;
}
.table td {
  white-space: nowrap;
}
.who {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}
.avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: 50%;
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 650;
  font-size: 12px;
}
.name {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
tr.off .who,
tr.off td.num {
  opacity: 0.55;
}
.hint {
  margin: 0 0 16px;
}
.fields {
  display: grid;
  gap: 14px;
}
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 6px;
}
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-2);
}
.mono-input :deep(input) {
  font-family: var(--mono);
}
.row .mono-input {
  flex: 1;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.row-enter-active,
.row-leave-active {
  transition: opacity 0.25s;
}
.row-enter-from,
.row-leave-to {
  opacity: 0;
}
</style>
