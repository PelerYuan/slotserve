<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, NInput, NModal, NSelect, useDialog, useMessage } from 'naive-ui'
import { Cpu, Plus, Server } from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AdminUser, ClusterNode, ClusterNodeToken, NodeStatus } from '@/api/types'
import CodeBlock from '@/components/CodeBlock.vue'
import MachineDrawer from '@/components/machines/MachineDrawer.vue'
import SharingPanel from '@/components/sharing/SharingPanel.vue'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDateTime, relativeFromNow } from '@/utils/format'

const { t } = useI18n()
const dialog = useDialog()
const message = useMessage()
const { showError } = useErrorMessage()

const nodes = ref<ClusterNode[]>([])
const loaded = ref(false)
let timer: number | undefined

async function load() {
  try {
    nodes.value = await api.admin.nodes()
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(() => {
  load()
  timer = window.setInterval(load, 5000)
})
onBeforeUnmount(() => window.clearInterval(timer))

const STATUS_CLASS: Record<NodeStatus, string> = {
  online: 'ok',
  offline: 'danger',
  never: '',
  revoked: '',
  mismatch: 'warn',
}

// the same machine card everyone sees on the Machines page
const openId = ref<number | null>(null)

const gb = (mb: number | null) => (mb == null ? '—' : `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB`)

// ---- add / edit -----------------------------------------------------------------

const form = reactive({ open: false, id: null as number | null, name: '', address: '', owner: null as number | null })
const users = ref<AdminUser[]>([])
const ownerOptions = computed(() => [
  { label: t('sharing.noOwner'), value: -1 },
  ...users.value.map((u) => ({ label: u.username, value: u.id })),
])
async function loadUsers() {
  if (!users.value.length) users.value = await api.admin.users().catch(() => [])
}
// Owner controls of one machine (sharing hours, pause, what runs, Wake-on-LAN).
const sharingNode = ref<ClusterNode | null>(null)
const busy = ref(false)
const issued = ref<ClusterNodeToken | null>(null)

function openNew() {
  Object.assign(form, { open: true, id: null, name: '', address: '', owner: null })
}
function openEdit(n: ClusterNode) {
  Object.assign(form, { open: true, id: n.id, name: n.name, address: n.address, owner: n.owner_id ?? -1 })
  loadUsers()
}

async function save() {
  busy.value = true
  try {
    if (form.id === null) {
      issued.value = await api.admin.createNode({ name: form.name.trim(), address: form.address.trim() })
    } else {
      await api.admin.updateNode(form.id, {
        name: form.name.trim(),
        address: form.address.trim(),
        owner_id: form.owner === -1 ? null : form.owner,
      })
      message.success(t('cluster.saved'))
    }
    form.open = false
    await load()
  } catch (e) {
    showError(e)
  } finally {
    busy.value = false
  }
}

function confirm(title: string, content: string, action: () => Promise<void>, positive = title) {
  dialog.warning({
    title,
    content,
    positiveText: positive,
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await action()
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

const rotate = (n: ClusterNode) =>
  confirm(t('cluster.newToken'), t('cluster.newTokenConfirm', { name: n.name }), async () => {
    issued.value = await api.admin.rotateNodeToken(n.id)
  })
const revoke = (n: ClusterNode) =>
  confirm(t('cluster.revoke'), t('cluster.revokeConfirm', { name: n.name }), async () => {
    await api.admin.revokeNode(n.id)
  })
const remove = (n: ClusterNode) =>
  confirm(t('cluster.delete'), t('cluster.deleteConfirm', { name: n.name }), async () => {
    await api.admin.deleteNode(n.id)
  })

async function scan(n: ClusterNode) {
  try {
    const r = await api.admin.scanNode(n.id)
    message.success(t('cluster.scanned', { added: r.added.length, known: r.skipped }))
    await load()
  } catch (e) {
    showError(e)
  }
}
</script>

<template>
  <p class="muted intro">{{ t('cluster.intro') }}</p>

  <section class="card">
    <div class="card-header">
      <h3 class="card-title">{{ t('cluster.title') }}</h3>
      <NButton size="small" type="primary" data-action="add-node" @click="openNew">
        <template #icon><Plus :size="14" /></template>
        {{ t('cluster.add') }}
      </NButton>
    </div>
    <div v-if="loaded && !nodes.length" class="empty">{{ t('cluster.empty') }}</div>
    <div v-else class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>{{ t('cluster.name') }}</th>
            <th>{{ t('cluster.status') }}</th>
            <th>{{ t('cluster.machine') }}</th>
            <th>GPU</th>
            <th>{{ t('cluster.lanes') }}</th>
            <th>{{ t('cluster.version') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <template v-for="n in nodes" :key="n.id">
            <tr :data-node="n.name">
              <td>
                <div class="strong"><Server :size="14" class="faint" /> {{ n.name }}</div>
                <div v-if="n.is_local" class="faint small">{{ t('cluster.builtIn') }}</div>
                <div v-else class="faint small mono">{{ n.effective_address || '—' }}</div>
              </td>
              <td>
                <span class="badge" :class="STATUS_CLASS[n.status]">{{ t(`cluster.statuses.${n.status}`) }}</span>
                <div v-if="!n.is_local && n.last_seen_at" class="faint small" :title="formatDateTime(n.last_seen_at)">
                  {{ t('cluster.lastSeen', { when: relativeFromNow(n.last_seen_at) }) }}
                </div>
              </td>
              <td class="small">
                <div>{{ n.hostname ?? '—' }}<span v-if="n.platform" class="faint"> · {{ n.platform }}</span></div>
                <div v-if="n.owner_name" class="owner">
                  <span class="badge brand">{{ t('sharing.owner') }}: {{ n.owner_name }}</span>
                  <span v-if="n.sharing_paused" class="badge warn">{{ t('sharing.paused') }}</span>
                  <span v-else-if="n.shared_now === false" class="badge">{{ t('sharing.notShared') }}</span>
                </div>
                <div class="faint">
                  <span v-if="n.cpu_cores">{{ t('cluster.cores', { n: n.cpu_cores }) }}</span>
                  <span v-if="n.cpu_percent != null"> · {{ n.cpu_percent.toFixed(0) }}%</span>
                  <span v-if="n.memory_total_mb">
                    · {{ n.memory_used_mb != null ? `${gb(n.memory_used_mb)} / ` : '' }}{{ gb(n.memory_total_mb) }}
                  </span>
                </div>
                <div v-if="n.models_dir" class="faint mono">{{ n.models_dir }} · {{ t('cluster.models', { n: n.model_count }) }}</div>
              </td>
              <td class="small">
                <div v-if="!n.gpus.length" class="faint">{{ t('cluster.noGpus') }}</div>
                <div v-for="g in n.gpus" :key="g.index" class="nowrap">
                  <Cpu :size="12" class="faint" /> {{ g.index }} · {{ g.name }}
                  <span class="faint">{{ gb(g.memory_total_mb) }}</span>
                </div>
              </td>
              <td class="small">
                <span v-if="!n.lanes.length" class="faint">—</span>
                <span v-for="l in n.lanes" :key="l.id" class="badge brand lane">{{ l.name }}</span>
              </td>
              <td class="small mono">
                <div>{{ n.agent_version ?? '—' }}</div>
                <div v-if="n.protocol_version != null" class="faint">{{ t('cluster.protocol', { n: n.protocol_version }) }}</div>
              </td>
              <td class="right nowrap">
                <NButton size="small" quaternary :disabled="n.status === 'revoked'" data-action="node-details" @click="openId = n.id">
                  {{ t('machines.details') }}
                </NButton>
                <NButton size="small" quaternary :disabled="n.status !== 'online'" @click="scan(n)">
                  {{ t('cluster.scan') }}
                </NButton>
                <NButton size="small" quaternary :data-sharing="n.name" @click="sharingNode = n">{{ t('sharing.manage') }}</NButton>
                <NButton size="small" quaternary @click="openEdit(n)">{{ t('common.edit') }}</NButton>
                <template v-if="!n.is_local">
                  <NButton size="small" quaternary @click="rotate(n)">{{ t('cluster.newToken') }}</NButton>
                  <NButton v-if="n.status !== 'revoked'" size="small" quaternary type="error" @click="revoke(n)">
                    {{ t('cluster.revoke') }}
                  </NButton>
                  <NButton v-else size="small" quaternary type="error" :disabled="n.lanes.length > 0" @click="remove(n)">
                    {{ t('common.delete') }}
                  </NButton>
                </template>
              </td>
            </tr>
            <tr v-if="n.last_error" class="error-row">
              <td colspan="7" class="small">{{ n.last_error }}</td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>

  <MachineDrawer :machine-id="openId" @close="openId = null" />

  <NModal
    v-model:show="form.open"
    preset="card"
    :title="form.id === null ? t('cluster.add') : t('cluster.edit')"
    :bordered="false"
    style="width: 480px; max-width: calc(100vw - 32px)"
  >
    <div class="fields">
      <label>
        <span class="field-label">{{ t('cluster.name') }}</span>
        <NInput v-model:value="form.name" maxlength="64" placeholder="gpu-b" data-field="node-name" />
      </label>
      <label v-if="form.id === null || !nodes.find((x) => x.id === form.id)?.is_local">
        <span class="field-label">{{ t('cluster.address') }}</span>
        <NInput v-model:value="form.address" maxlength="255" :placeholder="t('cluster.addressAuto')" />
        <p class="faint small hint">{{ t('cluster.addressHint') }}</p>
      </label>
      <label v-if="form.id !== null">
        <span class="field-label">{{ t('sharing.owner') }}</span>
        <NSelect v-model:value="form.owner" :options="ownerOptions" filterable data-field="node-owner" />
        <p class="faint small hint">{{ t('sharing.ownerHint') }}</p>
      </label>
    </div>
    <template #footer>
      <div class="footer">
        <NButton @click="form.open = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="busy" :disabled="!form.name.trim()" @click="save">
          {{ form.id === null ? t('cluster.create') : t('common.save') }}
        </NButton>
      </div>
    </template>
  </NModal>

  <NModal
    :show="sharingNode !== null"
    preset="card"
    :title="t('sharing.manageTitle', { name: sharingNode?.name ?? '' })"
    :bordered="false"
    style="width: 760px; max-width: calc(100vw - 32px)"
    @update:show="(v: boolean) => !v && ((sharingNode = null), load())"
  >
    <SharingPanel v-if="sharingNode" :node-id="sharingNode.id" />
  </NModal>

  <NModal
    :show="issued !== null"
    preset="card"
    :title="t('cluster.tokenTitle', { name: issued?.node.name ?? '' })"
    :bordered="false"
    style="width: 720px; max-width: calc(100vw - 32px)"
    @update:show="(v: boolean) => !v && (issued = null)"
  >
    <template v-if="issued">
      <p class="small">{{ t('cluster.tokenOnce') }}</p>
      <CodeBlock :code="issued.token" inline />
      <template v-if="issued.install_linux">
        <h4 class="sub">{{ t('sharing.install') }}</h4>
        <p class="faint small">{{ t('sharing.installHint') }}</p>
        <p class="small label">{{ t('sharing.installLinux') }}</p>
        <CodeBlock :code="issued.install_linux" />
        <p class="small label">{{ t('sharing.installWindows') }}</p>
        <CodeBlock :code="issued.install_windows ?? ''" />
      </template>
      <h4 class="sub">{{ t('cluster.docker') }}</h4>
      <p class="faint small">{{ t('cluster.dockerHint') }}</p>
      <CodeBlock :code="issued.docker_command" />
      <h4 class="sub">{{ t('cluster.direct') }}</h4>
      <CodeBlock :code="issued.agent_command" />
    </template>
    <template #footer>
      <div class="footer">
        <NButton type="primary" @click="issued = null">{{ t('cluster.done') }}</NButton>
      </div>
    </template>
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
.lane {
  margin: 0 4px 4px 0;
}
.owner {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin: 2px 0;
}
.label {
  margin: 10px 0 4px;
}
.error-row td {
  color: var(--warning, #b45309);
  border-top: none;
  padding-top: 0;
}
.fields {
  display: grid;
  gap: 16px;
}
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 6px;
}
.hint {
  margin: 6px 0 0;
}
.sub {
  margin: 18px 0 4px;
  font-size: 13.5px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
