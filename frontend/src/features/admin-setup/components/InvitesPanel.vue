<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { NButton, useDialog } from 'naive-ui'
import { Link2, Plus, UserCheck } from 'lucide-vue-next'
import { useErrorMessage } from '@/composables/useErrorMessage'
import { formatDate, formatDateTime, relativeFromNow } from '@/utils/format'
import { setupApi, type Invite, type PendingUser } from '../api'
import InviteDialog from './InviteDialog.vue'

// Invite links (F06 advanced): who signed up through each, revoke; and the sign-ups waiting for approval.
const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const dialog = useDialog()
const { showError, message } = useErrorMessage()

const invites = ref<Invite[]>([])
const pending = ref<PendingUser[]>([])
const loaded = ref(false)
const createOpen = ref(false)
const showInactive = ref(false)

async function load() {
  try {
    ;[invites.value, pending.value] = await Promise.all([setupApi.invites(), setupApi.pending()])
  } catch (e) {
    showError(e)
  } finally {
    loaded.value = true
  }
}
onMounted(load)
defineExpose({ load })

const active = computed(() => invites.value.filter((i) => i.state === 'active'))
const inactive = computed(() => invites.value.filter((i) => i.state !== 'active'))
const shown = computed(() => (showInactive.value ? invites.value : active.value))
const label = (i: Invite) => i.note || `${i.prefix}…`

function revoke(i: Invite) {
  dialog.warning({
    title: t('adminSetup.invites.revoke'),
    content: t('adminSetup.invites.revokeConfirm', { name: label(i) }),
    positiveText: t('adminSetup.invites.revoke'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await setupApi.revokeInvite(i.id)
        message.success(t('adminSetup.invites.revoked'))
        await load()
      } catch (e) {
        showError(e)
      }
    },
  })
}

async function approve(u: PendingUser) {
  try {
    await setupApi.approve(u.id)
    message.success(t('adminSetup.invites.approved', { name: u.username }))
    await load()
    emit('changed')
  } catch (e) {
    showError(e)
  }
}

function reject(u: PendingUser) {
  dialog.warning({
    title: t('adminSetup.invites.reject'),
    content: t('adminSetup.invites.rejectConfirm', { name: u.username }),
    positiveText: t('adminSetup.invites.reject'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await setupApi.reject(u.id)
        message.success(t('adminSetup.invites.rejected'))
        await load()
        emit('changed')
      } catch (e) {
        showError(e)
      }
    },
  })
}
</script>

<template>
  <div class="invites-wrap">
    <section v-if="pending.length" class="card pending" data-testid="pending-approvals">
      <div class="card-header">
        <div>
          <h3 class="card-title"><UserCheck :size="16" /> {{ t('adminSetup.invites.pendingTitle') }} <span class="badge warn">{{ pending.length }}</span></h3>
          <div class="card-sub">{{ t('adminSetup.invites.pendingSub') }}</div>
        </div>
      </div>
      <ul class="list">
        <li v-for="u in pending" :key="u.id">
          <span class="avatar">{{ u.username.slice(0, 1).toUpperCase() }}</span>
          <div class="who">
            <div class="name">{{ u.username }}</div>
            <div class="faint small">
              {{ relativeFromNow(u.created_at) }}
              <template v-if="u.invite_prefix">· {{ t('adminSetup.invites.via', { name: u.invite_note || `${u.invite_prefix}…` }) }}</template>
            </div>
          </div>
          <NButton size="small" type="primary" secondary @click="approve(u)">{{ t('adminSetup.invites.approve') }}</NButton>
          <NButton size="small" quaternary @click="reject(u)">{{ t('adminSetup.invites.reject') }}</NButton>
        </li>
      </ul>
    </section>

    <section class="card invites" data-testid="invites">
      <div class="card-header">
        <div>
          <h3 class="card-title"><Link2 :size="16" /> {{ t('adminSetup.invites.title') }}</h3>
          <div class="card-sub">{{ t('adminSetup.invites.sub') }}</div>
        </div>
        <NButton size="small" type="primary" secondary @click="createOpen = true">
          <template #icon><Plus :size="15" /></template>
          {{ t('adminSetup.invites.create') }}
        </NButton>
      </div>
      <div v-if="loaded && !shown.length" class="empty">{{ t('adminSetup.invites.empty') }}</div>
      <div v-else-if="shown.length" class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>{{ t('adminSetup.invites.prefix') }}</th>
              <th>{{ t('adminSetup.invites.state') }}</th>
              <th class="right">{{ t('adminSetup.invites.uses') }}</th>
              <th>{{ t('adminSetup.invites.expires') }}</th>
              <th>{{ t('adminSetup.invites.people') }}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="i in shown" :key="i.id" :class="{ off: i.state !== 'active' }">
              <td>
                <div class="name">{{ i.note || '—' }}</div>
                <div class="faint small mono">
                  {{ i.prefix }}… <template v-if="i.created_by">· {{ t('adminSetup.invites.createdBy', { name: i.created_by }) }}</template>
                </div>
              </td>
              <td>
                <span class="badge" :class="{ ok: i.state === 'active', danger: i.state === 'revoked' }">
                  {{ t(`adminSetup.invites.states.${i.state}`) }}
                </span>
                <span v-if="i.requires_approval" class="badge warn approval">{{ t('adminSetup.invites.needsApproval') }}</span>
              </td>
              <td class="right num">{{ i.uses }} / {{ i.max_uses }}</td>
              <td class="muted num nowrap" :title="formatDateTime(i.expires_at)">{{ formatDate(i.expires_at) }}</td>
              <td class="small people">
                <span v-for="u in i.users.slice(0, 6)" :key="u.id" class="person" :class="{ waiting: u.approval_pending }">{{ u.username }}</span>
                <span v-if="i.users.length > 6" class="faint">+{{ i.users.length - 6 }}</span>
                <span v-if="!i.users.length" class="faint">—</span>
              </td>
              <td class="right">
                <NButton v-if="i.state === 'active'" size="tiny" quaternary type="error" @click="revoke(i)">
                  {{ t('adminSetup.invites.revoke') }}
                </NButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="inactive.length" class="foot">
        <NButton size="tiny" quaternary @click="showInactive = !showInactive">
          {{ t('adminSetup.invites.showInactive', { n: inactive.length }) }}
        </NButton>
      </div>
      <InviteDialog v-model:show="createOpen" @created="load" />
    </section>
  </div>
</template>

<style scoped>
.pending,
.invites {
  margin-bottom: 16px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  border-top: 1px solid var(--border);
}
.list li:first-child {
  border-top: none;
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
  font-size: 12px;
  flex: none;
}
.who {
  flex: 1;
  min-width: 0;
}
.name {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.nowrap {
  white-space: nowrap;
}
.approval {
  margin-left: 4px;
}
.people {
  max-width: 280px;
}
.person {
  display: inline-block;
  margin: 0 6px 2px 0;
}
.person.waiting {
  color: var(--warn-text);
}
tr.off td {
  color: var(--text-3);
}
.foot {
  padding: 8px 16px;
  border-top: 1px solid var(--border);
}
</style>
