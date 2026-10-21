<script setup lang="ts">
// Admin -> Storage: host directories, NFS exports and SMB shares for shared data, personal spaces,
// kept project data and model libraries (backend: services/storage.py).
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, useDialog, useMessage } from 'naive-ui'
import { HardDrive, Plus, RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AdminUser } from '@/api/types'
import {
  storageApi,
  type Storage,
  type StorageAccess,
  type StorageInput,
  type StorageKind,
  type StoragePurpose,
  type StorageUsage,
} from '@/api/storage'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatBytes, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()

const rows = ref<Storage[]>([])
const users = ref<AdminUser[]>([])
const nodes = ref<{ id: number; name: string }[]>([]) // other machines (a host directory may exist there too)
const loading = ref(false)
const checking = ref<Set<number>>(new Set())

async function load() {
  loading.value = true
  try {
    rows.value = await storageApi.list()
  } catch (e) {
    showError(e)
  } finally {
    loading.value = false
  }
}
onMounted(async () => {
  await load()
  users.value = await api.admin.users().catch(() => [])
  nodes.value = (await api.admin.nodes().catch(() => [])).filter((n) => !n.is_local)
})
const nodeOptions = computed(() => nodes.value.map((n) => ({ label: n.name, value: n.id })))

const KINDS: StorageKind[] = ['host', 'nfs', 'smb']
const PURPOSES: StoragePurpose[] = ['shared', 'personal', 'projects', 'models']
const ACCESS: StorageAccess[] = ['everyone', 'users', 'admins']
const kindOptions = computed(() => KINDS.map((k) => ({ label: t(`storage.kind.${k}`), value: k })))
const purposeOptions = computed(() => PURPOSES.map((p) => ({ label: t(`storage.purpose.${p}`), value: p })))
const accessOptions = computed(() => ACCESS.map((a) => ({ label: t(`storage.access.${a}`), value: a })))
const userOptions = computed(() =>
  users.value.filter((u) => !u.disabled_at).map((u) => ({ label: u.username, value: u.id })),
)

const form = reactive({
  open: false,
  id: null as number | null,
  name: '',
  kind: 'host' as StorageKind,
  purpose: 'shared' as StoragePurpose,
  host_path: '',
  server: '',
  remote_path: '',
  options: '',
  username: '',
  password: '',
  hasPassword: false,
  clearPassword: false,
  mount_path: '',
  read_only: false,
  access: 'everyone' as StorageAccess,
  user_ids: [] as number[],
  local_path: '',
  quota_gb: null as number | null,
  enabled: true,
  node_ids: [] as number[],
})
const busy = ref(false)

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'data'

function openNew() {
  Object.assign(form, {
    open: true,
    id: null,
    name: '',
    kind: 'host',
    purpose: 'shared',
    host_path: '',
    server: '',
    remote_path: '',
    options: '',
    username: '',
    password: '',
    hasPassword: false,
    clearPassword: false,
    mount_path: '',
    read_only: false,
    access: 'everyone',
    user_ids: [],
    local_path: '',
    quota_gb: null,
    enabled: true,
    node_ids: [],
  })
}

function openEdit(s: Storage) {
  Object.assign(form, {
    open: true,
    id: s.id,
    name: s.name,
    kind: s.kind,
    purpose: s.purpose,
    host_path: s.host_path ?? '',
    server: s.server ?? '',
    remote_path: s.remote_path ?? '',
    options: s.options ?? '',
    username: s.username ?? '',
    password: '',
    hasPassword: s.has_password,
    clearPassword: false,
    mount_path: s.mount_path ?? '',
    read_only: s.read_only,
    access: s.access,
    user_ids: [...s.user_ids],
    local_path: s.local_path ?? '',
    quota_gb: s.quota_gb,
    enabled: s.enabled,
    node_ids: [...(s.node_ids ?? [])],
  })
}

const needsMount = computed(() => form.purpose === 'shared' || form.purpose === 'models')
const mountPlaceholder = computed(() =>
  form.purpose === 'models' ? `/mnt/models/${slug(form.name)}` : `/data/${slug(form.name)}`,
)
const valid = computed(() => {
  if (!form.name.trim()) return false
  if (form.kind === 'host' && !form.host_path.trim()) return false
  if (form.kind !== 'host' && (!form.remote_path.trim() || (!form.server.trim() && !form.remote_path.startsWith('//'))))
    return false
  if (form.purpose === 'shared' && !form.mount_path.trim()) return false
  if (form.purpose === 'models' && form.kind !== 'host' && !form.local_path.trim()) return false
  if (form.access === 'users' && !form.user_ids.length) return false
  return true
})

function body(): StorageInput {
  const b: StorageInput = {
    name: form.name.trim(),
    kind: form.kind,
    purpose: form.purpose,
    read_only: form.read_only,
    access: form.access,
    user_ids: form.access === 'users' ? form.user_ids : [],
    enabled: form.enabled,
  }
  if (form.kind === 'host') {
    b.host_path = form.host_path.trim()
    b.node_ids = form.node_ids
  }
  else {
    b.server = form.server.trim() || null
    b.remote_path = form.remote_path.trim()
    b.options = form.options.trim() || null
  }
  if (form.kind === 'smb') {
    b.username = form.username.trim() || null
    if (form.password) b.password = form.password
    else if (form.clearPassword) b.password = ''
  }
  if (needsMount.value) b.mount_path = form.mount_path.trim() || null
  if (form.purpose === 'models') b.local_path = form.local_path.trim() || null
  if (form.purpose === 'personal') b.quota_gb = form.quota_gb || null
  return b
}

async function save() {
  busy.value = true
  try {
    const saved = form.id === null ? await storageApi.create(body()) : await storageApi.update(form.id, body())
    form.open = false
    message.success(t('storage.saved'))
    await load()
    if (saved.enabled && !saved.status) void check(saved)
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

async function check(s: Storage) {
  checking.value = new Set([...checking.value, s.id])
  try {
    const updated = await storageApi.check(s.id)
    rows.value = rows.value.map((r) => (r.id === updated.id ? updated : r))
    if (updated.status?.ok) message.success(t('storage.checkOk', { name: s.name }))
    else message.error(t('storage.checkFailed', { name: s.name }))
  } catch (e) {
    showError(e)
  } finally {
    const next = new Set(checking.value)
    next.delete(s.id)
    checking.value = next
  }
}

function remove(s: Storage) {
  dialog.warning({
    title: t('storage.delete'),
    content: t('storage.deleteConfirm', { name: s.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await storageApi.remove(s.id)
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

const usage = reactive({ open: false, name: '', quota: null as number | null, rows: [] as StorageUsage[] })
async function openUsage(s: Storage) {
  try {
    Object.assign(usage, { open: true, name: s.name, quota: s.quota_gb, rows: await storageApi.usage(s.id) })
  } catch (e) {
    showError(e)
  }
}

function accessText(s: Storage) {
  if (s.access === 'users') return s.usernames.join(', ') || '—'
  return t(`storage.access.${s.access}`)
}
function pct(s: Storage) {
  const st = s.status
  return st?.total ? Math.round(((st.used ?? 0) / st.total) * 100) : 0
}
</script>

<template>
  <p class="muted intro">{{ t('storage.intro') }}</p>

  <section class="card storages">
    <div class="card-header">
      <h3 class="card-title">{{ t('storage.title') }}</h3>
      <div class="row">
        <NButton size="small" quaternary :loading="loading" @click="load">
          <template #icon><RefreshCw :size="14" /></template>
        </NButton>
        <NButton size="small" type="primary" data-test="storage-add" @click="openNew">
          <template #icon><Plus :size="14" /></template>
          {{ t('storage.add') }}
        </NButton>
      </div>
    </div>
    <div v-if="!rows.length" class="empty">
      <HardDrive :size="18" class="faint" />
      <span>{{ t('storage.empty') }}</span>
    </div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('storage.name') }}</th>
            <th>{{ t('storage.purposeLabel') }}</th>
            <th>{{ t('storage.source') }}</th>
            <th>{{ t('storage.inEnvironments') }}</th>
            <th>{{ t('storage.accessLabel') }}</th>
            <th>{{ t('storage.state') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in rows" :key="s.id" :class="{ off: !s.enabled }">
            <td class="strong">
              {{ s.name }}
              <span v-if="!s.enabled" class="badge">{{ t('storage.disabled') }}</span>
            </td>
            <td class="small">{{ t(`storage.purpose.${s.purpose}`) }}</td>
            <td class="small">
              <span class="badge">{{ t(`storage.kind.${s.kind}`) }}</span>
              <span class="mono"> {{ s.source }}</span>
            </td>
            <td class="small">
              <span v-if="s.container_path" class="mono">{{ s.container_path }}</span>
              <span v-else-if="s.purpose === 'projects'" class="muted">{{ t('storage.keptDirs') }}</span>
              <span v-else class="muted">—</span>
              <span v-if="s.read_only" class="badge"> {{ t('storage.ro') }}</span>
              <div v-if="s.purpose === 'models' && s.local_path" class="faint mono">{{ t('storage.localShort') }} {{ s.local_path }}</div>
            </td>
            <td class="small">{{ accessText(s) }}</td>
            <td class="small state">
              <template v-if="s.status">
                <span :class="s.status.ok ? 'ok-text' : 'bad-text'" data-test="storage-state">
                  {{ s.status.ok ? t('storage.reachable') : t('storage.unreachable') }}
                </span>
                <template v-if="s.status.ok && s.status.total">
                  <div class="bar"><span :style="{ width: `${pct(s)}%` }" /></div>
                  <div class="faint">
                    {{ t('storage.space', { free: formatBytes(s.status.free), total: formatBytes(s.status.total) }) }}
                    <span v-if="s.status.writable === false" class="bad-text"> · {{ t('storage.notWritable') }}</span>
                  </div>
                </template>
                <div v-else-if="s.status.error" class="mono faint error-line" :title="s.status.error">{{ s.status.error }}</div>
                <div v-if="s.purpose === 'personal' && s.usage_total != null" class="faint">
                  {{ t('storage.usageSummary', { used: formatBytes(s.usage_total), n: s.usage_people }) }}
                  <span v-if="s.over_quota" class="bad-text"> · {{ t('storage.overQuota', { n: s.over_quota }) }}</span>
                </div>
                <div v-if="s.checked_at" class="faint">{{ t('storage.checked', { when: relativeFromNow(s.checked_at) }) }}</div>
              </template>
              <span v-else class="muted">{{ t('storage.notChecked') }}</span>
            </td>
            <td class="right nowrap">
              <NButton size="small" quaternary :loading="checking.has(s.id)" @click="check(s)">{{ t('storage.check') }}</NButton>
              <NButton v-if="s.purpose === 'personal'" size="small" quaternary @click="openUsage(s)">
                {{ t('storage.usage') }}
              </NButton>
              <NButton size="small" quaternary @click="openEdit(s)">{{ t('common.edit') }}</NButton>
              <NButton size="small" quaternary type="error" @click="remove(s)">{{ t('common.delete') }}</NButton>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
  <p class="faint small hint-block">{{ t('storage.footnote') }}</p>

  <NModal
    v-model:show="form.open"
    preset="card"
    :title="form.id === null ? t('storage.add') : t('storage.edit')"
    :bordered="false"
    style="width: 560px; max-width: calc(100vw - 32px)"
  >
    <div class="fields">
      <label>
        <span class="field-label">{{ t('storage.name') }}</span>
        <NInput v-model:value="form.name" maxlength="64" data-field="storage-name" />
      </label>
      <div class="two">
        <label>
          <span class="field-label">{{ t('storage.kindLabel') }}</span>
          <NSelect v-model:value="form.kind" :options="kindOptions" data-field="storage-kind" />
        </label>
        <label>
          <span class="field-label">{{ t('storage.purposeLabel') }}</span>
          <NSelect v-model:value="form.purpose" :options="purposeOptions" data-field="storage-purpose" />
        </label>
      </div>
      <p class="faint small hint">{{ t(`storage.purposeHint.${form.purpose}`) }}</p>

      <label v-if="form.kind === 'host'">
        <span class="field-label">{{ t('storage.hostPath') }}</span>
        <NInput v-model:value="form.host_path" placeholder="/srv/slotserve/data" class="mono" data-field="storage-host-path" />
        <p class="faint small hint">{{ t('storage.hostPathHint') }}</p>
      </label>
      <label v-if="form.kind === 'host' && nodeOptions.length">
        <span class="field-label">{{ t('storage.nodes') }}</span>
        <NSelect
          v-model:value="form.node_ids"
          :options="nodeOptions"
          multiple
          clearable
          :placeholder="t('storage.nodesNone')"
          data-field="storage-nodes"
        />
        <p class="faint small hint">{{ t('storage.nodesHint') }}</p>
      </label>
      <template v-else>
        <div class="two">
          <label>
            <span class="field-label">{{ t('storage.server') }}</span>
            <NInput v-model:value="form.server" placeholder="nas.local" class="mono" />
          </label>
          <label>
            <span class="field-label">{{ form.kind === 'nfs' ? t('storage.export') : t('storage.share') }}</span>
            <NInput
              v-model:value="form.remote_path"
              :placeholder="form.kind === 'nfs' ? '/export/data' : 'share/folder'"
              class="mono"
            />
          </label>
        </div>
        <template v-if="form.kind === 'smb'">
          <div class="two">
            <label>
              <span class="field-label">{{ t('storage.username') }}</span>
              <NInput v-model:value="form.username" autocomplete="off" />
            </label>
            <label>
              <span class="field-label">{{ t('storage.password') }}</span>
              <NInput
                v-model:value="form.password"
                type="password"
                show-password-on="click"
                autocomplete="new-password"
                :placeholder="form.hasPassword ? t('storage.passwordKept') : ''"
              />
            </label>
          </div>
          <NCheckbox v-if="form.hasPassword && !form.password" v-model:checked="form.clearPassword">
            {{ t('storage.passwordClear') }}
          </NCheckbox>
        </template>
        <label>
          <span class="field-label">{{ t('storage.options') }}</span>
          <NInput v-model:value="form.options" :placeholder="form.kind === 'nfs' ? 'nfsvers=4' : 'vers=3.0'" class="mono" />
          <p class="faint small hint">{{ t(`storage.optionsHint.${form.kind}`) }}</p>
        </label>
      </template>

      <template v-if="needsMount">
        <div class="two">
          <label>
            <span class="field-label">{{ t('storage.mountPath') }}</span>
            <NInput v-model:value="form.mount_path" :placeholder="mountPlaceholder" class="mono" data-field="storage-mount" />
          </label>
          <div class="ro">
            <NCheckbox v-model:checked="form.read_only">{{ t('storage.readOnly') }}</NCheckbox>
          </div>
        </div>
        <p v-if="form.purpose === 'models'" class="faint small hint">{{ t('storage.mountOptional') }}</p>
      </template>
      <label v-if="form.purpose === 'models'">
        <span class="field-label">{{ t('storage.localPath') }}</span>
        <NInput v-model:value="form.local_path" :placeholder="form.kind === 'host' ? form.host_path : '/mnt/nas-models'" class="mono" />
        <p class="faint small hint">{{ t('storage.localPathHint') }}</p>
      </label>
      <label v-if="form.purpose === 'personal'">
        <span class="field-label">{{ t('storage.quota') }}</span>
        <NInputNumber v-model:value="form.quota_gb" :min="0" :show-button="false" clearable>
          <template #suffix>GB</template>
        </NInputNumber>
        <p class="faint small hint">{{ t('storage.quotaHint') }}</p>
      </label>

      <label>
        <span class="field-label">{{ t('storage.accessLabel') }}</span>
        <NSelect v-model:value="form.access" :options="accessOptions" />
      </label>
      <label v-if="form.access === 'users'">
        <span class="field-label">{{ t('storage.users') }}</span>
        <NSelect v-model:value="form.user_ids" :options="userOptions" multiple filterable />
      </label>
      <div class="row">
        <NSwitch v-model:value="form.enabled" size="small" />
        <span class="small">{{ t('storage.enabled') }}</span>
      </div>
    </div>
    <template #footer>
      <div class="footer">
        <NButton @click="form.open = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" :disabled="!valid" data-test="storage-save" @click="save">
          {{ t('common.save') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <NModal v-model:show="usage.open" preset="card" :title="t('storage.usageTitle', { name: usage.name })" style="max-width: 480px">
    <div v-if="!usage.rows.length" class="empty">{{ t('storage.usageEmpty') }}</div>
    <table v-else class="table">
      <tbody>
        <tr v-for="u in usage.rows" :key="u.user_id">
          <td>{{ u.username }}</td>
          <td class="num right" :class="{ 'bad-text': u.over }">{{ formatBytes(u.bytes) }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="usage.quota" class="faint small hint">{{ t('storage.quotaIs', { n: usage.quota }) }}</p>
  </NModal>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
}
.strong {
  font-weight: 600;
}
.small {
  font-size: 12.5px;
}
.nowrap {
  white-space: nowrap;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.empty {
  display: flex;
  align-items: center;
  gap: 8px;
}
tr.off td {
  opacity: 0.6;
}
.state {
  min-width: 160px;
}
.bar {
  height: 4px;
  border-radius: 2px;
  background: var(--track);
  margin: 4px 0 2px;
  max-width: 160px;
  overflow: hidden;
}
.bar span {
  display: block;
  height: 100%;
  background: var(--brand);
}
.error-line {
  max-width: 260px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ok-text {
  color: var(--ok-text);
}
.bad-text {
  color: var(--danger-text);
}
.fields {
  display: grid;
  gap: 14px;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.ro {
  display: flex;
  align-items: flex-end;
  padding-bottom: 6px;
}
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 6px;
}
.hint {
  margin: 4px 0 0;
}
.hint-block {
  margin: 10px 0 0;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
@media (max-width: 560px) {
  .two {
    grid-template-columns: 1fr;
  }
}
</style>
